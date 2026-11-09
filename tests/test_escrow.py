"""Escrow custody (FR-04): hold, PIN release, lock-out and refunds."""

import pytest

from app.constants import ESCROW_HELD, ESCROW_REFUNDED, ESCROW_RELEASED
from app.extensions import db
from app.services import escrow as escrow_service


def test_hold_then_release_splits_the_funds(app, sample, make_order):
    order = make_order([("tomatoes", 2), ("rice", 1)])
    db.session.commit()

    escrow_service.hold(order, "TEST-REF")
    assert order.payment_escrow_status == ESCROW_HELD
    assert order.status == "paid_escrow"
    assert order.paid_at is not None
    assert order.sla_deadline is not None

    pin = order.delivery_pin
    assert pin and len(pin) == 4

    summary = escrow_service.attempt_pin_release(order, pin)

    assert order.payment_escrow_status == ESCROW_RELEASED
    assert order.status == "delivered"
    assert summary["merchant_payout"] == round(order.gmv_amount - order.commission_amount, 2)
    assert summary["rider_payout"] == round(order.logistics_cost, 2)


def test_hold_is_idempotent_for_duplicate_webhooks(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()

    escrow_service.hold(order, "REF-1")
    escrow_service.hold(order, "REF-2")

    holds = [e for e in order.ledger_entries if e.entry_type == "hold"]
    assert len(holds) == 1


def test_wrong_pin_locks_after_three_attempts(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()
    escrow_service.hold(order, "REF")

    wrong = "0000" if order.delivery_pin != "0000" else "0001"

    # Attempts 1 and 2 report the remaining count.
    for remaining in (2, 1):
        with pytest.raises(escrow_service.EscrowError) as exc:
            escrow_service.attempt_pin_release(order, wrong)
        assert str(remaining) in str(exc.value)

    # Attempt 3 trips the lock.
    with pytest.raises(escrow_service.EscrowError):
        escrow_service.attempt_pin_release(order, wrong)
    assert order.delivery_pin_locked is True

    # Even the correct PIN is refused once locked.
    with pytest.raises(escrow_service.EscrowError):
        escrow_service.attempt_pin_release(order, order.delivery_pin)


def test_release_without_a_hold_is_rejected(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()

    with pytest.raises(escrow_service.EscrowError):
        escrow_service.release(order)


def test_refund_returns_funds_to_the_buyer(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    db.session.commit()

    escrow_service.refund(order, "Buyer cancelled before picking")

    assert order.payment_escrow_status == ESCROW_REFUNDED
    assert order.status == "cancelled"
