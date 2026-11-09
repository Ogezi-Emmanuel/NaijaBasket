"""JSON API powering the PWA (offline-first basket + live tracking).

Every mutating route re-validates on the server: the NGN 15,000 floor, stock
availability and ownership scoping are never trusted to the client.
"""

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from ..constants import CATEGORIES, CATEGORY_LABELS, PAYMENT_CHANNELS
from ..extensions import db
from ..integrations import dispatch as dispatch_service
from ..models import Product, money, utcnow
from ..services import cart as cart_service
from ..services import economics as economics_service
from ..services import geo as geo_service
from ..services import safety as safety_service
from ..services import sla as sla_service

bp = Blueprint("api", __name__)


def _json(payload, status: int = 200):
    return jsonify(payload), status


def _product_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "category_label": CATEGORY_LABELS.get(product.category, product.category),
        "unit": product.unit,
        "price": money(product.price),
        "price_display": f"\u20a6{product.price:,.0f}",
        "store_id": product.store_id,
        "store_name": product.store.name if product.store else None,
        "safety_category": product.safety_category,
        "nafdac_reg_no": product.nafdac_reg_no,
        "nafdac_verified": product.nafdac_verified,
        "in_stock": product.in_stock,
        "image_url": product.image_url,
    }


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
@bp.get("/health")
def health():
    from flask import current_app

    return _json(
        {
            "ok": True,
            "service": "naijabasket",
            "time": utcnow().isoformat(),
            "min_order_value": money(current_app.config["MIN_ORDER_VALUE"]),
        }
    )


