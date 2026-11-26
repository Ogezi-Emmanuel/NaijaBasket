"""NaijaBasket application factory.

Asset-light digital retail & grocery aggregation PWA:
consumer storefront -> partner supermarket OMS (15-minute SLA) -> 3PL dispatch
-> PIN-verified escrow release.
"""

import logging
import os
import threading

import click
from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import CONFIGS
from .constants import (
    CATEGORIES,
    CATEGORY_LABELS,
    ORDER_STATUS_LABELS,
    PAYMENT_CHANNELS,
    SAFETY_LABELS,
)
from .extensions import csrf, db, limiter, login_manager, migrate
from .models import money, utcnow

BLUEPRINTS = (
    ("auth", "/auth", "auth"),
    ("storefront", "", "storefront"),
    ("checkout", "/checkout", "checkout"),
    ("orders", "/orders", "orders"),
    ("subscription", "/subscription", "subscription"),
    ("merchant", "/merchant", "merchant"),
    ("admin", "/admin", "admin"),
    ("webhooks", "/webhooks", "webhooks"),
    ("api", "/api", "api"),
)

CSRF_EXEMPT_BLUEPRINTS = ("webhooks",)


def create_app(config_name: str | None = None) -> Flask:
    config_name = config_name or _resolve_config_name()
    config_class = CONFIGS.get(config_name, CONFIGS["default"])

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(config_class)
    config_class.init_app(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    _register_edge(app)
    _register_observability(app)
    _register_blueprints(app)
    _register_jinja(app)
    _register_errors(app)
    _register_cli(app)
    _register_bootstrap(app)

    return app


def _resolve_config_name() -> str:
    """Choose a configuration profile.

    Vercel exports VERCEL=1, so the app auto-selects the serverless profile and
    boots with zero configuration. An explicit FLASK_ENV still wins.
    """
    explicit = os.environ.get("FLASK_ENV")
    if explicit:
        return explicit
    if os.environ.get("VERCEL"):
        return "serverless"
    return "development"


_bootstrap_lock = threading.Lock()
_bootstrapped = False


def _register_bootstrap(app: Flask) -> None:
    """Create the schema and load the demo dataset once per container.

    Only the serverless profile opts in via AUTO_BOOTSTRAP. The work happens at
    application creation (i.e. cold start) so the very first request already has
    the seeded role accounts. It is idempotent, so warm re-imports are cheap.
    """
    if not app.config.get("AUTO_BOOTSTRAP"):
        return

    global _bootstrapped
    with _bootstrap_lock:
        if _bootstrapped:
            return
        try:
            with app.app_context():
                from .seed import seed_all

                summary = seed_all()
            app.logger.info("Bootstrap complete: %s", summary)
        except Exception:  # noqa: BLE001 - setup must never take the app down
            app.logger.exception(
                "Bootstrap failed; continuing with an empty database"
            )
        _bootstrapped = True


def _register_edge(app: Flask) -> None:
    """Trust the proxy chain so scheme/host/client-IP are correct.

    Without this, `url_for(..., _external=True)` builds the gateway callback
    URLs from the internal host over http, which breaks post-payment redirects.
    """
    hops = app.config.get("PROXY_FIX_HOPS", 0)
    if hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_port=hops
        )


def _register_observability(app: Flask) -> None:
    if not app.config.get("TESTING"):
        logging.basicConfig(
            level=app.config.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    dsn = app.config.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:  # pragma: no cover - sentry-sdk is an optional extra
        app.logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return

    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        traces_sample_rate=app.config.get("SENTRY_TRACES_SAMPLE_RATE", 0.1),
        environment=app.config.get("ENVIRONMENT", "production"),
        release=app.config.get("APP_RELEASE") or None,
    )


def _register_blueprints(app: Flask) -> None:
    import importlib

    for module_name, prefix, name in BLUEPRINTS:
        module = importlib.import_module(f"app.blueprints.{module_name}")
        app.register_blueprint(module.bp, url_prefix=prefix)
        if name in CSRF_EXEMPT_BLUEPRINTS:
            csrf.exempt(module.bp)


