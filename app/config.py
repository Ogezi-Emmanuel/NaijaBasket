"""Application configuration.

Every business constant from the PRD lives here so that the deterministic
financial and SLA logic has a single source of truth. Values are overridable
by environment variables for deployment without code changes.
"""

import os
from datetime import timedelta

from sqlalchemy.pool import NullPool

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def _normalize_db_uri(uri: str) -> str:
    """Accept managed-Postgres URLs as handed out by hosting providers."""
    if uri.startswith("postgres://"):
        return "postgresql+psycopg2://" + uri[len("postgres://") :]
    if uri.startswith("postgresql://"):
        return "postgresql+psycopg2://" + uri[len("postgresql://") :]
    return uri


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: Serverless functions get one writable directory. On Vercel this is /tmp.
SERVERLESS_DB_PATH = "/tmp/naijabasket.db"
SERVERLESS_DB_URI = "sqlite:///" + SERVERLESS_DB_PATH


def _serverless_engine_options(uri: str) -> dict:
    """Engine options for a stateless function container.

    Connections cannot outlive an invocation, so pooling is pointless (and
    risky) -> NullPool opens a fresh connection each time. SQLite also needs
    cross-thread access enabled because the runtime may reuse a worker thread.
    """
    options: dict = {"pool_pre_ping": True, "poolclass": NullPool}
    if uri.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return options



