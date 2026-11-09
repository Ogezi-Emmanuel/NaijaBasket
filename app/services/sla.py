"""Merchant SLA enforcement (FR-02) and compliance analytics.

The minute threshold is policy (>15 min => breach) even though the offer alerts
merchants during the final warning window.
"""

from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from ..constants import SLA_TRACKED_STATUSES, ST_PAID_ESCROW, ST_SLA_BREACH
from ..extensions import db
from ..models import Order, PartnerStore, SlaEvent, utcnow


def _minutes_between(start, end) -> float:
    if not start or not end:
        return 0.0
    return round((end - start).total_seconds() / 60.0, 2)


def warning_seconds() -> int:
    return current_app.config["SLA_WARNING_MINUTES"] * 60


def evaluate_order(order: Order, now=None) -> dict | None:
    """Returns a dict describing a state transition, or None if nothing changed."""
    now = now or utcnow()
    if order.status not in SLA_TRACKED_STATUSES or not order.sla_deadline:
        return None

    remaining = (order.sla_deadline - now).total_seconds()
    elapsed = _minutes_between(order.paid_at, now)

    if remaining < 0:
        order.sla_breached = True
        order.status = ST_SLA_BREACH
        db.session.add(
            SlaEvent(
                order_id=order.id,
                store_id=order.store_id,
                event_type="breach",
                minutes_elapsed=elapsed,
                detail=f"Order not packed within {current_app.config['MERCHANT_SLA_MINUTES']} minutes",
            )
        )
        store = db.session.get(PartnerStore, order.store_id)
        if store is not None:
            store.sla_rating = max(0.0, round(store.sla_rating - 2.0, 2))
        db.session.flush()
        return {"order": order, "event": "breach", "minutes_elapsed": elapsed}

    if remaining <= warning_seconds():
        already = (
            db.session.query(SlaEvent.id)
            .filter(SlaEvent.order_id == order.id, SlaEvent.event_type == "warning")
            .first()
        )
        if not already:
            db.session.add(
                SlaEvent(
                    order_id=order.id,
                    store_id=order.store_id,
                    event_type="warning",
                    minutes_elapsed=elapsed,
                    detail=f"Under {current_app.config['SLA_WARNING_MINUTES']} minutes left to pack",
                )
            )
            db.session.flush()
            return {"order": order, "event": "warning", "minutes_elapsed": elapsed}

    return None


def reconcile(store_id: int | None = None) -> dict:
    """Sweep live orders and escalate anything past its picking deadline.

    Called by the `flask sla-check` task (cron / systemd timer in production).
    """
    query = db.session.query(Order).filter(Order.status.in_(SLA_TRACKED_STATUSES))
    if store_id:
        query = query.filter(Order.store_id == store_id)

    transitions = []
    for order in query.all():
        change = evaluate_order(order)
        if change:
            transitions.append(
                {
                    "order_number": change["order"].order_number,
                    "event": change["event"],
                    "minutes_elapsed": change["minutes_elapsed"],
                }
            )
    db.session.commit()
    breached = [t for t in transitions if t["event"] == "breach"]
    return {
        "evaluated": len(transitions),
        "breaches": len(breached),
        "warnings": len([t for t in transitions if t["event"] == "warning"]),
        "transitions": transitions,
    }


def record_met(order: Order) -> None:
    """Log a successful pick when the merchant marks the order packed in time."""
    elapsed = _minutes_between(order.paid_at, order.packed_at or utcnow())
    db.session.add(
        SlaEvent(
            order_id=order.id,
            store_id=order.store_id,
            event_type="met",
            minutes_elapsed=elapsed,
            detail=f"Packed in {elapsed} minutes",
        )
    )


# ---------------------------------------------------------------------------
# Compliance reporting (/admin/partners)
# ---------------------------------------------------------------------------
def store_compliance(store: PartnerStore) -> dict:
    packed_rows = (
        db.session.query(Order.paid_at, Order.packed_at)
        .filter(
            Order.store_id == store.id,
            Order.packed_at.isnot(None),
            Order.paid_at.isnot(None),
        )
        .all()
    )
    packed_count = len(packed_rows)
    pick_minutes = [_minutes_between(paid, packed) for paid, packed in packed_rows]
    avg_pick = round(sum(pick_minutes) / packed_count, 2) if packed_count else None

    breached = (
        db.session.query(func.count(Order.id))
        .filter(Order.store_id == store.id, Order.status == ST_SLA_BREACH)
        .scalar()
        or 0
    )
    completed = packed_count
    gmv = (
        db.session.query(func.coalesce(func.sum(Order.gmv_amount), 0.0))
        .filter(Order.store_id == store.id, Order.status != "cancelled")
        .scalar()
        or 0.0
    )

    return {
        "store": store,
        "orders_completed": completed,
        "orders_breached": breached,
        "breach_rate": round(breached / completed * 100, 1) if completed else 0.0,
        "avg_pick_minutes": avg_pick,
        "sla_rating": round(store.sla_rating, 1),
        "gmv": round(float(gmv), 2),
        "target_minutes": store.sla_target_minutes,
    }


def platform_sla_overview() -> dict:
    total = (
        db.session.query(func.count(Order.id))
        .filter(Order.paid_at.isnot(None))
        .scalar()
        or 0
    )
    met = (
        db.session.query(func.count(Order.id))
        .filter(Order.packed_at.isnot(None), Order.sla_breached.is_(False))
        .scalar()
        or 0
    )
    breached = (
        db.session.query(func.count(Order.id))
        .filter(Order.sla_breached.is_(True))
        .scalar()
        or 0
    )
    return {
        "orders_paid": total,
        "orders_met": met,
        "orders_breached": breached,
        "compliance_rate": round(met / total * 100, 1) if total else 100.0,
        "target_minutes": current_app.config["MERCHANT_SLA_MINUTES"],
        "transit_target_minutes": current_app.config["TRANSIT_SLA_MINUTES"],
    }


def transit_deadline(dispatched_at):
    return dispatched_at + timedelta(minutes=current_app.config["TRANSIT_SLA_MINUTES"])
