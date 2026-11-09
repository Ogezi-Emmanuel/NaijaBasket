"""Merchant picking SLA (FR-02): the 15-minute warning/breach escalation."""

from datetime import timedelta

from app.extensions import db
from app.services import escrow as escrow_service
from app.services import sla as sla_service


def test_warning_then_breach_transitions(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()
    escrow_service.hold(order, "REF")  # arms the 15-minute clock
    paid_at = order.paid_at

    # 2 minutes left -> warning window (default threshold is 3 minutes).
    warned = sla_service.evaluate_order(order, now=paid_at + timedelta(minutes=13))
    assert warned["event"] == "warning"
    assert order.sla_breached is False

    # The warning is de-duplicated on repeat sweeps.
    assert sla_service.evaluate_order(order, now=paid_at + timedelta(minutes=13, seconds=30)) is None

    # Past the deadline -> breach.
    breached = sla_service.evaluate_order(order, now=paid_at + timedelta(minutes=16))
    assert breached["event"] == "breach"
    assert order.sla_breached is True
    assert order.status == "sla_breach"


def test_breach_lowers_the_store_rating(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()
    escrow_service.hold(order, "REF")

    store = sample["store"]
    before = store.sla_rating
    sla_service.evaluate_order(order, now=order.paid_at + timedelta(minutes=20))

    assert store.sla_rating == before - 2.0


def test_reconcile_sweeps_breached_orders(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()
    escrow_service.hold(order, "REF")

    # Force the deadline into the past, then let the sweep find it.
    order.sla_deadline = order.paid_at - timedelta(minutes=1)
    db.session.commit()

    result = sla_service.reconcile()
    assert result["breaches"] >= 1


def test_no_transition_inside_the_window(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()
    escrow_service.hold(order, "REF")

    assert sla_service.evaluate_order(order, now=order.paid_at + timedelta(minutes=5)) is None
