"""Basket engine (FR-01): the NGN 15,000 floor and single-store routing."""

import pytest

from app.services import cart as cart_service


def test_add_item_sets_store_and_subtotal(app, sample, fill_cart):
    cart = fill_cart([("tomatoes", 2), ("rice", 1)])

    assert cart.store_id == sample["store"].id
    assert cart.subtotal == 9_000 * 2 + 41_000
    assert cart.item_count == 3


def test_add_item_accumulates_for_the_same_product(app, sample, fill_cart):
    cart = fill_cart([("tomatoes", 1)])
    cart_service.add_item(cart, sample["products"]["tomatoes"], 2)

    assert cart.find_item(sample["products"]["tomatoes"].id).quantity == 3


def test_one_basket_means_one_partner_store(app, sample, fill_cart):
    from app.constants import CAT_DRY
    from app.extensions import db
    from app.models import PartnerStore, Product

    cart = fill_cart([("tomatoes", 2)])

    other = PartnerStore(
        name="Other Mart", gps_latitude=6.6, gps_longitude=3.35, commission_rate=0.05
    )
    db.session.add(other)
    db.session.flush()
    other_rice = Product(
        store_id=other.id, name="Other Rice", sku="OTH", category=CAT_DRY,
        unit="bag", price=20_000, stock_qty=10,
    )
    db.session.add(other_rice)
    db.session.flush()

    with pytest.raises(cart_service.CartError):
        cart_service.add_item(cart, other_rice, 1)


def test_removing_the_last_item_clears_the_store(app, sample, fill_cart):
    cart = fill_cart([("tomatoes", 2)])
    cart_service.remove_item(cart, sample["products"]["tomatoes"].id)

    assert cart.is_empty
    assert cart.store_id is None


def test_minimum_order_floor_is_enforced(app, sample, fill_cart):
    cart = fill_cart([("soap", 1), ("bleach", 1)])  # 4,650 NGN
    assert cart.subtotal < 15_000

    with pytest.raises(cart_service.MinimumOrderError) as exc:
        cart_service.validate_minimum(cart.subtotal)

    assert exc.value.minimum == 15_000
    assert exc.value.shortfall == round(15_000 - cart.subtotal, 2)


def test_minimum_order_state_reports_the_shortfall(app, sample, fill_cart):
    cart = fill_cart([("tomatoes", 1)])  # 9,000 NGN
    state = cart_service.minimum_order_state(cart.subtotal)

    assert state["met"] is False
    assert state["shortfall"] == 6_000
    assert state["target_aov"] == 35_000


def test_cross_sell_does_not_crash_on_a_populated_basket(
    app, sample, fill_cart, make_order
):
    """Regression: co-occurrence self-joins OrderItem, which raised
    InvalidRequestError the moment the basket held an item."""
    from app.extensions import db
    from app.models import User

    make_order([("tomatoes", 1), ("rice", 1)])  # the co-purchase signal

    shopper = User(
        name="Shopper", email="shopper@test.ng", phone="08030000009",
        user_type="consumer",
    )
    shopper.set_password("Password123!")
    db.session.add(shopper)
    db.session.flush()

    cart = cart_service.get_or_create_cart(shopper)
    cart_service.add_item(cart, sample["products"]["rice"], 1)

    suggestions = cart_service.frequently_bought_together(cart)

    names = {s["product"].name for s in suggestions}
    assert "Tomatoes (basket)" in names  # bought alongside rice elsewhere
    assert "Long Grain Rice (50kg)" not in names  # already in the basket
