"""Abandoned-cart recovery over WhatsApp (FR-06).

A discount token is minted server-side, attached to the cart, and delivered to
the buyer as an approved WhatsApp template through the BSP adapter. The `wa.me`
deep link is minted alongside every send: it is the fallback an agent can resend
by hand when a template is rejected, and it is what the sandbox records.

Inbound delivery/read receipts are applied by `record_status`, so the console can
tell "queued" apart from "the buyer actually read it".

`flask cart-recovery` runs on a schedule (every 5 minutes in production).
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import current_app

from ..constants import WA_STATUS_DELIVERED, WA_STATUS_FAILED, WA_STATUS_RANK, WA_STATUS_READ
from ..extensions import db
from ..integrations import whatsapp as whatsapp_client
from ..integrations.whatsapp import WhatsAppError, normalize_msisdn
from ..models import Cart, utcnow
from ..security import generate_token

__all__ = [
    "abandoned_carts",
    "build_recovery_link",
    "normalize_msisdn",
    "queue_recovery",
    "record_status",
    "run_sweep",
]


def build_recovery_link(cart: Cart, user, *, discount_percent: float, token: str) -> str:
    items = ", ".join(f"{item.quantity}x {item.product.name}" for item in cart.items[:4])
    shortfall = max(0.0, current_app.config["MIN_ORDER_VALUE"] - cart.subtotal)
    message = (
        f"Hi {user.name.split()[0]}, your NaijaBasket basket is still waiting "
        f"({items or 'your saved items'}). "
        f"Subtotal NGN {cart.subtotal:,.0f}"
    )
    if shortfall:
        message += f" - add NGN {shortfall:,.0f} to reach the NGN 15,000 minimum."
    else:
        message += " - ready for checkout."
    message += (
        f" Complete it in 3 taps with code {token} for {discount_percent * 100:.0f}% off "
        "the convenience fee today."
    )
    return (
        f"https://wa.me/{normalize_msisdn(user.phone)}"
        f"?text={quote(message)}"
    )


def queue_recovery(cart: Cart, user, *, commit: bool = True) -> dict:
    """Mint the discount, deliver the template, and record what the BSP said."""
    token = generate_token(8)
    percent = current_app.config["RECOVERY_DISCOUNT_PERCENT"]
    cart.recovery_token = token
    # The token waives a share of the platform convenience fee, not merchant GMV.
    cart.recovery_discount = round(current_app.config["CONVENIENCE_FEE"] * percent, 2)
    cart.recovery_sent_at = utcnow()
    cart.recovery_channel = "whatsapp"

    link = build_recovery_link(cart, user, discount_percent=percent, token=token)
    client = whatsapp_client.get_client()
    try:
        result = client.send_recovery(
            cart, user, link=link, token=token, discount_percent=percent
        )
    except WhatsAppError as exc:
        # One unreachable BSP must not abort the whole sweep.
        result = whatsapp_client.MessageResult(
            ok=False,
            provider=client.name,
            mode="sandbox" if not client.is_live else "live",
            status=WA_STATUS_FAILED,
            message=str(exc),
        )

    cart.recovery_provider = result.provider
    cart.recovery_status = result.status
    cart.recovery_message_id = result.message_id
    cart.recovery_error = None if result.ok else result.message[:255]

    if commit:
        db.session.commit()
    return {
        "cart_id": cart.id,
        "user_id": user.id,
        "phone": normalize_msisdn(user.phone),
        "token": token,
        "discount_percent": percent,
        "link": link,
        "subtotal": cart.subtotal,
        "provider": result.provider,
        "mode": result.mode,
        "status": result.status,
        "message_id": result.message_id,
        "ok": result.ok,
        "message": result.message,
    }


def _receipt_time(timestamp) -> datetime | None:
    """Meta sends epoch seconds as a string."""
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def record_status(message_id: str, status: str, *, timestamp=None) -> Cart | None:
    """Apply a delivery/read receipt to the cart that owns the message.

    Receipts can arrive out of order, so the state machine only moves forward.
    Returns the cart only when the receipt actually advanced the state, so the
    caller can report how many receipts were newly applied.
    """
    if not message_id or status not in WA_STATUS_RANK:
        return None
    cart = (
        db.session.query(Cart)
        .filter(Cart.recovery_message_id == message_id)
        .one_or_none()
    )
    if cart is None:
        return None
    if WA_STATUS_RANK[status] <= WA_STATUS_RANK.get(cart.recovery_status or "", -1):
        return None  # duplicate or stale receipt

    cart.recovery_status = status
    when = _receipt_time(timestamp) or utcnow()
    if status == WA_STATUS_DELIVERED:
        cart.recovery_delivered_at = when
    elif status == WA_STATUS_READ:
        cart.recovery_read_at = when
    db.session.flush()
    return cart


def abandoned_carts(limit: int = 200) -> list[Cart]:
    cutoff = utcnow() - timedelta(minutes=current_app.config["CART_RECOVERY_MINUTES"])
    return (
        db.session.query(Cart)
        .filter(
            Cart.updated_at <= cutoff,
            Cart.recovery_sent_at.is_(None),
        )
        .order_by(Cart.updated_at.asc())
        .limit(limit)
        .all()
    )


def run_sweep(limit: int = 200) -> list[dict]:
    """Returns one entry per recovery message attempted in this sweep."""
    queue = []
    for cart in abandoned_carts(limit=limit):
        if cart.is_empty or cart.user is None:
            continue
        queue.append(queue_recovery(cart, cart.user, commit=False))
    db.session.commit()
    return queue
