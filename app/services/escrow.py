"""Escrow custody (FR-04) and PIN/QR release with fraud lock-out.

Money movement is append-only: every state change writes a LedgerEntry so the
admin finance view can reconcile without recomputing history.
"""

from datetime import timedelta

from flask import current_app

from ..constants import (
    ESCROW_HELD,
    ESCROW_PENDING,
    ESCROW_REFUNDED,
    ESCROW_RELEASED,
    LEDGER_HOLD,
    LEDGER_MERCHANT_PAYOUT,
    LEDGER_PLATFORM_REVENUE,
    LEDGER_REFUND,
    LEDGER_RIDER_PAYOUT,
    ST_CANCELLED,
    ST_DELIVERED,
)
from ..extensions import db
from ..models import LedgerEntry, Order, money, utcnow


class EscrowError(ValueError):
    pass


def _entry(order: Order, entry_type: str, party: str, amount: float, note: str, reference=None):
    db.session.add(
        LedgerEntry(
            order_id=order.id,
            entry_type=entry_type,
            party=party,
            amount=money(amount),
            note=note,
            reference=reference,
        )
    )


def hold(order: Order, reference: str | None = None) -> Order:
    """Funds captured from the buyer are held by the platform, not the merchant."""
    if order.payment_escrow_status == ESCROW_HELD:
        return order  # idempotent (duplicate webhook)
    if order.payment_escrow_status == ESCROW_RELEASED:
        raise EscrowError("Escrow already released for this order.")

    order.payment_escrow_status = ESCROW_HELD
    order.payment_reference = reference or order.payment_reference
    order.paid_at = utcnow()
    order.sla_deadline = order.paid_at + timedelta(
        minutes=current_app.config["MERCHANT_SLA_MINUTES"]
    )
    if order.status == "pending_payment":
        order.status = "paid_escrow"
    _entry(order, LEDGER_HOLD, "buyer", order.gmv_amount, "Buyer funds captured into platform escrow", reference)
    return order


def release(order: Order, *, actor: str = "rider", note: str = "PIN verified at door") -> dict:
    """Split escrowed funds: merchant payout, rider payout, platform revenue."""
    if order.payment_escrow_status == ESCROW_RELEASED or order.status == ST_DELIVERED:
        raise EscrowError("This order has already been settled.")
    if order.payment_escrow_status != ESCROW_HELD:
        raise EscrowError("Escrow is not funded; nothing to release.")

    _entry(
        order,
        LEDGER_MERCHANT_PAYOUT,
        "merchant",
        order.gmv_amount - order.commission_amount,
        f"Merchant settlement for {order.store.name}",
    )
    _entry(
        order,
        LEDGER_RIDER_PAYOUT,
        "rider",
        order.logistics_cost,
        "3PL courier fare",
    )
    _entry(
        order,
        LEDGER_PLATFORM_REVENUE,
        "platform",
        order.gross_revenue - order.logistics_cost,
        "Commission, convenience fee and subscription allocation net of courier fare",
    )

    order.payment_escrow_status = ESCROW_RELEASED
    order.status = ST_DELIVERED
    order.delivered_at = utcnow()
    order.delivery_pin_attempts = 0

    job = order.latest_dispatch
    if job is not None:
        job.status = "delivered"
        job.delivered_at = order.delivered_at

    return {
        "order_number": order.order_number,
        "merchant_payout": money(order.gmv_amount - order.commission_amount),
        "rider_payout": money(order.logistics_cost),
        "platform_net": money(order.gross_revenue - order.logistics_cost),
        "released_by": actor,
        "note": note,
    }


def refund(order: Order, reason: str) -> Order:
    if order.payment_escrow_status == ESCROW_RELEASED:
        raise EscrowError("Funds were already released; raise a dispute instead.")
    if order.payment_escrow_status == ESCROW_REFUNDED:
        return order
    _entry(order, LEDGER_REFUND, "buyer", order.gmv_amount, reason)
    order.payment_escrow_status = ESCROW_REFUNDED
    order.status = ST_CANCELLED
    return order


def attempt_pin_release(order: Order, pin: str, *, actor: str = "rider") -> dict:
    """Verify the buyer's 4-digit code. Lock out after N failed attempts."""
    if order.delivery_pin_locked:
        raise EscrowError("PIN entry locked after repeated failures. Contact support.")
    if not pin or len(pin.strip()) != 4 or not pin.strip().isdigit():
        raise EscrowError("Enter the 4-digit delivery PIN.")

    if not order.verify_delivery_pin(pin.strip()):
        order.delivery_pin_attempts = (order.delivery_pin_attempts or 0) + 1
        remaining = current_app.config["ESCROW_PIN_MAX_ATTEMPTS"] - order.delivery_pin_attempts
        if remaining <= 0:
            order.delivery_pin_locked = True
            db.session.flush()
            raise EscrowError("Incorrect PIN. Entry locked - admin verification required.")
        db.session.flush()
        raise EscrowError(f"Incorrect PIN. {remaining} attempt(s) left before lock-out.")

    order.delivery_pin_attempts = 0
    return release(order, actor=actor, note="4-digit escrow code verified at delivery")


def ledger_for(order: Order) -> list[dict]:
    return [
        {
            "entry_type": entry.entry_type,
            "party": entry.party,
            "amount": money(entry.amount),
            "note": entry.note,
            "at": entry.created_at,
        }
        for entry in order.ledger_entries
    ]
