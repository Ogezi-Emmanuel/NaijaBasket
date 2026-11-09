"""Payment gateway adapters.

Live mode is used whenever the corresponding API key is configured; otherwise the
adapter transparently degrades to the deterministic sandbox so the full
escrow flow remains exercisable (dev, CI, demos). `mode` always reports which
one actually ran.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from flask import current_app, url_for

from ..constants import (
    PAY_FLUTTERWAVE,
    PAY_PAYSTACK,
    PAY_SANDBOX,
    PAYMENT_CHANNELS,
)
from ..models import money, utcnow
from ..security import generate_token


@dataclass
class PaymentIntent:
    gateway: str
    reference: str
    amount: float
    authorization_url: str
    mode: str = "sandbox"
    message: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_sandbox(self) -> bool:
        return self.mode != "live"


@dataclass
class PaymentResult:
    ok: bool
    gateway: str
    reference: str
    amount: float = 0.0
    message: str = ""
    mode: str = "sandbox"
    raw: dict = field(default_factory=dict)


class PaymentGatewayError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class BaseGateway:
    name = PAY_SANDBOX
    label = "Sandbox"

    def __init__(self, config=None):
        self.config = config or current_app.config

    @property
    def is_live(self) -> bool:
        return False

    def callback_url(self) -> str:
        return url_for("checkout.callback", _external=True)

    def _new_reference(self, order) -> str:
        return f"NB-{self.name[:3].upper()}-{order.order_number}-{generate_token(4)[:6]}"

    # -- interface ---------------------------------------------------------
    def initialize(self, order) -> PaymentIntent:
        raise NotImplementedError

    def verify(self, reference: str) -> PaymentResult:
        raise NotImplementedError

    def verify_webhook(self, headers: dict, raw_body: bytes) -> tuple[bool, str | None]:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------
    def _sandbox_intent(self, order, reference: str, message: str) -> PaymentIntent:
        return PaymentIntent(
            gateway=self.name,
            reference=reference,
            amount=money(order.gmv_amount),
            authorization_url=url_for(
                "checkout.sandbox_gateway", reference=reference, _external=True
            ),
            mode="sandbox",
            message=message,
            raw={"simulated": True},
        )

    def _post(self, url: str, *, json=None, headers=None, data=None):
        import requests

        try:
            return requests.post(
                url,
                json=json,
                data=data,
                headers=headers,
                timeout=self.config["HTTP_TIMEOUT_SECONDS"],
            )
        except Exception as exc:  # network / DNS / TLS
            raise PaymentGatewayError(f"{self.label} is unreachable: {exc}") from exc

    def _get(self, url: str, *, headers=None, params=None):
        import requests

        try:
            return requests.get(
                url,
                headers=headers,
                params=params,
                timeout=self.config["HTTP_TIMEOUT_SECONDS"],
            )
        except Exception as exc:
            raise PaymentGatewayError(f"{self.label} is unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------
class SandboxGateway(BaseGateway):
    name = PAY_SANDBOX
    label = "Sandbox simulator"

    def initialize(self, order) -> PaymentIntent:
        return self._sandbox_intent(
            order,
            self._new_reference(order),
            "Sandbox mode: no live gateway key configured. Payment is simulated.",
        )

    def verify(self, reference: str) -> PaymentResult:
        return PaymentResult(
            ok=True,
            gateway=self.name,
            reference=reference,
            message="Sandbox payment approved",
            mode="sandbox",
            raw={"simulated": True},
        )

    def verify_webhook(self, headers, raw_body) -> tuple[bool, str | None]:
        return False, None


# ---------------------------------------------------------------------------
# Paystack
# ---------------------------------------------------------------------------
class PaystackGateway(BaseGateway):
    name = PAY_PAYSTACK
    label = "Paystack"

    @property
    def is_live(self) -> bool:
        return bool(self.config["PAYSTACK_SECRET_KEY"])

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config['PAYSTACK_SECRET_KEY']}",
            "Content-Type": "application/json",
        }

    def initialize(self, order) -> PaymentIntent:
        reference = self._new_reference(order)
        if not self.is_live:
            return self._sandbox_intent(
                order,
                reference,
                "Paystack key not set: falling back to sandbox checkout.",
            )

        response = self._post(
            f"{self.config['PAYSTACK_BASE']}/transaction/initialize",
            json={
                "email": order.user.email,
                "amount": int(round(order.gmv_amount * 100)),
                "currency": "NGN",
                "reference": reference,
                "callback_url": self.callback_url(),
                "metadata": {"order_number": order.order_number},
            },
            headers=self._headers,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400 or not payload.get("status"):
            raise PaymentGatewayError(
                payload.get("message") or f"Paystack initialize failed ({response.status_code})"
            )
        data = payload["data"]
        return PaymentIntent(
            gateway=self.name,
            reference=data["reference"],
            amount=money(order.gmv_amount),
            authorization_url=data["authorization_url"],
            mode="live",
            message="Paystack transaction initialised",
            raw=data,
        )

    def verify(self, reference: str) -> PaymentResult:
        if not self.is_live:
            return PaymentResult(
                ok=True,
                gateway=self.name,
                reference=reference,
                message="Sandbox approval (no Paystack key configured)",
                mode="sandbox",
                raw={"simulated": True},
            )
        response = self._get(
            f"{self.config['PAYSTACK_BASE']}/transaction/verify/{reference}",
            headers=self._headers,
        )
        payload = response.json() if response.content else {}
        data = payload.get("data") or {}
        ok = bool(payload.get("status")) and data.get("status") == "success"
        return PaymentResult(
            ok=ok,
            gateway=self.name,
            reference=reference,
            amount=money((data.get("amount") or 0) / 100),
            message=data.get("gateway_response") or payload.get("message", ""),
            mode="live",
            raw=data,
        )

    def verify_webhook(self, headers, raw_body) -> tuple[bool, str | None]:
        secret = self.config["PAYSTACK_SECRET_KEY"]
        if not secret:
            return False, None
        import hashlib
        import hmac
        import json

        signature = (
            headers.get("x-paystack-signature")
            or headers.get("X-Paystack-Signature")
            or ""
        )
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False, None
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except ValueError:
            return False, None
        if payload.get("event") != "charge.success":
            return True, None
        return True, (payload.get("data") or {}).get("reference")


# ---------------------------------------------------------------------------
# Flutterwave
# ---------------------------------------------------------------------------
class FlutterwaveGateway(BaseGateway):
    name = PAY_FLUTTERWAVE
    label = "Flutterwave"

    @property
    def is_live(self) -> bool:
        return bool(self.config["FLUTTERWAVE_SECRET_KEY"])

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config['FLUTTERWAVE_SECRET_KEY']}",
            "Content-Type": "application/json",
        }

    def initialize(self, order) -> PaymentIntent:
        reference = self._new_reference(order)
        if not self.is_live:
            return self._sandbox_intent(
                order,
                reference,
                "Flutterwave key not set: falling back to sandbox checkout.",
            )

        response = self._post(
            f"{self.config['FLUTTERWAVE_BASE']}/v3/payments",
            json={
                "tx_ref": reference,
                "amount": order.gmv_amount,
                "currency": "NGN",
                "redirect_url": self.callback_url(),
                "payment_options": "ussd,banktransfer,card",
                "customer": {
                    "email": order.user.email,
                    "phonenumber": order.user.phone,
                    "name": order.user.name,
                },
                "customizations": {"title": "NaijaBasket", "description": order.order_number},
            },
            headers=self._headers,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400 or payload.get("status") != "success":
            raise PaymentGatewayError(
                payload.get("message") or f"Flutterwave initialize failed ({response.status_code})"
            )
        return PaymentIntent(
            gateway=self.name,
            reference=reference,
            amount=money(order.gmv_amount),
            authorization_url=payload["data"]["link"],
            mode="live",
            message="Flutterwave payment link created",
            raw=payload.get("data", {}),
        )

    def verify(self, reference: str) -> PaymentResult:
        if not self.is_live:
            return PaymentResult(
                ok=True,
                gateway=self.name,
                reference=reference,
                message="Sandbox approval (no Flutterwave key configured)",
                mode="sandbox",
                raw={"simulated": True},
            )
        response = self._get(
            f"{self.config['FLUTTERWAVE_BASE']}/v3/transactions/verify_by_reference",
            headers=self._headers,
            params={"tx_ref": reference},
        )
        payload = response.json() if response.content else {}
        data = payload.get("data") or {}
        ok = data.get("status") == "successful"
        return PaymentResult(
            ok=ok,
            gateway=self.name,
            reference=reference,
            amount=money(data.get("amount") or 0),
            message=data.get("processor_response") or payload.get("message", ""),
            mode="live",
            raw=data,
        )

    def verify_webhook(self, headers, raw_body) -> tuple[bool, str | None]:
        import hmac
        import json

        expected = self.config["FLUTTERWAVE_WEBHOOK_HASH"]
        if not expected:
            return False, None
        signature = headers.get("verif-hash") or headers.get("Verif-Hash") or ""
        if not hmac.compare_digest(expected, signature):
            return False, None
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except ValueError:
            return False, None
        data = payload.get("data") or {}
        return True, data.get("tx_ref") or payload.get("txRef")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_GATEWAYS = {
    PAY_SANDBOX: SandboxGateway,
    PAY_PAYSTACK: PaystackGateway,
    PAY_FLUTTERWAVE: FlutterwaveGateway,
}


def get_gateway(name: str | None) -> BaseGateway:
    return _GATEWAYS.get((name or PAY_SANDBOX).lower(), SandboxGateway)()


def gateway_for_channel(channel: str) -> BaseGateway:
    spec = PAYMENT_CHANNELS.get(channel) or PAYMENT_CHANNELS["transfer"]
    return get_gateway(spec["gateway"])


def gateway_modes() -> dict:
    """Surfaced in the admin dashboard so operators know what is live."""
    return {
        name: {
            "label": cls.label,
            "live": cls().is_live,
            "mode": "live" if cls().is_live else "sandbox",
        }
        for name, cls in _GATEWAYS.items()
        if name != PAY_SANDBOX
    }


def payment_deadline(minutes: int = 30):
    return utcnow() + timedelta(minutes=minutes)
