"""Basket engine (FR-01): minimum order validation and cross-sell.

The NGN 15,000 floor is enforced here in server code - the checkout route and
the JSON API both call `validate_minimum`, so a tampered client payload can
never produce a sub-threshold order.
"""

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from ..constants import CATEGORIES
from ..extensions import db
from ..models import Cart, CartItem, OrderItem, Product, money


class CartError(ValueError):
    """Raised for user-correctable basket problems."""


class MinimumOrderError(CartError):
    def __init__(self, subtotal: float, minimum: float):
        self.subtotal = money(subtotal)
        self.minimum = money(minimum)
        self.shortfall = money(max(0.0, self.minimum - self.subtotal))
        super().__init__(
            f"Minimum basket is NGN {self.minimum:,.0f}. Add NGN {self.shortfall:,.0f} more."
        )


def get_or_create_cart(user) -> Cart:
    cart = (
        db.session.execute(
            select(Cart).where(Cart.user_id == user.id).order_by(Cart.id.desc())
        )
        .scalars()
        .first()
    )
    if cart is None:
        cart = Cart(user_id=user.id)
        db.session.add(cart)
        db.session.flush()
    return cart


def cart_for(user) -> Cart:
    """Read-only accessor for views that must not create rows on GET."""
    return (
        db.session.execute(
            select(Cart).where(Cart.user_id == user.id).order_by(Cart.id.desc())
        )
        .scalars()
        .first()
    )


def touch(cart: Cart) -> None:
    from ..models import utcnow

    cart.updated_at = utcnow()
    db.session.add(cart)


def add_item(cart: Cart, product: Product, quantity: int = 1) -> CartItem:
    quantity = max(1, int(quantity))
    if not product.is_active:
        raise CartError(f"{product.name} is no longer available.")
    if product.stock_qty < quantity:
        raise CartError(f"Only {product.stock_qty} x {product.name} left in this store.")

    # One basket = one partner store: orders are routed to a single vetted store.
    if cart.store_id and cart.store_id != product.store_id and not cart.find_item(product.id):
        raise CartError(
            "Your basket is fulfilled by one partner store at a time. "
            "Empty the basket to shop another store."
        )

    existing = cart.find_item(product.id)
    if existing:
        existing.quantity = min(existing.quantity + quantity, product.stock_qty)
    else:
        existing = CartItem(product_id=product.id, quantity=quantity)
        cart.items.append(existing)
    if cart.store_id is None:
        cart.store_id = product.store_id
    touch(cart)
    db.session.flush()
    return existing


def update_item(cart: Cart, product_id: int, quantity: int) -> CartItem | None:
    item = cart.find_item(product_id)
    if item is None:
        raise CartError("That item is not in your basket.")
    quantity = int(quantity)
    if quantity <= 0:
        return remove_item(cart, product_id)
    if quantity > item.product.stock_qty:
        raise CartError(f"Only {item.product.stock_qty} x {item.product.name} left in this store.")
    item.quantity = quantity
    touch(cart)
    db.session.flush()
    return item


def remove_item(cart: Cart, product_id: int) -> None:
    item = cart.find_item(product_id)
    if item is not None:
        cart.items.remove(item)
        db.session.flush()
    if not cart.items:
        cart.store_id = None
    touch(cart)


def clear(cart: Cart) -> None:
    for item in list(cart.items):
        db.session.delete(item)
    cart.store_id = None
    cart.recovery_token = None
    cart.recovery_discount = 0.0
    cart.recovery_sent_at = None
    cart.recovery_channel = None
    cart.recovery_provider = None
    cart.recovery_message_id = None
    cart.recovery_status = None
    cart.recovery_delivered_at = None
    cart.recovery_read_at = None
    cart.recovery_error = None
    touch(cart)
    db.session.flush()


# ---------------------------------------------------------------------------
# Minimum order rule
# ---------------------------------------------------------------------------
def minimum_order_value() -> float:
    return money(current_app.config["MIN_ORDER_VALUE"])


def validate_minimum(subtotal: float) -> None:
    minimum = minimum_order_value()
    if money(subtotal) < minimum:
        raise MinimumOrderError(subtotal, minimum)


