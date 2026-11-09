"""Gateway webhooks (signature-verified) - the authoritative payment confirmation.

Registered without CSRF because the caller is a payment provider, not a browser
session; authenticity comes from the HMAC / hash-header check instead.
"""

import logging

from flask import Blueprint, jsonify, request

from ..constants import ESCROW_HELD
from ..extensions import db, limiter
from ..integrations import payments as payments_service
from ..integrations import whatsapp as whatsapp_client
from ..models import Order, SubscriptionPass, money, utcnow

bp = Blueprint("webhooks", __name__)
logger = logging.getLogger(__name__)


def _settle(order: Order, reference: str, amount: float | None) -> None:
    from ..services import escrow as escrow_service
    from ..services import orders as orders_service

    if order.payment_escrow_status == ESCROW_HELD:
        return
    if amount is not None and abs(money(amount) - order.gmv_amount) > 0.5:
        escrow_service.refund(order, f"Webhook amount mismatch ({amount} vs {order.gmv_amount})")
        logger.warning("Escrow amount mismatch for %s", order.order_number)
        return

    escrow_service.hold(order, reference)
    orders_service.reserve_stock(order)
    from ..services import cart as cart_service

    cart = cart_service.cart_for(order.user)
    if cart is not None:
        cart_service.clear(cart)


@bp.post("/paystack")
@limiter.limit("120 per minute")
def paystack():
    gateway = payments_service.get_gateway("paystack")
    ok, reference = gateway.verify_webhook(request.headers, request.get_data())
    if not ok:
        logger.warning("Rejected Paystack webhook (bad signature or key)")
        return jsonify({"status": False, "message": "invalid signature"}), 401
    if not reference:
        return jsonify({"status": True, "message": "ignored event"}), 200

    order = db.session.query(Order).filter(Order.payment_reference == reference).first()
    if order is not None:
        _settle(order, reference, None)
        db.session.commit()
        return jsonify({"status": True, "order": order.order_number}), 200

    pass_row = (
        db.session.query(SubscriptionPass)
        .filter(SubscriptionPass.reference == reference)
        .first()
    )
    if pass_row is not None:
        from ..blueprints.subscription import _activate

        _activate(pass_row)
        return jsonify({"status": True, "pass": pass_row.id}), 200

    return jsonify({"status": True, "message": "unknown reference"}), 200


@bp.post("/flutterwave")
@limiter.limit("120 per minute")
def flutterwave():
    gateway = payments_service.get_gateway("flutterwave")
    ok, reference = gateway.verify_webhook(request.headers, request.get_data())
    if not ok:
        logger.warning("Rejected Flutterwave webhook (bad verif-hash or key)")
        return jsonify({"status": False, "message": "invalid signature"}), 401
    if not reference:
        return jsonify({"status": True, "message": "ignored event"}), 200

    order = db.session.query(Order).filter(Order.payment_reference == reference).first()
    if order is None:
        return jsonify({"status": True, "message": "unknown reference"}), 200

    payload = request.get_json(silent=True) or {}
    amount = (payload.get("data") or {}).get("amount")
    _settle(order, reference, amount)
    db.session.commit()
    return jsonify({"status": True, "order": order.order_number}), 200


@bp.get("/whatsapp")
def whatsapp_verify():
    """Meta's webhook subscription handshake (echoes hub.challenge)."""
    challenge = whatsapp_client.get_client().verify_challenge(request.args)
    if challenge is None:
        logger.warning("Rejected WhatsApp verify handshake (bad mode or token)")
        return jsonify({"status": False, "message": "verification failed"}), 403
    return challenge, 200, {"Content-Type": "text/plain"}


@bp.post("/whatsapp")
@limiter.limit("120 per minute")
def whatsapp_receipts():
    """Delivery + read receipts for FR-06 recovery messages (FR-06)."""
    client = whatsapp_client.get_client()
    if not client.verify_webhook(request.headers, request.get_data()):
        logger.warning("Rejected WhatsApp receipt (bad signature)")
        return jsonify({"status": False, "message": "invalid signature"}), 401

    from ..services import whatsapp as whatsapp_service

    applied = 0
    for receipt in whatsapp_client.decode_receipts(request.get_data()):
        if whatsapp_service.record_status(
            receipt.message_id, receipt.status, timestamp=receipt.timestamp
        ):
            applied += 1
    if applied:
        db.session.commit()
    return jsonify({"status": True, "applied": applied}), 200
