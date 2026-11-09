"""Consumer order history, live tracker and the escrow PIN release desk."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..integrations import dispatch as dispatch_service
from ..models import Order
from ..services import escrow as escrow_service
from ..services import sla as sla_service

bp = Blueprint("orders", __name__)


def resolve_order(ref: str) -> Order:
    """Accept either the human order number (NB-...) or the numeric id."""
    order = db.session.query(Order).filter(Order.order_number == ref).first()
    if order is None and ref.isdigit():
        order = db.session.get(Order, int(ref))
    if order is None:
        abort(404)
    return order


def can_view(order: Order) -> bool:
    if current_user.is_admin:
        return True
    if order.user_id == current_user.id:
        return True
    return current_user.is_merchant and current_user.store_id == order.store_id


@bp.get("/")
@login_required
def index():
    orders = (
        db.session.query(Order)
        .filter(Order.user_id == current_user.id)
        .order_by(Order.created_at.desc())
        .limit(50)
        .all()
    )
    live = [o for o in orders if o.status not in ("delivered", "cancelled", "refunded")]
    return render_template("orders/index.html", orders=orders, live=live)


@bp.get("/<ref>")
@login_required
def detail(ref):
    order = resolve_order(ref)
    if not can_view(order):
        abort(403)

    return render_template(
        "orders/detail.html",
        order=order,
        is_owner=order.user_id == current_user.id,
        transit=dispatch_service.transit_state(order),
        ledger=escrow_service.ledger_for(order),
        pick_list=None,
        reveal_pin=order.user_id == current_user.id and order.payment_escrow_status == "held",
    )


@bp.post("/<ref>/release")
@login_required
def release(ref):
    """Courier-facing: submit the buyer's 4-digit code to release escrow."""
    order = resolve_order(ref)
    if not (current_user.is_admin or (current_user.is_merchant and current_user.store_id == order.store_id)):
        abort(403)

    pin = request.form.get("pin", "")
    try:
        summary = escrow_service.attempt_pin_release(
            order, pin, actor=f"{current_user.user_type}:{current_user.name}"
        )
    except escrow_service.EscrowError as exc:
        db.session.commit()
        flash(str(exc), "danger")
    else:
        db.session.commit()
        flash(
            f"Escrow released. Merchant {summary['merchant_payout']:,.0f} NGN, "
            f"courier {summary['rider_payout']:,.0f} NGN.",
            "success",
        )
    return redirect(url_for("merchant.order_detail", ref=order.order_number))


@bp.post("/<ref>/cancel")
@login_required
def cancel(ref):
    """Buyer-initiated cancellation, only before the merchant starts packing."""
    order = resolve_order(ref)
    if order.user_id != current_user.id and not current_user.is_admin:
        abort(403)

    if order.status not in ("pending_payment", "paid_escrow"):
        flash("This order can no longer be cancelled - the store is already packing.", "warning")
        return redirect(url_for("orders.detail", ref=order.order_number))

    try:
        escrow_service.refund(order, f"Cancelled by {current_user.user_type} before picking")
    except escrow_service.EscrowError as exc:
        flash(str(exc), "danger")
    else:
        flash("Order cancelled and escrow refunded.", "success")
    db.session.commit()
    return redirect(url_for("orders.detail", ref=order.order_number))


@bp.post("/<ref>/sla-check")
@login_required
def sla_check(ref):
    """Manual SLA refresh used by the admin console."""
    if not current_user.is_admin:
        abort(403)
    order = resolve_order(ref)
    change = sla_service.evaluate_order(order)
    db.session.commit()
    if change:
        flash(f"SLA {change['event']} recorded at {change['minutes_elapsed']} minutes.", "warning")
    else:
        flash("Order is within its SLA window.", "success")
    return redirect(url_for("orders.detail", ref=order.order_number))
