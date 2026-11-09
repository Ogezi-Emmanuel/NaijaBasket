"""Subscription pass portal - monthly recurring baskets."""

from dataclasses import dataclass
from datetime import timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..constants import PASS_MONTHLY, PASS_PLANS, PAY_FLUTTERWAVE, PAY_PAYSTACK
from ..extensions import db
from ..integrations import payments as payments_service
from ..models import Order, SubscriptionPass, money, utcnow
from ..security import generate_token

bp = Blueprint("subscription", __name__)


@dataclass
class _PassCharge:
    """Minimal order-shaped object the gateways can initialise against."""

    order_number: str
    gmv_amount: float
    user: object


def plan_price(plan: str) -> float:
    spec = PASS_PLANS.get(plan, PASS_PLANS[PASS_MONTHLY])
    return money(current_app.config["SUBSCRIPTION_PASS_PRICE"] * spec["multiplier"])


@bp.get("/")
@login_required
def portal():
    active = (
        db.session.query(SubscriptionPass)
        .filter(
            SubscriptionPass.user_id == current_user.id,
            SubscriptionPass.status == "active",
            SubscriptionPass.expires_at > utcnow(),
        )
        .order_by(SubscriptionPass.expires_at.desc())
        .first()
    )
    history = (
        db.session.query(SubscriptionPass)
        .filter(SubscriptionPass.user_id == current_user.id)
        .order_by(SubscriptionPass.started_at.desc())
        .limit(8)
        .all()
    )
    orders = (
        db.session.query(Order)
        .filter(Order.user_id == current_user.id, Order.status == "delivered")
        .count()
    )
    allocation = money(
        current_app.config["SUBSCRIPTION_PASS_PRICE"]
        / max(1, current_app.config["SUBSCRIPTION_ORDERS_PER_PASS"])
    )
    return render_template(
        "subscription/portal.html",
        plans=PASS_PLANS,
        prices={plan: plan_price(plan) for plan in PASS_PLANS},
        active=active,
        history=history,
        orders_delivered=orders,
        per_order_allocation=allocation,
        gateway_modes=payments_service.gateway_modes(),
    )


@bp.post("/purchase")
@login_required
def purchase():
    plan = request.form.get("plan", PASS_MONTHLY)
    if plan not in PASS_PLANS:
        flash("Choose a valid pass.", "danger")
        return redirect(url_for("subscription.portal"))

    price = plan_price(plan)
    months = PASS_PLANS[plan]["months"]
    reference = f"NB-SUB-{generate_token(6)}"

    pass_row = SubscriptionPass(
        user_id=current_user.id,
        plan=plan,
        price=price,
        status="pending",
        reference=reference,
        expires_at=utcnow() + timedelta(days=30 * months),
    )
    db.session.add(pass_row)
    db.session.commit()

    requested = (request.form.get("gateway") or PAY_PAYSTACK).lower()
    gateway = payments_service.get_gateway(requested)
    if not gateway.is_live:
        _activate(pass_row)
        flash(
            f"{PASS_PLANS[plan]['label']} activated in sandbox mode. "
            f"Checkout now carries {money(price / 10)} of recognised revenue per order.",
            "success",
        )
        return redirect(url_for("subscription.portal"))

    charge = _PassCharge(
        order_number=f"SUB-{reference[-6:]}", gmv_amount=price, user=current_user
    )
    try:
        intent = gateway.initialize(charge)
    except payments_service.PaymentGatewayError as exc:
        flash(f"Could not start the pass payment: {exc}", "danger")
        return redirect(url_for("subscription.portal"))

    pass_row.reference = intent.reference
    db.session.commit()
    return redirect(intent.authorization_url)


@bp.get("/callback")
@login_required
def callback():
    reference = request.args.get("reference") or request.args.get("tx_ref")
    pass_row = (
        db.session.query(SubscriptionPass)
        .filter(SubscriptionPass.reference == reference, SubscriptionPass.user_id == current_user.id)
        .first()
    )
    if pass_row is None:
        flash("We could not match that payment to a pass.", "danger")
        return redirect(url_for("subscription.portal"))

    gateway = payments_service.get_gateway(_gateway_from_reference(reference))
    result = gateway.verify(reference)
    if not result.ok:
        pass_row.status = "failed"
        db.session.commit()
        flash("Pass payment was not completed.", "danger")
        return redirect(url_for("subscription.portal"))

    _activate(pass_row)
    flash(f"{PASS_PLANS[pass_row.plan]['label']} is active.", "success")
    return redirect(url_for("subscription.portal"))


@bp.post("/cancel")
@login_required
def cancel():
    pass_row = (
        db.session.query(SubscriptionPass)
        .filter(
            SubscriptionPass.user_id == current_user.id,
            SubscriptionPass.status == "active",
        )
        .order_by(SubscriptionPass.expires_at.desc())
        .first()
    )
    if pass_row is None:
        flash("You have no active pass.", "warning")
        return redirect(url_for("subscription.portal"))

    pass_row.status = "cancelled"
    pass_row.cancelled_at = utcnow()
    current_user.subscription_pass = False
    db.session.commit()
    flash("Pass cancelled. You keep access until the period ends.", "info")
    return redirect(url_for("subscription.portal"))


def _gateway_from_reference(reference: str | None) -> str:
    """References are minted as NB-<GATEWAY>-... so we can verify without state."""
    token = (reference or "").upper()
    if token.startswith("NB-FLU"):
        return PAY_FLUTTERWAVE
    return PAY_PAYSTACK


def _activate(pass_row: SubscriptionPass) -> None:
    pass_row.status = "active"
    pass_row.started_at = utcnow()
    months = PASS_PLANS.get(pass_row.plan, PASS_PLANS[PASS_MONTHLY])["months"]
    pass_row.expires_at = utcnow() + timedelta(days=30 * months)

    user = pass_row.user
    user.subscription_pass = True
    user.subscription_expires_at = pass_row.expires_at
    db.session.commit()
