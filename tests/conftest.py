"""Shared fixtures for the NaijaBasket test-suite."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.constants import CAT_CARE, CAT_CLEAN, CAT_DRY, CAT_FRESH  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import PartnerStore, Product, User  # noqa: E402
from app.services import cart as cart_service  # noqa: E402

PASSWORD = "Password123!"


@pytest.fixture()
def app():
    """An isolated app bound to an in-memory database."""
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def make_user(email, role, *, name="Test User", phone="08030000001",
              store_id=None, coords=None, address=None, password=PASSWORD):
    user = User(name=name, email=email, phone=phone, user_type=role, store_id=store_id)
    user.set_password(password)
    if coords:
        user.gps_latitude, user.gps_longitude = coords
    if address:
        user.address = address
    _db.session.add(user)
    _db.session.flush()
    return user


def make_store(name="Test Mart Lekki", lat=6.4478, lng=3.4723, rate=0.055):
    store = PartnerStore(
        name=name,
        gps_latitude=lat,
        gps_longitude=lng,
        commission_rate=rate,
        sla_rating=100.0,
        nafdac_compliant=True,
    )
    store.address = "1 Test Way, Lekki, Lagos"
    _db.session.add(store)
    _db.session.flush()
    return store


def make_product(store, name, category, price, nafdac=None, stock=60):
    product = Product(
        store_id=store.id,
        name=name,
        sku=name[:3].upper(),
        category=category,
        unit="unit",
        price=price,
        nafdac_reg_no=nafdac,
        stock_qty=stock,
    )
    _db.session.add(product)
    _db.session.flush()
    return product


@pytest.fixture()
def sample(app):
    """One Lekki partner, a four-item catalogue and the three role accounts."""
    store = make_store()
    products = {
        "tomatoes": make_product(store, "Tomatoes (basket)", CAT_FRESH, 9_000, "A1-2210"),
        "rice": make_product(store, "Long Grain Rice (50kg)", CAT_DRY, 41_000, "01-4457", stock=25),
        "soap": make_product(store, "Bath Soap", CAT_CARE, 2_700, None, stock=80),
        "bleach": make_product(store, "Hypo Bleach", CAT_CLEAN, 1_950, None, stock=80),
    }
    users = {
        "consumer": make_user(
            "consumer@test.ng", "consumer", name="Test Consumer",
            coords=(6.4419, 3.4799), address="24B Freedom Way, Lekki, Lagos",
        ),
        "merchant": make_user(
            "merchant@test.ng", "merchant", name="Test Merchant",
            phone="08030000003", store_id=store.id,
        ),
        "admin": make_user(
            "admin@test.ng", "admin", name="Test Admin", phone="08030000004",
        ),
    }
    _db.session.commit()
    return {"store": store, "products": products, "users": users}


@pytest.fixture()
def login(client):
    def _login(email, password=PASSWORD):
        return client.post(
            "/auth/login", data={"email": email, "password": password}
        )

    return _login


@pytest.fixture()
def fill_cart(app, sample):
    """Add [(product_key, quantity), ...] to a user's basket."""

    def _fill(items, user=None, commit=True):
        user = user or sample["users"]["consumer"]
        cart = cart_service.get_or_create_cart(user)
        for key, quantity in items:
            cart_service.add_item(cart, sample["products"][key], quantity)
        if commit:
            _db.session.commit()
        return cart

    return _fill


@pytest.fixture()
def make_order(app, sample, fill_cart):
    """Create a real order through the same service the checkout uses."""
    from app.services import orders as orders_service

    def _make(items, user=None, latitude=6.4419, longitude=3.4799):
        user = user or sample["users"]["consumer"]
        cart = fill_cart(items, user=user)
        return orders_service.create_order_from_cart(
            user,
            cart,
            delivery_address="24B Freedom Way, Lekki, Lagos",
            latitude=latitude,
            longitude=longitude,
            channel="transfer",
            gateway="paystack",
        )

    return _make