def _register_jinja(app: Flask) -> None:
    from flask_login import current_user

    from .integrations.dispatch import courier_modes
    from .integrations.payments import gateway_modes
    from .integrations.whatsapp import whatsapp_modes
    from .services import cart as cart_service

    @app.template_filter("naira")
    def naira(value):
        try:
            return f"\u20a6{float(value):,.0f}"
        except (TypeError, ValueError):
            return "\u20a60"

    @app.template_filter("naira2")
    def naira2(value):
        try:
            return f"\u20a6{float(value):,.2f}"
        except (TypeError, ValueError):
            return "\u20a60.00"

    @app.template_filter("pct")
    def pct(value, digits=1):
        try:
            return f"{float(value):.{digits}f}%"
        except (TypeError, ValueError):
            return "0%"

    @app.template_filter("timeago")
    def timeago(value):
        if not value:
            return ""
        delta = (utcnow() - value).total_seconds()
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"

    @app.template_filter("hhmmss")
    def hhmmss(seconds):
        seconds = max(0, int(seconds or 0))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    # Static label tables belong in the Jinja environment, not the request
    # context: macros imported with `{% from %}` are evaluated without the
    # render context, so context-processor variables are invisible inside them.
    app.jinja_env.globals.update(
        CATEGORIES=CATEGORIES,
        CATEGORY_LABELS=CATEGORY_LABELS,
        SAFETY_LABELS=SAFETY_LABELS,
        ORDER_STATUS_LABELS=ORDER_STATUS_LABELS,
        PAYMENT_CHANNELS=PAYMENT_CHANNELS,
    )

    @app.context_processor
    def inject_globals():
        cart = None
        cart_count = 0
        if current_user.is_authenticated:
            cart = cart_service.cart_for(current_user)
            cart_count = cart.item_count if cart else 0

        return {
            "NB": {
                "min_order_value": money(app.config["MIN_ORDER_VALUE"]),
                "target_aov": money(app.config["TARGET_AOV"]),
                "convenience_fee": money(app.config["CONVENIENCE_FEE"]),
                "merchant_sla_minutes": app.config["MERCHANT_SLA_MINUTES"],
                "transit_sla_minutes": app.config["TRANSIT_SLA_MINUTES"],
                "delivery_radius_km": app.config["DELIVERY_RADIUS_KM"],
                "gateways": gateway_modes(),
                "couriers": courier_modes(),
                "whatsapp": whatsapp_modes(),
            },
            "active_cart": cart,
            "cart_count": cart_count,
            "current_year": utcnow().year,
        }


def _register_errors(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(error):
        if _wants_json():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(error):
        if _wants_json():
            return jsonify({"ok": False, "error": "not_found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        if _wants_json():
            return jsonify({"ok": False, "error": "server_error"}), 500
        return render_template("errors/500.html"), 500

    @app.errorhandler(429)
    def rate_limited(error):
        if _wants_json():
            return jsonify({"ok": False, "error": "rate_limited"}), 429
        return (
            render_template(
                "errors/400.html",
                message="Too many attempts. Please wait a moment and try again.",
            ),
            429,
        )

    @app.errorhandler(ValueError)
    def bad_request(error):
        if _wants_json():
            return jsonify({"ok": False, "error": str(error)}), 400
        return render_template("errors/400.html", message=str(error)), 400

    def _wants_json() -> bool:
        return request.path.startswith("/api/") or request.is_json


def _register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db():
        """Create tables directly. Local development only - deployed
        environments apply schema changes with `flask db upgrade`."""
        db.create_all()
        click.echo("Database schema created.")

    @app.cli.command("reset-db")
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    def reset_db(yes):
        """Drop and recreate all tables."""
        if not yes:
            click.confirm("This deletes every record. Continue?", abort=True)
        db.drop_all()
        db.create_all()
        click.echo("Database reset.")

    @app.cli.command("seed")
    def seed_db():
        """Load demo stores, products, users, orders and an abandoned cart."""
        from .seed import seed_all

        summary = seed_all()
        for key, value in summary.items():
            click.echo(f"  {key}: {value}")
        click.echo("Seed complete.")

    @app.cli.command("sla-check")
    def sla_check():
        """Escalate orders that blew the 15-minute picking window."""
        from .services import sla as sla_service

        result = sla_service.reconcile()
        click.echo(
            f"SLA sweep: {result['warnings']} warning(s), {result['breaches']} breach(es) "
            f"across {result['evaluated']} transition(s)."
        )

    @app.cli.command("cart-recovery")
    @click.option("--limit", default=200, show_default=True)
    def cart_recovery(limit):
        """Generate WhatsApp recovery links for carts idle > 30 minutes."""
        from .services import whatsapp as whatsapp_service

        links = whatsapp_service.run_sweep(limit=limit)
        click.echo(f"{len(links)} recovery message(s) dispatched.")
        for entry in links:
            click.echo(
                f"  cart #{entry['cart_id']} [{entry['provider']}/{entry['mode']}] "
                f"{entry['status']} -> {entry['link']}"
            )

    @app.cli.command("benchmark")
    def benchmark():
        """Print the PRD's target tiered unit economics."""
        from .services import economics as economics_service

        for key, value in economics_service.benchmark_assertion().items():
            click.echo(f"  {key}: {value}")
