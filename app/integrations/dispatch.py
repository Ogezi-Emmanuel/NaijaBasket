"""3PL dispatch bridge (FR-03).

`request_dispatch(order)` is the single entry point used by the merchant OMS:
it geo-selects a courier by proximity, enforces the 5-8 km delivery radius,
persists a DispatchJob and advances the order to `dispatched`.

The rider payload deliberately excludes the delivery PIN - escrow fraud
prevention depends on the code never reaching the courier's device.
"""

import json
from dataclasses import dataclass, field

from flask import current_app

from ..constants import (
    DISPATCH_ASSIGNED,
    DISPATCH_FAILED,
    DISPATCH_REQUESTED,
    PROVIDER_GOKADA,
    PROVIDER_KWIK,
    PROVIDER_SANDBOX,
    ST_DISPATCHED,
)
from ..extensions import db
from ..models import DispatchJob, money, utcnow
from ..services import geo as geo_service
from ..services import sla as sla_service


class DispatchError(RuntimeError):
    pass


@dataclass
class DispatchQuote:
    provider: str
    mode: str
    amount: float
    eta_minutes: int
    raw: dict = field(default_factory=dict)


@dataclass
class DispatchResult:
    ok: bool
    provider: str
    mode: str
    provider_ref: str | None = None
    rider_name: str | None = None
    rider_phone: str | None = None
    vehicle: str | None = None
    eta_minutes: int | None = None
    amount: float = 0.0
    message: str = ""
    raw: dict = field(default_factory=dict)


class BaseCourier:
    name = PROVIDER_SANDBOX
    label = "Sandbox courier"

    def __init__(self, config=None):
        self.config = config or current_app.config

    @property
    def is_live(self) -> bool:
        return False

    def quote(self, distance_km: float) -> DispatchQuote:
        raise NotImplementedError

    def create_task(self, order, distance_km: float) -> DispatchResult:
        raise NotImplementedError

    # -- shared ------------------------------------------------------------
    def _eta_minutes(self, distance_km: float) -> int:
        # Lagos traffic assumption: ~9 minutes base + 4 min per km.
        return int(round(9 + 4 * max(0.0, distance_km)))

    def _rider_payload(self, order, distance_km: float) -> dict:
        """Never includes the delivery PIN or the full street address."""
        store = order.store
        return {
            "order_number": order.order_number,
            "pickup": {
                "store": store.name,
                "latitude": store.gps_latitude,
                "longitude": store.gps_longitude,
            },
            "dropoff": {
                "latitude": order.delivery_latitude,
                "longitude": order.delivery_longitude,
                "area_hint": (order.delivery_address or "")[-28:],
            },
            "distance_km": distance_km,
            "thermal_bag_required": True,
            "cod_amount": 0,
            "note": "Escrow order. Collect the buyer's 4-digit code on delivery.",
        }

    def _post(self, url, *, json_body=None, headers=None):
        import requests

        try:
            return requests.post(
                url,
                json=json_body,
                headers=headers,
                timeout=self.config["HTTP_TIMEOUT_SECONDS"],
            )
        except Exception as exc:
            raise DispatchError(f"{self.label} unreachable: {exc}") from exc


class SandboxCourier(BaseCourier):
    """Deterministic courier used when no 3PL key is configured."""

    name = PROVIDER_SANDBOX
    label = "Sandbox courier"
    _RIDERS = (
        ("Chinedu O.", "+2348030000001", "Bajaj Boxer"),
        ("Musa A.", "+2348030000002", "TVS HLX"),
        ("Blessing E.", "+2348030000003", "Jincheng"),
        ("Tunde S.", "+2348030000004", "Bajaj Pulsar"),
    )

    def quote(self, distance_km: float) -> DispatchQuote:
        return DispatchQuote(
            provider=self.name,
            mode="sandbox",
            amount=money(self.config["LOGISTICS_BASE_COST"] + 12.5 * max(0.0, distance_km)),
            eta_minutes=self._eta_minutes(distance_km),
            raw={"simulated": True},
        )

    def create_task(self, order, distance_km: float) -> DispatchResult:
        rider_name, rider_phone, vehicle = self._RIDERS[order.id % len(self._RIDERS)]
        quote = self.quote(distance_km)
        return DispatchResult(
            ok=True,
            provider=self.name,
            mode="sandbox",
            provider_ref=f"SBX-{order.order_number}",
            rider_name=rider_name,
            rider_phone=rider_phone,
            vehicle=vehicle,
            eta_minutes=quote.eta_minutes,
            amount=quote.amount,
            message="Sandbox courier assigned automatically",
            raw={"simulated": True},
        )


