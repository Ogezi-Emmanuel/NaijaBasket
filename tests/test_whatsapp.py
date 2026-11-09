"""WhatsApp Business delivery (FR-06): sandbox sends and inbound receipts."""

import hashlib
import hmac
import json

from app.constants import (
    WA_STATUS_DELIVERED,
    WA_STATUS_FAILED,
    WA_STATUS_QUEUED,
    WA_STATUS_SENT,
)
from app.extensions import db
from app.integrations import whatsapp as whatsapp_client
from app.integrations.whatsapp import normalize_msisdn
from app.models import Cart
from app.services import whatsapp as whatsapp_service

SECRET = "wa-app-secret"
VERIFY_TOKEN = "wa-verify-token"


def _receipt_body(message_id, status, timestamp="1710000000"):
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "1234",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "statuses": [
                                    {
                                        "id": message_id,
                                        "status": status,
                                        "timestamp": timestamp,
                                        "recipient_id": "2348030000001",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def _signed_headers(body):
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def _recoverable_cart(fill_cart, user):
    """A cart with a recovery message already recorded against it."""
    cart = fill_cart([("tomatoes", 2), ("rice", 1)], user=user)
    whatsapp_service.queue_recovery(cart, user)
    return cart


# --- adapter ----------------------------------------------------------------
def test_normalize_msisdn_accepts_nigerian_formats():
    assert normalize_msisdn("08030000001") == "2348030000001"
    assert normalize_msisdn("+234 803 000 0001") == "2348030000001"
    assert normalize_msisdn("8030000001") == "2348030000001"
    assert normalize_msisdn("") == ""


def test_cloud_api_is_live_only_when_credentials_are_configured(app):
    client = whatsapp_client.get_client()
    assert client.is_live is False

    app.config["WHATSAPP_TOKEN"] = "token"
    app.config["WHATSAPP_PHONE_NUMBER_ID"] = "123456"
    assert whatsapp_client.get_client().is_live is True

    app.config["WHATSAPP_TOKEN"] = ""
    app.config["WHATSAPP_PHONE_NUMBER_ID"] = ""
    assert whatsapp_client.get_client().is_live is False


def test_modes_report_the_configured_channel(app):
    modes = whatsapp_client.whatsapp_modes()
    assert modes["cloud_api"]["mode"] == "sandbox"  # no credentials in tests
    assert modes["cloud_api"]["live"] is False


def test_receipt_parsing_flattens_metas_envelope():
    receipts = whatsapp_client.decode_receipts(_receipt_body("wamid.A", WA_STATUS_DELIVERED))
    assert len(receipts) == 1
    assert receipts[0].message_id == "wamid.A"
    assert receipts[0].status == WA_STATUS_DELIVERED

    assert whatsapp_client.decode_receipts(b"not json") == []


# --- outbound ---------------------------------------------------------------
def test_sandbox_records_the_send_without_a_bsp_account(app, fill_cart, sample):
    cart = fill_cart([("tomatoes", 2), ("rice", 1)])
    result = whatsapp_service.queue_recovery(cart, sample["users"]["consumer"])

    assert result["ok"] is True
    assert result["mode"] == "sandbox"
    assert result["status"] == WA_STATUS_QUEUED
    assert result["message_id"] == f"SBX-WA-{cart.id}"
    assert result["link"].startswith("https://wa.me/234")
    assert result["discount_percent"] > 0

    stored = db.session.get(Cart, cart.id)
    assert stored.recovery_channel == "whatsapp"
    assert stored.recovery_provider == "cloud_api"
    assert stored.recovery_status == WA_STATUS_QUEUED
    assert stored.recovery_status_label == "Queued (sandbox)"


def test_sweep_only_touches_idle_baskets(app, fill_cart, sample):
    from datetime import timedelta

    from app.models import utcnow

    idle = fill_cart([("tomatoes", 2), ("rice", 1)])
    fresh = fill_cart([("soap", 4)], user=sample["users"]["admin"])
    idle.updated_at = utcnow() - timedelta(minutes=90)
    db.session.commit()

    swept = whatsapp_service.run_sweep()
    swept_ids = {entry["cart_id"] for entry in swept}
    assert idle.id in swept_ids
    assert fresh.id not in swept_ids


# --- webhook handshake ------------------------------------------------------
def test_verification_handshake_echoes_the_challenge(app, client):
    app.config["WHATSAPP_VERIFY_TOKEN"] = VERIFY_TOKEN

    ok = client.get(
        "/webhooks/whatsapp",
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "abc123",
        },
    )
    assert ok.status_code == 200
    assert ok.get_data(as_text=True) == "abc123"


def test_verification_handshake_rejects_a_wrong_token(app, client):
    app.config["WHATSAPP_VERIFY_TOKEN"] = VERIFY_TOKEN

    bad = client.get(
        "/webhooks/whatsapp",
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": "nope",
            "hub.challenge": "abc123",
        },
    )
    assert bad.status_code == 403


