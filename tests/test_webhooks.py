"""Gateway webhooks: signature verification and escrow settlement."""

import hashlib
import hmac
import json

from app.constants import ESCROW_HELD, ESCROW_REFUNDED
from app.extensions import db
from app.models import Order


def _json_headers(**extra):
    return {"Content-Type": "application/json", **extra}


def test_paystack_webhook_rejects_a_bad_signature(app, client, sample, make_order):
    app.config["PAYSTACK_SECRET_KEY"] = "sk_test_secret"

    order = make_order([("tomatoes", 2)])
    order.payment_reference = "PS-REF-1"
    db.session.commit()

    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "PS-REF-1"}}
    ).encode()

    rejected = client.post(
        "/webhooks/paystack", data=body,
        headers=_json_headers(**{"x-paystack-signature": "deadbeef"}),
    )
    assert rejected.status_code == 401
    assert db.session.get(Order, order.id).payment_escrow_status != ESCROW_HELD


def test_paystack_webhook_settles_escrow_on_a_valid_signature(app, client, sample, make_order):
    app.config["PAYSTACK_SECRET_KEY"] = "sk_test_secret"

    order = make_order([("tomatoes", 2)])
    order.payment_reference = "PS-REF-2"
    db.session.commit()

    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "PS-REF-2"}}
    ).encode()
    signature = hmac.new(b"sk_test_secret", body, hashlib.sha512).hexdigest()

    accepted = client.post(
        "/webhooks/paystack", data=body,
        headers=_json_headers(**{"x-paystack-signature": signature}),
    )
    assert accepted.status_code == 200
    assert db.session.get(Order, order.id).payment_escrow_status == ESCROW_HELD


def test_flutterwave_webhook_uses_the_verif_hash(app, client, sample, make_order):
    app.config["FLUTTERWAVE_WEBHOOK_HASH"] = "flw-hash"

    order = make_order([("tomatoes", 2)])
    order.payment_reference = "FLW-REF-1"
    db.session.commit()

    body = json.dumps(
        {"data": {"tx_ref": "FLW-REF-1", "amount": order.gmv_amount}}
    ).encode()

    rejected = client.post(
        "/webhooks/flutterwave", data=body,
        headers=_json_headers(**{"verif-hash": "wrong"}),
    )
    assert rejected.status_code == 401

    accepted = client.post(
        "/webhooks/flutterwave", data=body,
        headers=_json_headers(**{"verif-hash": "flw-hash"}),
    )
    assert accepted.status_code == 200
    assert db.session.get(Order, order.id).payment_escrow_status == ESCROW_HELD


def test_webhook_amount_mismatch_triggers_a_refund(app, client, sample, make_order):
    app.config["FLUTTERWAVE_WEBHOOK_HASH"] = "flw-hash"

    order = make_order([("tomatoes", 2)])
    order.payment_reference = "FLW-REF-2"
    db.session.commit()

    body = json.dumps({"data": {"tx_ref": "FLW-REF-2", "amount": 100.0}}).encode()
    response = client.post(
        "/webhooks/flutterwave", data=body,
        headers=_json_headers(**{"verif-hash": "flw-hash"}),
    )
    assert response.status_code == 200
    assert db.session.get(Order, order.id).payment_escrow_status == ESCROW_REFUNDED