def minimum_order_state(subtotal: float) -> dict:
    minimum = minimum_order_value()
    subtotal = money(subtotal)
    return {
        "subtotal": subtotal,
        "minimum": minimum,
        "shortfall": money(max(0.0, minimum - subtotal)),
        "met": subtotal >= minimum,
        "progress_pct": round(min(100.0, subtotal / minimum * 100), 1),
        "target_aov": money(current_app.config["TARGET_AOV"]),
        "aov_progress_pct": round(
            min(100.0, subtotal / max(1, current_app.config["TARGET_AOV"]) * 100), 1
        ),
    }


# ---------------------------------------------------------------------------
# Cross-sell (dynamic add-on incentives)
# ---------------------------------------------------------------------------
def frequently_bought_together(cart: Cart, limit: int = 4) -> list[dict]:
    """Co-occurrence based recommendations, topped up to reach target AOV."""
    in_cart = {item.product_id for item in cart.items}
    if not in_cart:
        return _top_boosters(cart, limit)

    # Co-occurrence: products that share an order with anything already in the
    # basket. OrderItem has to be joined to a second alias of itself - joining
    # it to the same mapper gives SQLAlchemy no left side to anchor to.
    companion = aliased(OrderItem)
    co_occurrence = (
        db.session.query(
            OrderItem.product_id,
            func.count(func.distinct(OrderItem.order_id)).label("orders"),
        )
        .join(companion, companion.order_id == OrderItem.order_id)
        .filter(companion.product_id.in_(in_cart))
        .filter(OrderItem.product_id.notin_(in_cart))
        .group_by(OrderItem.product_id)
        .order_by(func.count(func.distinct(OrderItem.order_id)).desc())
        .limit(40)
        .all()
    )

    picked: list[dict] = []
    seen: set[int] = set(in_cart)  # never suggest what the basket already holds
    for product_id, orders in co_occurrence:
        product = db.session.get(Product, product_id)
        if product is None or not product.in_stock or product.id in seen:
            continue
        seen.add(product.id)
        picked.append(
            {
                "product": product,
                "reason": f"Bought with {len(in_cart)} of your basket items in {orders} orders",
                "badge": "Frequently bought together",
            }
        )
        if len(picked) >= limit:
            return picked

    for suggestion in _top_boosters(cart, limit):
        if suggestion["product"].id not in seen:
            picked.append(suggestion)
            seen.add(suggestion["product"].id)
        if len(picked) >= limit:
            break
    return picked


def _top_boosters(cart: Cart, limit: int) -> list[dict]:
    """Fill the gap to the NGN 35,000 target AOV with high-value staples."""
    subtotal = cart.subtotal
    remaining = max(0.0, current_app.config["TARGET_AOV"] - subtotal)
    categories = list(CATEGORIES)
    query = (
        select(Product)
        .where(Product.is_active.is_(True), Product.stock_qty > 0)
        .order_by(Product.price.desc())
        .limit(24)
    )
    if cart.store_id:
        query = query.where(Product.store_id == cart.store_id)

    products = list(db.session.execute(query).scalars())
    boosts = []
    for product in products:
        if remaining and product.price > remaining * 2.2:
            continue
        boosts.append(
            {
                "product": product,
                "reason": (
                    f"Closes NGN {remaining:,.0f} of your target basket"
                    if remaining
                    else f"Top {product.category.replace('_', ' ')} staple"
                ),
                "badge": "Smart add-on",
            }
        )
        if len(boosts) >= limit:
            break
    if boosts:
        return boosts
    # Fallback: any active stock, ordered by category coverage.
    fallback = list(
        db.session.execute(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.id).limit(limit)
        ).scalars()
    )
    return [
        {"product": p, "reason": f"Popular in {p.category.replace('_', ' ')}", "badge": "Smart add-on"}
        for p in fallback
    ]


def merge_suggestion_lists(*lists) -> list[dict]:
    merged, seen = [], set()
    for group in lists:
        for suggestion in group or []:
            product = suggestion["product"]
            if product.id in seen:
                continue
            seen.add(product.id)
            merged.append(suggestion)
    return merged
