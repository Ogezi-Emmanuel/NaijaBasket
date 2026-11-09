"""Order construction: server-authoritative validation and frozen snapshots."""

import pytest

from app.extensions import db
from app.services import cart as cart_service
from app.services import orders as orders_service


def test_order_number_format(app, sample, make_order):
    order = make_order([("tomatoes", 2)])
    assert order.order_number.startswith("NB-")
    assert len(order.order_number.split("-")) == 3


def test_sub_minimum_order_is_rejected(app, sample, fill_cart):
    user = sample["users"]["consumer"]
    cart = fill_cart([("soap", 1), ("bleach", 1)])  # 4,650 NGN

    with pytest.raises(cart_service.MinimumOrderError):
        orders_service.create_order_from_cart(
            user, cart,
            delivery_address="24B Freedom Way, Lekki, Lagos",
            latitude=6.4419, longitude=3.4799,
        )


def test_out_of_radius_order_is_rejected(app, sample, fill_cart):
    user = sample["users"]["consumer"]
    cart = fill_cart([("tomatoes", 2)])  # 18,000 NGN, clears the floor

    with pytest.raises(orders_service.OrderError) as exc:
        # Ikeja is ~17 km from the Lekki partner: beyond the 8 km radius.
        orders_service.create_order_from_cart(
            user, cart,
            delivery_address="23 Allen Avenue, Ikeja, Lagos",
            latitude=6.6018, longitude=3.3515,
        )
    assert "km away" in str(exc.value)


def test_items_are_frozen_into_the_order(app, sample, make_order):
    order = make_order([("tomatoes", 2), ("rice", 1)])

    assert {item.name for item in order.items} == {
        "Tomatoes (basket)", "Long Grain Rice (50kg)"
    }
    assert order.gmv_amount == 9_000 * 2 + 41_000


def test_economics_columns_are_populated(app, sample, make_order):
    order = make_order([("tomatoes", 2)])  # 18,000 NGN

    assert order.commission_rate == 0.055
    assert order.commission_amount == round(order.gmv_amount * 0.055, 2)
    assert order.gross_revenue == round(
        order.commission_amount + order.convenience_fee + order.subscription_allocation, 2
    )


def test_reserve_stock_decrements_inventory(app, sample, make_order):
    rice = sample["products"]["rice"]
    before = rice.stock_qty

    order = make_order([("rice", 3)])
    db.session.commit()
    orders_service.reserve_stock(order)

    assert rice.stock_qty == before - 3