# --- inbound receipts -------------------------------------------------------
def test_receipt_route_rejects_an_unsigned_payload(app, client, fill_cart, sample):
    app.config["WHATSAPP_APP_SECRET"] = SECRET
    cart = _recoverable_cart(fill_cart, sample["users"]["consumer"])

    body = _receipt_body(cart.recovery_message_id, WA_STATUS_DELIVERED)
    rejected = client.post(
        "/webhooks/whatsapp", data=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
    )

    assert rejected.status_code == 401
    assert db.session.get(Cart, cart.id).recovery_status == WA_STATUS_QUEUED


def test_receipt_route_moves_the_state_machine_forward_only(app, client, fill_cart, sample):
    app.config["WHATSAPP_APP_SECRET"] = SECRET
    cart = _recoverable_cart(fill_cart, sample["users"]["consumer"])
    message_id = cart.recovery_message_id

    # A "sent" receipt arrives first.
    sent = client.post(
        "/webhooks/whatsapp", data=_receipt_body(message_id, WA_STATUS_SENT),
        headers=_signed_headers(_receipt_body(message_id, WA_STATUS_SENT)),
    )
    assert sent.status_code == 200
    assert sent.get_json()["applied"] == 1
    assert db.session.get(Cart, cart.id).recovery_status == WA_STATUS_SENT

    # Then a stale duplicate of the earlier state: no change, nothing applied.
    duplicate_body = _receipt_body(message_id, WA_STATUS_QUEUED)
    duplicate = client.post(
        "/webhooks/whatsapp", data=duplicate_body, headers=_signed_headers(duplicate_body),
    )
    assert duplicate.get_json()["applied"] == 0

    # "delivered" and then "read" both advance.
    for status in (WA_STATUS_DELIVERED, "read"):
        body = _receipt_body(message_id, status)
        response = client.post(
            "/webhooks/whatsapp", data=body, headers=_signed_headers(body),
        )
        assert response.get_json()["applied"] == 1

    stored = db.session.get(Cart, cart.id)
    assert stored.recovery_status == "read"
    assert stored.recovery_delivered_at is not None
    assert stored.recovery_read_at is not None


def test_failed_is_terminal(app, client, fill_cart, sample):
    app.config["WHATSAPP_APP_SECRET"] = SECRET
    cart = _recoverable_cart(fill_cart, sample["users"]["consumer"])
    message_id = cart.recovery_message_id

    failed_body = _receipt_body(message_id, WA_STATUS_FAILED)
    client.post("/webhooks/whatsapp", data=failed_body, headers=_signed_headers(failed_body))
    assert db.session.get(Cart, cart.id).recovery_status == WA_STATUS_FAILED

    later_body = _receipt_body(message_id, "read")
    later = client.post(
        "/webhooks/whatsapp", data=later_body, headers=_signed_headers(later_body),
    )
    assert later.get_json()["applied"] == 0
    assert db.session.get(Cart, cart.id).recovery_status == WA_STATUS_FAILED


def test_receipt_for_an_unknown_message_is_ignored(app, client):
    app.config["WHATSAPP_APP_SECRET"] = SECRET

    body = _receipt_body("wamid.UNKNOWN", WA_STATUS_DELIVERED)
    response = client.post(
        "/webhooks/whatsapp", data=body, headers=_signed_headers(body),
    )
    assert response.status_code == 200
    assert response.get_json()["applied"] == 0


def test_clearing_a_cart_resets_the_receipt_state(app, fill_cart, sample):
    from app.services import cart as cart_service

    cart = _recoverable_cart(fill_cart, sample["users"]["consumer"])
    cart_service.clear(cart)
    db.session.commit()

    stored = db.session.get(Cart, cart.id)
    assert stored.recovery_provider is None
    assert stored.recovery_message_id is None
    assert stored.recovery_status is None
    assert stored.recovery_status_label == "Not sent"
