"""Merchant OMS: 15-minute SLA queue, pick & pack checklist, dispatch trigger."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..constants import (
    CLOSED_STATUSES,
    DISPATCH_REQUESTED,
    ORDER_STATUS_LABELS,
    SLA_TRACKED_STATUSES,
)
from ..extensions import db
from ..integrations import dispatch as dispatch_service
from ..integrations import payments as payments_service
from ..models import Order, PartnerStore, Product, money, utcnow
from ..security import merchant_required
from ..services import escrow as escrow_service
from ..services import safety as safety_service
from ..services import sla as sla_service

bp = Blueprint("merchant", __name__)


def scoped_store() -> PartnerStore:
    """Merchants are hard-scoped to their own store; admins may inspect any."""
    if current_user.is_admin:
        store_id = request.args.get("store", type=int) or current_user.store_id
        store = db.session.get(PartnerStore, store_id) if store_id else None
        store = store or db.session.query(PartnerStore).order_by(PartnerStore.id).first()
    else:
        store = db.session.get(PartnerStore, current_user.store_id)
    if store is None:
        abort(403)
    return store


@bp.before_request
@merchant_required
def _guard():
    return None


@bp.get("/")
def dashboard():
    store = scoped_store()
    live = (
        db.session.query(Order)
        .filter(Order.store_id == store.id, Order.status.in_(SLA_TRACKED_STATUSES))
        .order_by(Order.sla_deadline.asc())
        .all()
    )
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    revenue_rows = (
        db.session.query(
            Order.id,
            Order.gmv_amount,
            Order.gross_revenue,
            Order.net_contribution_margin,
            Order.status,
        )
        .filter(Order.store_id == store.id)
        .all()
    )
    todays_orders = (
        db.session.query(Order)
        .filter(Order.store_id == store.id, Order.created_at >= today_start)
        .count()
    )

    return render_template(
        "merchant/dashboard.html",
        store=store,
        live_orders=live,
        compliance=sla_service.store_compliance(store),
        kpis={
            "live_queue": len(live),
            "orders_today": todays_orders,
            "gmv": money(sum(r.gmv_amount for r in revenue_rows)),
            "platform_revenue": money(sum(r.gross_revenue for r in revenue_rows)),
            "orders_total": len(revenue_rows),
            "delivered": len([r for r in revenue_rows if r.status == "delivered"]),
        },
        low_stock=(
            db.session.query(Product)
            .filter(Product.store_id == store.id, Product.stock_qty <= 12)
            .order_by(Product.stock_qty.asc())
            .limit(6)
            .all()
        ),
        gateway_modes=payments_service.gateway_modes(),
        courier_modes=dispatch_service.courier_modes(),
    )


@bp.get("/orders")
def orders():
    store = scoped_store()
    live = (
        db.session.query(Order)
        .filter(Order.store_id == store.id, Order.status.in_(SLA_TRACKED_STATUSES))
        .order_by(Order.sla_deadline.asc())
        .all()
    )
    packed = (
        db.session.query(Order)
        .filter(Order.store_id == store.id, Order.status.in_(("packed", "dispatched", "en_route")))
        .order_by(Order.packed_at.desc())
        .limit(12)
        .all()
    )
    closed = (
        db.session.query(Order)
        .filter(Order.store_id == store.id, Order.status.in_(CLOSED_STATUSES))
        .order_by(Order.created_at.desc())
        .limit(12)
        .all()
    )
    return render_template(
        "merchant/orders.html",
        store=store,
        live_orders=live,
        packed_orders=packed,
        closed_orders=closed,
    )


def _scoped_order(ref: str) -> Order:
    order = db.session.query(Order).filter(Order.order_number == ref).first()
    if order is None and ref.isdigit():
        order = db.session.get(Order, int(ref))
    if order is None:
        abort(404)
    if not current_user.is_admin and order.store_id != current_user.store_id:
        abort(403)
    return order


@bp.get("/orders/<ref>")
def order_detail(ref):
    order = _scoped_order(ref)
    return render_template(
        "merchant/order_detail.html",
        order=order,
        store=order.store,
        pick_list=safety_service.build_pick_list(order.items),
        transit=dispatch_service.transit_state(order),
        ledger=escrow_service.ledger_for(order),
        status_labels=ORDER_STATUS_LABELS,
    )


@bp.post("/orders/<ref>/packed")
def mark_packed(ref):
    """Merchant confirmation -> automated 3PL dispatch bridge (FR-02 -> FR-03)."""
    order = _scoped_order(ref)
    if order.status not in SLA_TRACKED_STATUSES:
        flash(f"Order is '{order.status_label}', it cannot be marked packed now.", "warning")
        return redirect(url_for("merchant.order_detail", ref=order.order_number))

    order.packed_at = utcnow()
    sla_service.record_met(order)
    order.status = "packed"
    db.session.flush()

    try:
        job = dispatch_service.request_dispatch(order)
    except dispatch_service.DispatchError as exc:
        db.session.commit()
        flash(f"Packed in time, but courier dispatch failed: {exc}", "danger")
        return redirect(url_for("merchant.order_detail", ref=order.order_number))

    db.session.commit()
    flash(
        f"Packed in {round((order.packed_at - order.paid_at).total_seconds() / 60, 1)} min. "
        f"{job.provider.title()} courier {job.rider_name} assigned "
        f"(ETA {job.eta_minutes} min, {job.mode} mode).",
        "success",
    )
    return redirect(url_for("merchant.order_detail", ref=order.order_number))


@bp.post("/orders/<ref>/redispatch")
def redispatch(ref):
    order = _scoped_order(ref)
    if order.status not in ("packed", "sla_breach"):
        flash("Only packed orders can be re-dispatched.", "warning")
        return redirect(url_for("merchant.order_detail", ref=order.order_number))
    try:
        job = dispatch_service.request_dispatch(order, actor=current_user.email)
    except dispatch_service.DispatchError as exc:
        db.session.commit()
        flash(f"Dispatch retry failed: {exc}", "danger")
    else:
        db.session.commit()
        flash(f"Courier re-requested via {job.provider} ({job.mode} mode).", "success")
    return redirect(url_for("merchant.order_detail", ref=order.order_number))


@bp.get("/inventory")
def inventory():
    store = scoped_store()
    products = (
        db.session.query(Product)
        .filter(Product.store_id == store.id)
        .order_by(Product.category, Product.name)
        .all()
    )
    return render_template("merchant/inventory.html", store=store, products=products)


@bp.post("/inventory/<int:product_id>")
def update_inventory(product_id):
    store = scoped_store()
    product = db.session.get(Product, product_id)
    if product is None or (product.store_id != store.id and not current_user.is_admin):
        abort(404)

    stock = request.form.get("stock_qty", type=int)
    if stock is not None:
        product.stock_qty = max(0, stock)

    price = request.form.get("price", type=float)
    if price is not None and price > 0:
        product.price = money(price)

    nafdac = (request.form.get("nafdac_reg_no") or "").strip()
    if nafdac != (product.nafdac_reg_no or ""):
        ok, message = safety_service.validate_nafdac(product.category, nafdac)
        if not ok:
            db.session.rollback()
            flash(message, "danger")
            return redirect(url_for("merchant.inventory"))
        product.nafdac_reg_no = nafdac

    if request.form.get("toggle_active"):
        product.is_active = not product.is_active

    db.session.commit()
    flash(f"{product.name} updated.", "success")
    return redirect(url_for("merchant.inventory"))


@bp.get("/dispatch")
def dispatch_board():
    """Operational view of every courier request raised by this store."""
    store = scoped_store()
    jobs = (
        db.session.query(Order)
        .filter(Order.store_id == store.id, Order.packed_at.isnot(None))
        .order_by(Order.packed_at.desc())
        .limit(25)
        .all()
    )
    board = []
    for order in jobs:
        board.append(
            {
                "order": order,
                "transit": dispatch_service.transit_state(order),
                "requested": bool(
                    order.latest_dispatch
                    and order.latest_dispatch.status == DISPATCH_REQUESTED
                ),
            }
        )
    return render_template("merchant/dispatch.html", store=store, board=board)
