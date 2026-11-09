"""Registration, session handling and role-based routing."""

from app.extensions import db
from app.models import User


def test_register_creates_a_consumer_and_seals_the_address(app, client):
    response = client.post(
        "/auth/register",
        data={
            "name": "Ada Obi",
            "email": "ada@example.ng",
            "phone": "08031234567",
            "password": "Password123!",
            "confirm_password": "Password123!",
            "address": "12 Admiralty Way, Lekki, Lagos",
        },
    )
    assert response.status_code == 302

    user = db.session.query(User).filter(User.email == "ada@example.ng").first()
    assert user is not None
    assert user.user_type == "consumer"
    assert user.address == "12 Admiralty Way, Lekki, Lagos"
    # NDPA 2023: the address is encrypted at rest.
    assert user.address_encrypted != "12 Admiralty Way, Lekki, Lagos"


def test_register_rejects_a_weak_password(app, client):
    response = client.post(
        "/auth/register",
        data={
            "name": "Ada Obi",
            "email": "weak@example.ng",
            "phone": "08031234567",
            "password": "short",
            "confirm_password": "short",
        },
    )
    assert response.status_code == 200
    assert b"at least 8" in response.data.lower()


def test_login_rejects_a_bad_password(app, client, sample):
    response = client.post(
        "/auth/login", data={"email": "consumer@test.ng", "password": "wrong-password"}
    )
    assert response.status_code == 200
    assert b"Invalid email or password" in response.data


def test_login_routes_each_role_to_its_console(app, client, sample, login):
    merchant = login("merchant@test.ng")
    assert merchant.status_code == 302
    assert "/merchant" in merchant.headers["Location"]


def test_consumer_cannot_open_the_merchant_console(app, client, sample, login):
    login("consumer@test.ng")
    assert client.get("/merchant/").status_code == 403


def test_consumer_cannot_open_the_admin_console(app, client, sample, login):
    login("consumer@test.ng")
    assert client.get("/admin/dashboard").status_code == 403


def test_anonymous_visitors_are_sent_to_login(client):
    response = client.get("/orders/")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]