class KwikCourier(BaseCourier):
    name = PROVIDER_KWIK
    label = "Kwik"

    @property
    def is_live(self) -> bool:
        return bool(self.config["KWIK_API_KEY"])

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config['KWIK_API_KEY']}",
            "Content-Type": "application/json",
        }

    def quote(self, distance_km: float) -> DispatchQuote:
        amount = money(self.config["LOGISTICS_BASE_COST"] + 12.5 * max(0.0, distance_km))
        return DispatchQuote(
            provider=self.name,
            mode="live" if self.is_live else "sandbox",
            amount=amount,
            eta_minutes=self._eta_minutes(distance_km),
        )

    def create_task(self, order, distance_km: float) -> DispatchResult:
        if not self.is_live:
            fallback = SandboxCourier(self.config).create_task(order, distance_km)
            fallback.provider = self.name
            fallback.mode = "sandbox"
            fallback.message = "Kwik key not set: sandbox courier assigned"
            return fallback

        response = self._post(
            f"{self.config['KWIK_API_BASE']}/v1/tasks",
            json_body=self._rider_payload(order, distance_km),
            headers=self._headers,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise DispatchError(payload.get("message") or f"Kwik error {response.status_code}")
        data = payload.get("data") or payload
        return DispatchResult(
            ok=True,
            provider=self.name,
            mode="live",
            provider_ref=str(data.get("task_id") or data.get("id") or ""),
            rider_name=(data.get("rider") or {}).get("name"),
            rider_phone=(data.get("rider") or {}).get("phone"),
            vehicle=(data.get("rider") or {}).get("vehicle"),
            eta_minutes=data.get("eta_minutes") or self._eta_minutes(distance_km),
            amount=money(data.get("price") or self.quote(distance_km).amount),
            message="Kwik task created",
            raw=data,
        )


class GokadaCourier(BaseCourier):
    name = PROVIDER_GOKADA
    label = "Gokada"

    @property
    def is_live(self) -> bool:
        return bool(self.config["GOKADA_API_KEY"])

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config['GOKADA_API_KEY']}",
            "Content-Type": "application/json",
        }

    def quote(self, distance_km: float) -> DispatchQuote:
        amount = money(self.config["LOGISTICS_BASE_COST"] + 15.0 * max(0.0, distance_km))
        return DispatchQuote(
            provider=self.name,
            mode="live" if self.is_live else "sandbox",
            amount=amount,
            eta_minutes=self._eta_minutes(distance_km) + 2,
        )

    def create_task(self, order, distance_km: float) -> DispatchResult:
        if not self.is_live:
            fallback = SandboxCourier(self.config).create_task(order, distance_km)
            fallback.provider = self.name
            fallback.mode = "sandbox"
            fallback.message = "Gokada key not set: sandbox courier assigned"
            return fallback

        response = self._post(
            f"{self.config['GOKADA_API_BASE']}/api/v1/deliveries",
            json_body=self._rider_payload(order, distance_km),
            headers=self._headers,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise DispatchError(payload.get("message") or f"Gokada error {response.status_code}")
        data = payload.get("data") or payload
        return DispatchResult(
            ok=True,
            provider=self.name,
            mode="live",
            provider_ref=str(data.get("delivery_id") or data.get("id") or ""),
            rider_name=(data.get("courier") or {}).get("name"),
            rider_phone=(data.get("courier") or {}).get("phone"),
            vehicle=(data.get("courier") or {}).get("vehicle"),
            eta_minutes=data.get("eta_minutes") or self.quote(distance_km).eta_minutes,
            amount=money(data.get("fare") or self.quote(distance_km).amount),
            message="Gokada delivery created",
            raw=data,
        )


_COURIERS = {
    PROVIDER_SANDBOX: SandboxCourier,
    PROVIDER_KWIK: KwikCourier,
    PROVIDER_GOKADA: GokadaCourier,
}


def get_courier(name: str | None) -> BaseCourier:
    return _COURIERS.get((name or PROVIDER_SANDBOX).lower(), SandboxCourier)()


def courier_modes() -> dict:
    return {
        name: {"label": cls.label, "live": cls().is_live, "mode": "live" if cls().is_live else "sandbox"}
        for name, cls in _COURIERS.items()
        if name != PROVIDER_SANDBOX
    }


def select_courier(distance_km: float) -> BaseCourier:
    """`auto` prefers a live provider with the cheapest quote, else sandbox."""
    configured = (current_app.config.get("DISPATCH_PROVIDER") or "auto").lower()
    if configured in _COURIERS:
        return get_courier(configured)

    live = [get_courier(name) for name in (PROVIDER_KWIK, PROVIDER_GOKADA) if get_courier(name).is_live]
    if not live:
        return get_courier(PROVIDER_SANDBOX)
    return min(live, key=lambda courier: courier.quote(distance_km).amount)


def order_distance_km(order) -> float | None:
    if order.delivery_latitude is None or order.delivery_longitude is None:
        return None
    return geo_service.distance_between_stores(
        order.delivery_latitude,
        order.delivery_longitude,
        order.store.gps_latitude,
        order.store.gps_longitude,
    )


def request_dispatch(order, *, actor: str = "merchant") -> DispatchJob:
    """Triggered when the merchant marks an order 'Packed' (FR-03)."""
    if order.status not in ("packed", "paid_escrow", "packing"):
        raise DispatchError(f"Cannot dispatch an order in state '{order.status}'.")

    distance = order_distance_km(order)
    if distance is not None and not geo_service.within_delivery_radius(distance):
        raise DispatchError(
            f"Drop-off is {distance:.1f} km away, outside the "
            f"{geo_service.delivery_radius_km():.0f} km delivery radius."
        )
    effective_distance = distance if distance is not None else 5.0

    courier = select_courier(effective_distance)
    job = DispatchJob(
        order_id=order.id,
        provider=courier.name,
        status=DISPATCH_REQUESTED,
        mode="sandbox",
        distance_km=effective_distance,
        thermal_bag=True,
        requested_at=utcnow(),
    )
    db.session.add(job)
    db.session.flush()

    try:
        result = courier.create_task(order, effective_distance)
    except DispatchError as exc:
        job.status = DISPATCH_FAILED
        job.error = str(exc)[:255]
        db.session.flush()
        raise

    job.status = DISPATCH_ASSIGNED
    job.mode = result.mode
    job.provider_ref = result.provider_ref
    job.rider_name = result.rider_name
    job.rider_phone = result.rider_phone
    job.vehicle = result.vehicle
    job.eta_minutes = result.eta_minutes
    job.quote_amount = money(result.amount)
    job.assigned_at = utcnow()
    job.payload = json.dumps(_masked_payload(courier._rider_payload(order, effective_distance)))

    order.status = ST_DISPATCHED
    order.dispatched_at = job.assigned_at
    order.logistics_cost = job.quote_amount
    db.session.flush()
    return job


def _masked_payload(payload: dict) -> dict:
    """Persisted copy has the street fragment redacted (NDPA minimisation)."""
    safe = json.loads(json.dumps(payload))
    safe.get("dropoff", {}).pop("area_hint", None)
    return safe


def transit_state(order) -> dict:
    job = order.latest_dispatch
    if job is None:
        return {"assigned": False}
    deadline = sla_service.transit_deadline(job.assigned_at) if job.assigned_at else None
    return {
        "assigned": True,
        "provider": job.provider,
        "mode": job.mode,
        "status": job.status,
        "rider_name": job.rider_name,
        "rider_phone": job.rider_phone,
        "vehicle": job.vehicle,
        "eta_minutes": job.eta_minutes,
        "distance_km": job.distance_km,
        "assigned_at": job.assigned_at,
        "transit_deadline": deadline,
        "on_time": (
            deadline is None
            or order.delivered_at is None
            or order.delivered_at <= deadline
        ),
    }
