"""Order construction: basket -> escrow-ready Order with frozen item snapshot."""

import secrets

from flask import current_app

from ..constants import (
    ESCROW_PENDING,
    PAY_SANDBOX,
    ST_PENDING_PAYMENT,
)
from ..extensions import db
from ..models import Order, OrderItem, PartnerStore, Product, User, money, utcnow
from ..security import generate_delivery_pin
from . import cart as cart_service
from . import economics as economics_service
from . import geo as geo_service


class OrderError(ValueError):
    pass


def _next_order_number() -> str:
    for _ in range(8):
        candidate = f"NB-{utcnow():%y%m%d}-{secrets.randbelow(9000) + 1000}"
        exists = (
            db.session.query(Order.id).filter(Order.order_number == candidate).first()
        )
        if not exists:
            return candidate
    raise OrderError("Could not allocate an order number, please retry.")


def resolve_store(user_lat: float | None, user_lng: float | None):
    """Nearest active partner to the delivery point, if coordinates are known."""
    stores = list(
        db.session.query(PartnerStore).filter(PartnerStore.is_active.is_(True)).all()
    )
    if not stores:
        raise OrderError("No partner store is currently online.")
    if user_lat is None or user_lng is None:
        return stores[0], None
    store, distance = geo_service.nearest_store(stores, user_lat, user_lng)
    return store, distance


def create_order_from_cart(
    user: User,
    cart,
    *,
    delivery_address: str,
    latitude: float | None = None,
    longitude: float | None = None,
    channel: str = "transfer",
    gateway: str | None = None,
    delivery_window: str = "asap",
) -> Order:
    """Deterministic, server-authoritative order creation.

    Raises MinimumOrderError / OrderError so the caller can render a precise
    message instead of silently creating a bad order.
    """
    if cart is None or not cart.items:
        raise OrderError("Your basket is empty.")

    subtotal = cart.subtotal
    cart_service.validate_minimum(subtotal)  # FR-01 (hard server-side floor)

    store = cart.store
    if store is None:
        store, _ = resolve_store(latitude, longitude)

    distance = None
    if latitude is not None and longitude is not None:
        distance = geo_service.distance_between_stores(
            latitude, longitude, store.gps_latitude, store.gps_longitude
        )
        if not geo_service.within_delivery_radius(distance):
            raise OrderError(
                f"{store.name} is {distance:.1f} km away. Deliveries are limited to "
                f"{geo_service.delivery_radius_km():.0f} km from the store."
            )

    economics = economics_service.compute_order_economics(
        subtotal,
        store.commission_rate,
        is_subscriber=user.subscription_active,
        distance_km=distance,
        recovery_discount=cart.recovery_discount or 0.0,
    )

    order = Order(
        order_number=_next_order_number(),
        user_id=user.id,
        store_id=store.id,
        status=ST_PENDING_PAYMENT,
        payment_escrow_status=ESCROW_PENDING,
        payment_channel=channel,
        delivery_window=delivery_window,
        delivery_latitude=latitude,
        delivery_longitude=longitude,
        recovery_discount=money(cart.recovery_discount or 0.0),
        **economics.order_columns(),
    )
    order.delivery_address = delivery_address
    order.payment_gateway = gateway or PAY_SANDBOX
    order.issue_delivery_pin(generate_delivery_pin())
    db.session.add(order)
    db.session.flush()

    for item in cart.items:
        product: Product = item.product
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                name=product.name,
                category=product.category,
                quantity=item.quantity,
                unit_price=money(product.price),
                safety_category=product.safety_category,
                nafdac_reg_no=product.nafdac_reg_no,
            )
        )

    db.session.flush()
    return order


def reserve_stock(order: Order) -> None:
    """Decrement partner stock once funds are held in escrow."""
    for item in order.items:
        product = db.session.get(Product, item.product_id)
        if product is not None:
            product.stock_qty = max(0, product.stock_qty - item.quantity)


def open_orders_for_store(store_id: int):
    from ..constants import SLA_TRACKED_STATUSES

    return (
        db.session.query(Order)
        .filter(
            Order.store_id == store_id,
            Order.status.in_(SLA_TRACKED_STATUSES),
        )
        .order_by(Order.sla_deadline.asc())
        .all()
    )
