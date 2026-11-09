"""Admin governance: revenue analytics, partner SLA compliance, escrow finance."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func

from ..constants import (
    CATEGORY_LABELS,
    LEDGER_MERCHANT_PAYOUT,
    LEDGER_RIDER_PAYOUT,
    ORDER_STATUS_LABELS,
    WA_STATUS_LABELS,
)
from ..extensions import db
from ..integrations import dispatch as dispatch_service
from ..integrations import payments as payments_service
from ..integrations import whatsapp as whatsapp_bsp
from ..models import LedgerEntry, Order, OrderItem, PartnerStore, Product, User, money, utcnow
from ..security import admin_required
from ..services import economics as economics_service
from ..services import sla as sla_service

bp = Blueprint("admin", __name__)


@bp.before_request
@admin_required
def _guard():
    return None


def revenue_rollup():
    row = db.session.query(
        func.count(Order.id),
        func.coalesce(func.sum(Order.gmv_amount), 0.0),
        func.coalesce(func.sum(Order.gross_revenue), 0.0),
        func.coalesce(func.sum(Order.net_contribution_margin), 0.0),
        func.coalesce(func.sum(Order.commission_amount), 0.0),
        func.coalesce(func.sum(Order.convenience_fee), 0.0),
        func.coalesce(func.sum(Order.subscription_allocation), 0.0),
        func.coalesce(func.sum(Order.gateway_fee), 0.0),
        func.coalesce(func.sum(Order.logistics_cost), 0.0),
    ).filter(Order.status != "cancelled").first()

    orders, gmv, gross, margin, commission, fee, allocation, gateway, logistics = row
    orders = int(orders or 0)
    return {
        "orders": orders,
        "gmv": money(gmv),
        "gross_revenue": money(gross),
        "net_margin": money(margin),
        "commission": money(commission),
        "convenience_fee": money(fee),
        "subscription_allocation": money(allocation),
        "gateway_fee": money(gateway),
        "logistics": money(logistics),
        "aov": money(gmv / orders) if orders else 0.0,
        "margin_pct": round(margin / gmv * 100, 2) if gmv else 0.0,
        "blended_take_rate_pct": round(commission / gmv * 100, 2) if gmv else 0.0,
    }


@bp.get("/")
def index():
    return redirect(url_for("admin.dashboard"))


@bp.get("/dashboard")
def dashboard():
    status_rows = db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all()
    gateway_rows = (
        db.session.query(Order.payment_gateway, func.count(Order.id), func.coalesce(func.sum(Order.gmv_amount), 0.0))
        .group_by(Order.payment_gateway)
        .all()
    )
    from ..models import DispatchJob

    courier_mix = (
        db.session.query(
            DispatchJob.provider,
            DispatchJob.mode,
            func.count(DispatchJob.id),
            func.coalesce(func.sum(DispatchJob.quote_amount), 0.0),
        )
        .group_by(DispatchJob.provider, DispatchJob.mode)
        .all()
    )
    category_rows = (
        db.session.query(
            OrderItem.category,
            func.coalesce(func.sum(OrderItem.unit_price * OrderItem.quantity), 0.0),
            func.coalesce(func.sum(OrderItem.quantity), 0),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status != "cancelled")
        .group_by(OrderItem.category)
        .all()
    )
    total_category_gmv = sum(float(row[1]) for row in category_rows) or 1.0
    category_mix = [
        {
            "category": category,
            "label": CATEGORY_LABELS.get(category, category),
            "gmv": money(gmv),
            "units": int(units or 0),
            "share_pct": round(float(gmv) / total_category_gmv * 100, 1),
        }
        for category, gmv, units in sorted(category_rows, key=lambda r: float(r[1]), reverse=True)
    ]

    top_stores = []
    for store in db.session.query(PartnerStore).all():
        compliance = sla_service.store_compliance(store)
        if compliance["orders_completed"] or compliance["gmv"]:
            top_stores.append(compliance)
    top_stores.sort(key=lambda row: row["gmv"], reverse=True)

    recent = (
        db.session.query(Order).order_by(Order.created_at.desc()).limit(12).all()
    )

    return render_template(
        "admin/dashboard.html",
        kpis=revenue_rollup(),
        status_breakdown=[
            {"status": status, "label": ORDER_STATUS_LABELS.get(status, status), "count": count}
            for status, count in sorted(status_rows, key=lambda r: r[1], reverse=True)
        ],
        gateway_mix=[
            {"gateway": gateway or "unset", "count": count, "gmv": money(gmv)}
            for gateway, count, gmv in gateway_rows
        ],
        courier_mix=[
            {"provider": provider, "mode": mode, "count": count, "fees": money(fees)}
            for provider, mode, count, fees in courier_mix
        ],
        category_mix=category_mix,
        top_stores=top_stores,
        recent_orders=recent,
        sla=sla_service.platform_sla_overview(),
        benchmark=economics_service.benchmark_assertion(),
        gateway_modes=payments_service.gateway_modes(),
        courier_modes=dispatch_service.courier_modes(),
        whatsapp_modes=whatsapp_bsp.whatsapp_modes(),
        abandoned=_abandoned_count(),
        recovery_mix=_recovery_rollup(),
    )


def _abandoned_count() -> int:
    from ..models import Cart

    return (
        db.session.query(func.count(Cart.id))
        .filter(Cart.recovery_sent_at.is_(None))
        .scalar()
        or 0
    )


def _recovery_rollup() -> list[dict]:
    """FR-06 receipt funnel: how far each recovery message actually got."""
    from ..models import Cart

    rows = (
        db.session.query(Cart.recovery_status, func.count(Cart.id))
        .filter(Cart.recovery_status.isnot(None))
        .group_by(Cart.recovery_status)
        .all()
    )
    return [
        {"status": status, "label": WA_STATUS_LABELS.get(status, status), "count": count}
        for status, count in rows
    ]


@bp.get("/partners")
def partners():
    rows = [sla_service.store_compliance(store) for store in db.session.query(PartnerStore).order_by(PartnerStore.name)]
    return render_template(
        "admin/partners.html",
        rows=rows,
        stores=[row["store"] for row in rows],
        product_counts={
            store.id: db.session.query(func.count(Product.id))
            .filter(Product.store_id == store.id)
            .scalar()
            for store in db.session.query(PartnerStore)
        },
        overview=sla_service.platform_sla_overview(),
    )


@bp.post("/partners/<int:store_id>")
def update_partner(store_id):
    store = db.session.get(PartnerStore, store_id)
    if store is None:
        abort(404)

    if request.form.get("toggle_active"):
        store.is_active = not store.is_active
        flash(f"{store.name} is now {'online' if store.is_active else 'offline'}.", "success")

    rate = request.form.get("commission_rate", type=float)
    if rate is not None:
        # Accept either 5.5 (%) or 0.055 (fraction); clamp to the contractual band.
        fraction = rate / 100 if rate > 1 else rate
        clamped = economics_service.clamp_take_rate(fraction)
        if abs(clamped - fraction) > 1e-9:
            flash(
                f"Take rate clamped to the contractual band "
                f"({store.commission_rate * 100:.1f}% -> {clamped * 100:.1f}%).",
                "warning",
            )
        store.commission_rate = clamped

    sla_target = request.form.get("sla_target_minutes", type=int)
    if sla_target:
        store.sla_target_minutes = max(5, sla_target)

    db.session.commit()
    flash(f"{store.name} updated.", "success")
    return redirect(url_for("admin.partners"))


@bp.post("/partners/<int:store_id>/recompute-rating")
def recompute_rating(store_id):
    store = db.session.get(PartnerStore, store_id)
    if store is None:
        abort(404)
    compliance = sla_service.store_compliance(store)
    store.sla_rating = round(max(0.0, 100.0 - compliance["breach_rate"]), 2)
    db.session.commit()
    flash(f"{store.name} SLA rating recomputed to {store.sla_rating}.", "success")
    return redirect(url_for("admin.partners"))


@bp.get("/finance")
def finance():
    escrow_rows = (
        db.session.query(Order.payment_escrow_status, func.count(Order.id), func.coalesce(func.sum(Order.gmv_amount), 0.0))
        .group_by(Order.payment_escrow_status)
        .all()
    )
    party_rows = (
        db.session.query(
            LedgerEntry.party,
            LedgerEntry.entry_type,
            func.count(LedgerEntry.id),
            func.coalesce(func.sum(LedgerEntry.amount), 0.0),
        )
        .group_by(LedgerEntry.party, LedgerEntry.entry_type)
        .all()
    )
    settlements = (
        db.session.query(Order)
        .filter(Order.payment_escrow_status.in_(("held", "released", "refunded")))
        .order_by(Order.created_at.desc())
        .limit(30)
        .all()
    )
    held = (
        db.session.query(func.coalesce(func.sum(Order.gmv_amount), 0.0))
        .filter(Order.payment_escrow_status == "held")
        .scalar()
        or 0.0
    )
    merchant_payable = (
        db.session.query(func.coalesce(func.sum(Order.gmv_amount - Order.commission_amount), 0.0))
        .filter(Order.payment_escrow_status == "released")
        .scalar()
        or 0.0
    )
    rider_payable = (
        db.session.query(func.coalesce(func.sum(Order.logistics_cost), 0.0))
        .filter(Order.payment_escrow_status == "released")
        .scalar()
        or 0.0
    )

    return render_template(
        "admin/finance.html",
        escrow_breakdown=[
            {"status": status, "count": count, "gmv": money(gmv)}
            for status, count, gmv in escrow_rows
        ],
        ledger_rows=[
            {"party": party, "entry_type": entry_type, "count": count, "total": money(total)}
            for party, entry_type, count, total in sorted(party_rows, key=lambda r: (r[0], r[1]))
        ],
        settlements=settlements,
        totals={
            "held": money(held),
            "merchant_paid": money(merchant_payable),
            "rider_paid": money(rider_payable),
            "platform_revenue": money(revenue_rollup()["gross_revenue"]),
            "net_margin": money(revenue_rollup()["net_margin"]),
        },
    )


@bp.get("/orders")
def orders():
    status = request.args.get("status")
    query = db.session.query(Order).order_by(Order.created_at.desc())
    if status:
        query = query.filter(Order.status == status)
    return render_template(
        "admin/orders.html",
        orders=query.limit(100).all(),
        status=status,
        status_labels=ORDER_STATUS_LABELS,
    )


@bp.post("/users/<int:user_id>/role")
def set_role(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    role = request.form.get("user_type")
    if role not in ("consumer", "merchant", "admin"):
        flash("Unknown role.", "danger")
        return redirect(request.referrer or url_for("admin.dashboard"))

    user.user_type = role
    if role == "merchant" and not user.store_id:
        first_store = db.session.query(PartnerStore).order_by(PartnerStore.id).first()
        user.store_id = first_store.id if first_store else None
    db.session.commit()
    flash(f"{user.email} is now a {role}.", "success")
    return redirect(request.referrer or url_for("admin.dashboard"))


@bp.post("/maintenance/sla-sweep")
def sla_sweep():
    result = sla_service.reconcile()
    flash(
        f"SLA sweep complete: {result['warnings']} warning(s), {result['breaches']} breach(es).",
        "success" if not result["breaches"] else "warning",
    )
    return redirect(url_for("admin.dashboard"))


@bp.post("/maintenance/cart-recovery")
def cart_recovery():
    from ..services import whatsapp as whatsapp_service

    sent = whatsapp_service.run_sweep()
    failed = sum(1 for entry in sent if not entry["ok"])
    if failed:
        flash(
            f"{len(sent)} WhatsApp recovery message(s) attempted, {failed} failed "
            "at the BSP - see the recovery funnel below.",
            "warning",
        )
    else:
        flash(f"{len(sent)} WhatsApp recovery message(s) dispatched for idle baskets.", "success")
    return redirect(url_for("admin.dashboard"))