@bp.get("/config")
def config():
    from flask import current_app

    return _json(
        {
            "min_order_value": money(current_app.config["MIN_ORDER_VALUE"]),
            "target_aov": money(current_app.config["TARGET_AOV"]),
            "convenience_fee": money(current_app.config["CONVENIENCE_FEE"]),
            "merchant_sla_minutes": current_app.config["MERCHANT_SLA_MINUTES"],
            "transit_sla_minutes": current_app.config["TRANSIT_SLA_MINUTES"],
            "delivery_radius_km": current_app.config["DELIVERY_RADIUS_KM"],
            "categories": [
                {"value": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES
            ],
            "payment_channels": [
                {"value": key, "label": spec["label"], "hint": spec["hint"]}
                for key, spec in PAYMENT_CHANNELS.items()
            ],
        }
    )


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
@bp.get("/catalog")
def catalog():
    from .storefront import browse_products

    products = browse_products(
        query=request.args.get("q"),
        category=request.args.get("category"),
        store_id=request.args.get("store", type=int),
        sort=request.args.get("sort", "popular"),
        limit=min(60, request.args.get("limit", 24, type=int)),
    )
    return _json({"ok": True, "count": len(products), "products": [_product_payload(p) for p in products]})


# ---------------------------------------------------------------------------
# Basket
# ---------------------------------------------------------------------------
def _cart_payload(cart) -> dict:
    if cart is None:
        return {
            "items": [],
            "subtotal": 0.0,
            "item_count": 0,
            "minimum": cart_service.minimum_order_state(0),
            "suggestions": [],
        }
    suggestions = cart_service.frequently_bought_together(cart) if cart.items else []
    return {
        "items": [
            {
                "product_id": item.product_id,
                "name": item.product.name,
                "quantity": item.quantity,
                "unit_price": money(item.product.price),
                "line_total": item.line_total,
                "safety_category": item.safety_category,
                "stock_qty": item.product.stock_qty,
                "image_url": item.product.image_url,
            }
            for item in cart.items
        ],
        "subtotal": cart.subtotal,
        "item_count": cart.item_count,
        "store": cart.store.name if cart.store else None,
        "minimum": cart_service.minimum_order_state(cart.subtotal),
        "suggestions": [
            {
                "product_id": s["product"].id,
                "name": s["product"].name,
                "price": money(s["product"].price),
                "badge": s["badge"],
                "reason": s["reason"],
                "image_url": s["product"].image_url,
            }
            for s in suggestions
        ],
    }


@bp.get("/cart")
@login_required
def get_cart():
    cart = cart_service.cart_for(current_user)
    return _json({"ok": True, "cart": _cart_payload(cart)})


@bp.post("/cart/items")
@login_required
def add_to_cart():
    payload = request.get_json(silent=True) or request.form
    product_id = payload.get("product_id")
    if not product_id:
        return _json({"ok": False, "error": "product_id is required"}, 400)

    product = db.session.get(Product, int(product_id))
    if product is None or not product.is_active:
        return _json({"ok": False, "error": "Product is not available"}, 404)

    cart = cart_service.get_or_create_cart(current_user)
    try:
        cart_service.add_item(cart, product, int(payload.get("quantity") or 1))
    except cart_service.CartError as exc:
        db.session.rollback()
        return _json({"ok": False, "error": str(exc)}, 400)

    db.session.commit()
    return _json({"ok": True, "message": f"{product.name} added", "cart": _cart_payload(cart)})


@bp.patch("/cart/items/<int:product_id>")
@login_required
def update_cart_item(product_id):
    payload = request.get_json(silent=True) or request.form
    cart = cart_service.get_or_create_cart(current_user)
    try:
        cart_service.update_item(cart, product_id, int(payload.get("quantity") or 0))
    except cart_service.CartError as exc:
        db.session.rollback()
        return _json({"ok": False, "error": str(exc)}, 400)

    db.session.commit()
    return _json({"ok": True, "cart": _cart_payload(cart)})


@bp.delete("/cart/items/<int:product_id>")
@login_required
def delete_cart_item(product_id):
    cart = cart_service.get_or_create_cart(current_user)
    cart_service.remove_item(cart, product_id)
    db.session.commit()
    return _json({"ok": True, "cart": _cart_payload(cart)})


@bp.post("/cart/clear")
@login_required
def clear_cart():
    cart = cart_service.get_or_create_cart(current_user)
    cart_service.clear(cart)
    db.session.commit()
    return _json({"ok": True, "cart": _cart_payload(cart)})


@bp.post("/cart/validate")
@login_required
def validate_cart():
    """Server-authoritative gate used by the checkout button."""
    cart = cart_service.cart_for(current_user)
    if cart is None or not cart.items:
        return _json({"ok": False, "error": "Your basket is empty."}, 400)

    state = cart_service.minimum_order_state(cart.subtotal)
    if not state["met"]:
        return _json(
            {
                "ok": False,
                "error": f"Minimum basket is \u20a6{state['minimum']:,.0f}.",
                "minimum": state,
                "cart": _cart_payload(cart),
            },
            400,
        )

    store = cart.store
    economics = economics_service.estimate_basket_economics(
        cart.subtotal, store.commission_rate if store else 0.055
    )
    return _json({"ok": True, "minimum": state, "economics": economics, "cart": _cart_payload(cart)})


# ---------------------------------------------------------------------------
# Orders / tracking
# ---------------------------------------------------------------------------
@bp.get("/orders/<ref>/status")
@login_required
def order_status(ref):
    from ..blueprints.orders import resolve_order

    order = resolve_order(ref)
    if not (
        current_user.is_admin
        or order.user_id == current_user.id
        or (current_user.is_merchant and current_user.store_id == order.store_id)
    ):
        return _json({"ok": False, "error": "forbidden"}, 403)

    transit = dispatch_service.transit_state(order)
    payload = {
        "ok": True,
        "order_number": order.order_number,
        "status": order.status,
        "status_label": order.status_label,
        "escrow_status": order.payment_escrow_status,
        "tracker": [{"label": label, "reached": reached} for label, reached in order.tracker_steps],
        "sla": {
            "deadline": order.sla_deadline.isoformat() if order.sla_deadline else None,
            "seconds_remaining": order.sla_seconds_remaining,
            "state": order.sla_state,
            "breached": order.sla_breached,
        },
        "transit": {
            key: (value.isoformat() if hasattr(value, "isoformat") else value)
            for key, value in transit.items()
        },
        "pin_available": order.user_id == current_user.id and order.payment_escrow_status == "held",
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
    }
    if order.user_id == current_user.id and order.payment_escrow_status == "held":
        payload["delivery_pin"] = order.delivery_pin
    return _json(payload)


@bp.get("/orders")
@login_required
def my_orders():
    from ..models import Order

    orders = (
        db.session.query(Order)
        .filter(Order.user_id == current_user.id)
        .order_by(Order.created_at.desc())
        .limit(30)
        .all()
    )
    return _json(
        {
            "ok": True,
            "orders": [
                {
                    "order_number": o.order_number,
                    "status": o.status,
                    "status_label": o.status_label,
                    "gmv": money(o.gmv_amount),
                    "store": o.store.name,
                    "created_at": o.created_at.isoformat(),
                }
                for o in orders
            ],
        }
    )


# ---------------------------------------------------------------------------
# Merchant OMS feed
# ---------------------------------------------------------------------------
@bp.get("/merchant/orders")
@login_required
def merchant_orders():
    if not (current_user.is_merchant or current_user.is_admin):
        return _json({"ok": False, "error": "forbidden"}, 403)

    from ..constants import SLA_TRACKED_STATUSES
    from ..models import Order

    store_id = current_user.store_id if current_user.is_merchant else request.args.get("store", type=int)
    query = db.session.query(Order).filter(Order.status.in_(SLA_TRACKED_STATUSES))
    if store_id:
        query = query.filter(Order.store_id == store_id)

    return _json(
        {
            "ok": True,
            "orders": [
                {
                    "order_number": o.order_number,
                    "status": o.status,
                    "gmv": money(o.gmv_amount),
                    "item_count": o.item_count,
                    "seconds_remaining": o.sla_seconds_remaining,
                    "sla_state": o.sla_state,
                    "cross_contamination_risk": o.cross_contamination_risk,
                    "url": f"/merchant/orders/{o.order_number}",
                }
                for o in query.order_by(Order.sla_deadline.asc()).all()
            ],
        }
    )


@bp.get("/merchant/orders/<ref>/picklist")
@login_required
def merchant_picklist(ref):
    if not (current_user.is_merchant or current_user.is_admin):
        return _json({"ok": False, "error": "forbidden"}, 403)

    from ..blueprints.merchant import _scoped_order

    order = _scoped_order(ref)
    pick_list = safety_service.build_pick_list(order.items)
    return _json(
        {
            "ok": True,
            "order_number": order.order_number,
            "status": order.status,
            "seconds_remaining": order.sla_seconds_remaining,
            "cross_contamination_risk": pick_list["cross_contamination_risk"],
            "segregation_rules": pick_list["segregation_rules"],
            "lanes": [
                {
                    "safety_category": lane["safety_category"],
                    "label": lane["label"],
                    "instruction": lane["instruction"],
                    "line_total": lane["line_total"],
                    "items": [
                        {
                            "name": item.name,
                            "quantity": item.quantity,
                            "nafdac_reg_no": item.nafdac_reg_no,
                            "requires_nafdac": item.requires_nafdac,
                        }
                        for item in lane["items"]
                    ],
                }
                for lane in pick_list["lanes"]
            ],
        }
    )


@bp.get("/sla/overview")
@login_required
def sla_overview():
    if not (current_user.is_merchant or current_user.is_admin):
        return _json({"ok": False, "error": "forbidden"}, 403)
    return _json({"ok": True, **sla_service.platform_sla_overview()})


@bp.get("/geo/radius-check")
def radius_check():
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    if lat is None or lng is None:
        return _json({"ok": False, "error": "lat and lng are required"}, 400)
    distance = geo_service.haversine_km(*geo_service.LAGOS_CENTER, lat, lng)
    return _json(
        {
            "ok": True,
            "distance_km": distance,
            "within_radius": geo_service.within_delivery_radius(distance),
            "radius_km": geo_service.delivery_radius_km(),
        }
    )