class Config:
    # --- Core -----------------------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "naijabasket-dev-secret-change-me")
    JSON_SORT_KEYS = False

    # --- Database -------------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = _normalize_db_uri(
        os.environ.get("DATABASE_URL")
        or "sqlite:///" + os.path.join(BASE_DIR, "naijabasket.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Session / cookie hardening -------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    WTF_CSRF_TIME_LIMIT = None

    # --- PII encryption (NDPA 2023) -------------------------------------------
    ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

    # --- Cart engine (FR-01) --------------------------------------------------
    MIN_ORDER_VALUE = _int_env("MIN_ORDER_VALUE", 15_000)
    TARGET_AOV = _int_env("TARGET_AOV", 35_000)

    # --- Unit economics -------------------------------------------------------
    CONVENIENCE_FEE = _int_env("CONVENIENCE_FEE", 300)
    GATEWAY_FEE_RATE = _float_env("GATEWAY_FEE_RATE", 0.015)
    GATEWAY_FEE_CAP = _int_env("GATEWAY_FEE_CAP", 2_000)
    PROCESSING_DIRECT_COST = _int_env("PROCESSING_DIRECT_COST", 175)
    DEFAULT_TAKE_RATE = _float_env("DEFAULT_TAKE_RATE", 0.055)
    MIN_TAKE_RATE = _float_env("MIN_TAKE_RATE", 0.03)
    MAX_TAKE_RATE = _float_env("MAX_TAKE_RATE", 0.08)
    SUBSCRIPTION_PASS_PRICE = _int_env("SUBSCRIPTION_PASS_PRICE", 5_000)
    SUBSCRIPTION_ORDERS_PER_PASS = _int_env("SUBSCRIPTION_ORDERS_PER_PASS", 10)

    # --- SLA (FR-02 / success criteria) ---------------------------------------
    MERCHANT_SLA_MINUTES = _int_env("MERCHANT_SLA_MINUTES", 15)
    SLA_WARNING_MINUTES = _int_env("SLA_WARNING_MINUTES", 3)
    TRANSIT_SLA_MINUTES = _int_env("TRANSIT_SLA_MINUTES", 45)

    # --- Logistics ------------------------------------------------------------
    DELIVERY_RADIUS_KM = _int_env("DELIVERY_RADIUS_KM", 8)
    DELIVERY_RADIUS_MIN_KM = _int_env("DELIVERY_RADIUS_MIN_KM", 5)
    LOGISTICS_BASE_COST = _int_env("LOGISTICS_BASE_COST", 175)
    DISPATCH_PROVIDER = os.environ.get("DISPATCH_PROVIDER", "auto")

    # --- Cart recovery (FR-06) ------------------------------------------------
    CART_RECOVERY_MINUTES = _int_env("CART_RECOVERY_MINUTES", 30)
    RECOVERY_DISCOUNT_TOKEN = os.environ.get("RECOVERY_DISCOUNT_TOKEN", "COMEBACK10")
    RECOVERY_DISCOUNT_PERCENT = _float_env("RECOVERY_DISCOUNT_PERCENT", 0.10)
    WHATSAPP_SENDER = os.environ.get("WHATSAPP_SENDER", "2348000000000")

    # --- WhatsApp Business delivery (FR-06) -----------------------------------
    # Blank token / phone-number id -> the deterministic sandbox records the send
    # instead of calling the BSP, so recovery is exercisable without an account.
    WHATSAPP_API_BASE = os.environ.get(
        "WHATSAPP_API_BASE", "https://graph.facebook.com/v21.0"
    )
    WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    # Signs inbound delivery/read receipts (X-Hub-Signature-256).
    WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
    # Echoed back on the Meta webhook subscription handshake.
    WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    WHATSAPP_TEMPLATE_NAME = os.environ.get(
        "WHATSAPP_TEMPLATE_NAME", "abandoned_cart_recovery"
    )
    WHATSAPP_TEMPLATE_LANG = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")

    # --- Threat protection ----------------------------------------------------
    ESCROW_PIN_MAX_ATTEMPTS = _int_env("ESCROW_PIN_MAX_ATTEMPTS", 3)

    # --- Edge / reverse proxy -------------------------------------------------
    # Cloudflare -> Render/Fly terminate TLS upstream, so Flask must be told to
    # trust the X-Forwarded-* headers or url_for(_external=True) hands the
    # gateways an http:// callback URL.
    PROXY_FIX_HOPS = _int_env("PROXY_FIX_HOPS", 0)
    PREFERRED_URL_SCHEME = os.environ.get("PREFERRED_URL_SCHEME", "http")

    # --- Rate limiting --------------------------------------------------------
    RATELIMIT_ENABLED = os.environ.get("RATELIMIT_ENABLED", "1") == "1"
    # Falls back to the platform Redis, then to per-process memory. Memory is
    # only accurate with a single worker; point REDIS_URL at a managed instance
    # so the limits are shared across gunicorn workers.
    RATELIMIT_STORAGE_URI = (
        os.environ.get("RATELIMIT_STORAGE_URI")
        or os.environ.get("REDIS_URL")
        or "memory://"
    )
    RATELIMIT_HEADERS_ENABLED = True

    # --- Observability --------------------------------------------------------
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    ENVIRONMENT = os.environ.get("FLASK_ENV", "development")
    APP_RELEASE = os.environ.get("APP_RELEASE", "")
    SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
    SENTRY_TRACES_SAMPLE_RATE = _float_env("SENTRY_TRACES_SAMPLE_RATE", 0.1)

    # --- Gateways / 3PL -------------------------------------------------------
    PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
    PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
    PAYSTACK_BASE = os.environ.get("PAYSTACK_BASE", "https://api.paystack.co")
    FLUTTERWAVE_SECRET_KEY = os.environ.get("FLUTTERWAVE_SECRET_KEY", "")
    FLUTTERWAVE_PUBLIC_KEY = os.environ.get("FLUTTERWAVE_PUBLIC_KEY", "")
    FLUTTERWAVE_WEBHOOK_HASH = os.environ.get("FLUTTERWAVE_WEBHOOK_HASH", "")
    FLUTTERWAVE_BASE = os.environ.get("FLUTTERWAVE_BASE", "https://api.flutterwave.com")
    KWIK_API_KEY = os.environ.get("KWIK_API_KEY", "")
    KWIK_API_BASE = os.environ.get("KWIK_API_BASE", "https://api.kwik.delivery")
    GOKADA_API_KEY = os.environ.get("GOKADA_API_KEY", "")
    GOKADA_API_BASE = os.environ.get("GOKADA_API_BASE", "https://api.gokada.ng")

    HTTP_TIMEOUT_SECONDS = _float_env("HTTP_TIMEOUT_SECONDS", 12.0)

    @staticmethod
    def init_app(app):  # pragma: no cover - hook for subclasses
        pass


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    ENCRYPTION_KEY = "0" * 64
    SECRET_KEY = "testing-secret-key"
    # Login/webhook throttles would leak between tests through the shared store.
    RATELIMIT_ENABLED = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    ENVIRONMENT = "production"
    # Cloudflare and the PaaS proxy both sit in front of gunicorn.
    PROXY_FIX_HOPS = _int_env("PROXY_FIX_HOPS", 1)
    PREFERRED_URL_SCHEME = "https"

    @staticmethod
    def init_app(app):
        if not os.environ.get("SECRET_KEY"):
            raise RuntimeError("SECRET_KEY must be set in production")
        if not os.environ.get("ENCRYPTION_KEY"):
            raise RuntimeError("ENCRYPTION_KEY must be set in production")


class ServerlessConfig(ProductionConfig):
    """Vercel / serverless profile.

    A function container is ephemeral and mostly read-only, so this profile:
      * keeps the database in /tmp (the only writable path) unless a managed
        DATABASE_URL is supplied;
      * disables connection pooling;
      * bootstraps the demo dataset on first touch, so the seeded role accounts
        work on a pristine deploy with no external host and no manual step;
      * tolerates missing SECRET_KEY / ENCRYPTION_KEY instead of refusing to boot.
    """

    SQLALCHEMY_DATABASE_URI = _normalize_db_uri(
        os.environ.get("DATABASE_URL") or SERVERLESS_DB_URI
    )
    SQLALCHEMY_ENGINE_OPTIONS = _serverless_engine_options(
        os.environ.get("DATABASE_URL") or SERVERLESS_DB_URI
    )

    # No shared Redis / worker exists per-invocation; memory is the only store.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI") or "memory://"

    # Demo-friendly: derive a stable key so a zero-config deploy still boots.
    SECRET_KEY = os.environ.get("SECRET_KEY") or "naijabasket-vercel-demo-secret"
    ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

    # Create the schema and load the demo dataset on cold start.
    AUTO_BOOTSTRAP = True

    @staticmethod
    def init_app(app):  # deliberately lenient - this is the zero-config profile
        app.logger.info(
            "Serverless profile active: db=%s bootstrap=%s",
            app.config["SQLALCHEMY_DATABASE_URI"],
            app.config.get("AUTO_BOOTSTRAP"),
        )


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "serverless": ServerlessConfig,
    "default": DevelopmentConfig,
}
