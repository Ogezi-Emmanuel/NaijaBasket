"""Basket, minimum-order gate and the escrow-backed payment handoff."""

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from ..constants import PAYMENT_CHANNELS
from ..extensions import db
from ..integrations import payments as payments_service
from ..models import Order, money
from ..services import cart as cart_service
from ..services import economics as economics_service
from ..services import geo as geo_service
from ..services import orders as orders_service
from ..services import safety as safety_service

bp = Blueprint("checkout", __name__)


def order_by_reference(reference: str):
    return db.session.query(Order).filter(Order.payment_reference == reference).first()


def settle_payment(order: Order, reference: str | None, *, amount_paid: float | None = None):
    """Escrow hold + stock reservation + basket clear. Called from the gateway
    callback, the sandbox approval page and the webhooks blueprint."""
    from ..services import escrow as escrow_service

    if order.payment_escrow_status == "held":
        return order

    if amount_paid is not None and abs(float(amount_paid) - order.gmv_amount) > 0.5:
        escrow_service.refund(order, f"Amount mismatch: paid {amount_paid} vs {order.gmv_amount}")
        db.session.commit()
        return order

    escrow_service.hold(order, reference)
    orders_service.reserve_stock(order)
    cart = cart_service.cart_for(order.user)
    if cart is not None:
        cart_service.clear(cart)
    db.session.commit()
    return order


@bp.get("/")
@login_required
def basket():
    cart = cart_service.cart_for(current_user)
    subtotal = cart.subtotal if cart else 0.0
    store = cart.store if cart else None
    suggestions = cart_service.frequently_bought_together(cart) if cart and cart.items else []

    distance = None
    if store and current_user.gps_latitude is not None:
        distance = geo_service.distance_between_stores(
            current_user.gps_latitude,
            current_user.gps_longitude,
            store.gps_latitude,
            store.gps_longitude,
        )

    return render_template(
        "checkout/basket.html",
        cart=cart,
        store=store,
        minimum=cart_service.minimum_order_state(subtotal),
        suggestions=suggestions,
        economics=economics_service.estimate_basket_economics(
            subtotal, store.commission_rate if store else current_app.config["DEFAULT_TAKE_RATE"]
        ),
        channels=PAYMENT_CHANNELS,
        distance_km=distance,
        within_radius=(distance is None or geo_service.within_delivery_radius(distance)),
        default_address=current_user.address or "",
    )


@bp.post("/pay")
@login_required
def pay():
    cart = cart_service.cart_for(current_user)
    if cart is None or not cart.items:
        flash("Your basket is empty.", "warning")
        return redirect(url_for("storefront.catalog"))

    channel = request.form.get("channel", "transfer")
    if channel not in PAYMENT_CHANNELS:
        flash("Choose a valid payment method.", "danger")
        return redirect(url_for("checkout.basket"))

    address = (request.form.get("address") or current_user.address or "").strip()
    if len(address) < 8:
        flash("Enter a delivery address so the courier can find you.", "danger")
        return redirect(url_for("checkout.basket"))

    try:
        latitude = float(request.form.get("latitude")) if request.form.get("latitude") else None
        longitude = float(request.form.get("longitude")) if request.form.get("longitude") else None
    except ValueError:
        latitude = longitude = None
    if latitude is None:
        latitude = current_user.gps_latitude or geo_service.LAGOS_CENTER[0]
        longitude = current_user.gps_longitude or geo_service.LAGOS_CENTER[1]

    try:
        order = orders_service.create_order_from_cart(
            current_user,
            cart,
            delivery_address=address,
            latitude=latitude,
            longitude=longitude,
            channel=channel,
            delivery_window=request.form.get("delivery_window", "asap"),
        )
    except cart_service.MinimumOrderError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("checkout.basket"))
    except (cart_service.CartError, orders_service.OrderError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("checkout.basket"))

    gateway = payments_service.gateway_for_channel(channel)
    order.payment_gateway = gateway.name
    db.session.commit()

    try:
        intent = gateway.initialize(order)
    except payments_service.PaymentGatewayError as exc:
        flash(f"Payment could not start: {exc}", "danger")
        return redirect(url_for("checkout.basket", order=order.order_number))

    order.payment_reference = intent.reference
    db.session.commit()

    if intent.is_sandbox:
        flash(
            f"{gateway.label} is in sandbox mode - approving the simulated charge.",
            "info",
        )
    return redirect(intent.authorization_url)


@bp.route("/sandbox/<reference>", methods=["GET", "POST"])
@login_required
def sandbox_gateway(reference):
    order = order_by_reference(reference)
    if order is None or order.user_id != current_user.id:
        flash("That payment session is not available.", "danger")
        return redirect(url_for("checkout.basket"))

    if request.method == "POST":
        if request.form.get("decision") == "decline":
            flash("Sandbox charge declined. Your basket is untouched.", "warning")
            return redirect(url_for("checkout.basket"))
        settle_payment(order, reference, amount_paid=order.gmv_amount)
        flash(
            f"Payment approved. NGN {order.gmv_amount:,.0f} is held in escrow until you "
            "hand the courier your delivery PIN.",
            "success",
        )
        return redirect(url_for("orders.detail", ref=order.order_number))

    return render_template("checkout/sandbox_gateway.html", order=order)


@bp.get("/callback")
@login_required
def callback():
    reference = (
        request.args.get("reference")
        or request.args.get("tx_ref")
        or request.args.get("trxref")
    )
    order = order_by_reference(reference) if reference else None
    if order is None:
        flash("We could not match that payment to an order.", "danger")
        return redirect(url_for("orders.index"))

    if order.user_id != current_user.id and not current_user.is_admin:
        flash("That order belongs to another account.", "danger")
        return redirect(url_for("orders.index"))

    gateway = payments_service.get_gateway(order.payment_gateway)
    try:
        result = gateway.verify(reference)
    except payments_service.PaymentGatewayError as exc:
        flash(f"Payment verification failed: {exc}", "danger")
        return redirect(url_for("orders.detail", ref=order.order_number))

    if not result.ok:
        flash(f"Payment was not completed ({result.message or 'declined'}).", "danger")
        return redirect(url_for("checkout.basket"))

    settle_payment(
        order,
        reference,
        amount_paid=result.amount if result.mode == "live" and result.amount else None,
    )
    if order.payment_escrow_status == "held":
        flash(
            f"Payment confirmed. {order.order_number} is with {order.store.name}; "
            f"the store has {current_app.config['MERCHANT_SLA_MINUTES']} minutes to pick and pack.",
            "success",
        )
        return redirect(url_for("orders.detail", ref=order.order_number))

    flash("We could not secure those funds. Our team has been alerted.", "danger")
    return redirect(url_for("orders.index"))


@bp.post("/recovery/apply")
@login_required
def apply_recovery_token():
    """Basket recovery tokens waive part of the convenience fee."""
    code = (request.form.get("token") or "").strip()
    cart = cart_service.cart_for(current_user)
    if cart is None or not code:
        flash("Enter the code from your WhatsApp message.", "warning")
        return redirect(url_for("checkout.basket"))

    if cart.recovery_token and code.lower() == cart.recovery_token.lower():
        cart.recovery_discount = round(
            current_app.config["CONVENIENCE_FEE"] * current_app.config["RECOVERY_DISCOUNT_PERCENT"], 2
        )
        db.session.commit()
        flash(f"Recovery code applied: {money(cart.recovery_discount)} off your convenience fee.", "success")
    else:
        flash("That recovery code is not valid for this basket.", "danger")
    return redirect(url_for("checkout.basket"))
