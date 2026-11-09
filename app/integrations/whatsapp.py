"""WhatsApp Business delivery adapter (FR-06).

`send_recovery` posts an approved message template through the Meta WhatsApp
Cloud API whenever a token and phone-number id are configured. Without them the
adapter degrades to the deterministic sandbox, which records the send so the
whole recovery flow stays exercisable in dev, CI and demos. `mode` always
reports which one actually ran.

Inbound delivery/read receipts are authenticated with the app secret
(`X-Hub-Signature-256`) and applied by `parse_status_receipts`.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field

from flask import current_app

from ..constants import (
    WA_PROVIDER_CLOUD,
    WA_PROVIDER_SANDBOX,
    WA_STATUS_FAILED,
    WA_STATUS_QUEUED,
    WA_STATUS_SENT,
)


class WhatsAppError(RuntimeError):
    pass


def normalize_msisdn(phone: str) -> str:
    """Nigerian numbers -> WhatsApp international format (234XXXXXXXXXX)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("234"):
        return digits
    if digits.startswith("0"):
        return "234" + digits[1:]
    if len(digits) == 10:
        return "234" + digits
    return digits


@dataclass
class MessageResult:
    ok: bool
    provider: str
    mode: str
    message_id: str | None = None
    status: str = WA_STATUS_QUEUED
    message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class StatusReceipt:
    message_id: str
    status: str
    timestamp: str | None = None
    recipient: str | None = None
    error: str | None = None


class BaseWhatsAppClient:
    name = WA_PROVIDER_SANDBOX
    label = "Sandbox"

    def __init__(self, config=None):
        self.config = config or current_app.config

    @property
    def is_live(self) -> bool:
        return False

    # -- interface ---------------------------------------------------------
    def send_recovery(self, cart, user, *, link: str, token: str, **context) -> MessageResult:
        raise NotImplementedError

    def verify_challenge(self, params) -> str | None:
        """Answer Meta's webhook subscription handshake."""
        return None

    def verify_webhook(self, headers, raw_body: bytes) -> bool:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------
    def _post(self, url: str, *, json_body=None, headers=None):
        import requests

        try:
            return requests.post(
                url,
                json=json_body,
                headers=headers,
                timeout=self.config["HTTP_TIMEOUT_SECONDS"],
            )
        except Exception as exc:  # network / DNS / TLS
            raise WhatsAppError(f"{self.label} is unreachable: {exc}") from exc

    def _template_components(self, cart, user, *, link, token, discount_percent) -> list:
        """Body parameters for the approved `abandoned_cart_recovery` template."""
        first_name = (user.name or "there").split()[0]
        items = ", ".join(
            f"{item.quantity}x {item.product.name}" for item in cart.items[:4]
        ) or "your saved items"
        return [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": first_name},
                    {"type": "text", "text": items},
                    {"type": "text", "text": f"NGN {cart.subtotal:,.0f}"},
                    {"type": "text", "text": f"{discount_percent * 100:.0f}%"},
                    {"type": "text", "text": token},
                    {"type": "text", "text": link},
                ],
            }
        ]


class SandboxWhatsAppClient(BaseWhatsAppClient):
    """Deterministic BSP used when no Cloud API credentials are configured."""

    name = WA_PROVIDER_SANDBOX
    label = "Sandbox BSP"

    def send_recovery(self, cart, user, *, link: str, token: str, **context) -> MessageResult:
        return MessageResult(
            ok=True,
            provider=self.name,
            mode="sandbox",
            message_id=f"SBX-WA-{cart.id}",
            status=WA_STATUS_QUEUED,
            message="Sandbox BSP accepted the template; nothing left the building.",
            raw={"simulated": True, "to": normalize_msisdn(user.phone), "link": link},
        )

    def verify_challenge(self, params) -> str | None:
        return None

    def verify_webhook(self, headers, raw_body: bytes) -> bool:
        # With no app secret configured there is nothing to authenticate against.
        # The receipt route therefore stays closed in sandbox mode rather than
        # accepting unverified writes.
        return False


class CloudApiWhatsAppClient(BaseWhatsAppClient):
    """Meta WhatsApp Cloud API."""

    name = WA_PROVIDER_CLOUD
    label = "WhatsApp Cloud API"

    @property
    def is_live(self) -> bool:
        return bool(self.config["WHATSAPP_TOKEN"] and self.config["WHATSAPP_PHONE_NUMBER_ID"])

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config['WHATSAPP_TOKEN']}",
            "Content-Type": "application/json",
        }

    def send_recovery(self, cart, user, *, link: str, token: str, **context) -> MessageResult:
        to = normalize_msisdn(user.phone)
        if not self.is_live:
            fallback = SandboxWhatsAppClient(self.config).send_recovery(
                cart, user, link=link, token=token, **context
            )
            fallback.provider = self.name
            fallback.message = "Cloud API credentials not set: sandbox BSP used."
            return fallback
        if not to:
            return MessageResult(
                ok=False,
                provider=self.name,
                mode="live",
                status=WA_STATUS_FAILED,
                message="Buyer has no usable WhatsApp number.",
            )

        discount_percent = context.get("discount_percent", 0.0)
        response = self._post(
            f"{self.config['WHATSAPP_API_BASE']}/{self.config['WHATSAPP_PHONE_NUMBER_ID']}/messages",
            json_body={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "template",
                "template": {
                    "name": self.config["WHATSAPP_TEMPLATE_NAME"],
                    "language": {"code": self.config["WHATSAPP_TEMPLATE_LANG"]},
                    "components": self._template_components(
                        cart,
                        user,
                        link=link,
                        token=token,
                        discount_percent=discount_percent,
                    ),
                },
            },
            headers=self._headers,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            error = (payload.get("error") or {}).get("message") or f"Cloud API error {response.status_code}"
            return MessageResult(
                ok=False,
                provider=self.name,
                mode="live",
                status=WA_STATUS_FAILED,
                message=error,
                raw=payload,
            )

        messages = payload.get("messages") or [{}]
        return MessageResult(
            ok=True,
            provider=self.name,
            mode="live",
            message_id=messages[0].get("id"),
            status=WA_STATUS_SENT,
            message="Recovery template accepted by the Cloud API",
            raw=payload,
        )

    def verify_challenge(self, params) -> str | None:
        expected = self.config["WHATSAPP_VERIFY_TOKEN"]
        if not expected:
            return None
        if params.get("hub.mode") != "subscribe":
            return None
        if not hmac.compare_digest(expected, params.get("hub.verify_token") or ""):
            return None
        return params.get("hub.challenge")

    def verify_webhook(self, headers, raw_body: bytes) -> bool:
        secret = self.config["WHATSAPP_APP_SECRET"]
        if not secret:
            return False
        signature = (
            headers.get("x-hub-signature-256")
            or headers.get("X-Hub-Signature-256")
            or ""
        )
        expected = "sha256=" + hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_status_receipts(self, payload: dict) -> list[StatusReceipt]:
        return parse_status_receipts(payload)


def parse_status_receipts(payload: dict) -> list[StatusReceipt]:
    """Flatten Meta's nested receipt envelope into one entry per message."""
    receipts: list[StatusReceipt] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                errors = status.get("errors") or []
                receipts.append(
                    StatusReceipt(
                        message_id=status.get("id") or "",
                        status=status.get("status") or "",
                        timestamp=status.get("timestamp"),
                        recipient=status.get("recipient_id"),
                        error=(errors[0].get("title") if errors else None),
                    )
                )
    return receipts


def decode_receipts(raw_body: bytes) -> list[StatusReceipt]:
    try:
        payload = json.loads(raw_body.decode() or "{}")
    except ValueError:
        return []
    return parse_status_receipts(payload)


_CLIENTS = {
    WA_PROVIDER_SANDBOX: SandboxWhatsAppClient,
    WA_PROVIDER_CLOUD: CloudApiWhatsAppClient,
}


def get_client(name: str | None = None) -> BaseWhatsAppClient:
    return _CLIENTS.get((name or WA_PROVIDER_CLOUD).lower(), CloudApiWhatsAppClient)()


def whatsapp_modes() -> dict:
    """Surfaced in the admin console so operators know whether BSP sends are live."""
    return {
        name: {
            "label": cls.label,
            "live": cls().is_live,
            "mode": "live" if cls().is_live else "sandbox",
        }
        for name, cls in _CLIENTS.items()
        if name != WA_PROVIDER_SANDBOX
    }
