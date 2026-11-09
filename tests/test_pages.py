"""Page rendering smoke tests across every role surface."""

import pytest

PUBLIC_PAGES = [
    "/",
    "/catalog",
    "/how-it-works",
    "/offline",
    "/auth/login",
    "/auth/register",
    "/api/health",
    "/api/config",
]

PRIVATE_PAGES = [
    "/orders/",
    "/checkout/",
    "/subscription/",
    "/merchant/",
    "/admin/dashboard",
]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_health_api_reports_the_order_floor(client):
    payload = client.get("/api/health").get_json()
    assert payload["ok"] is True
    assert payload["min_order_value"] == 15_000


@pytest.mark.parametrize("path", PRIVATE_PAGES)
def test_private_pages_redirect_anonymous_visitors(client, path):
    assert client.get(path).status_code == 302


def test_consumer_area_renders_for_a_buyer(app, client, sample, login):
    login("consumer@test.ng")
    assert client.get("/orders/").status_code == 200
    assert client.get("/checkout/").status_code == 200
    assert client.get("/subscription/").status_code == 200
    assert client.get("/auth/profile").status_code == 200


def test_merchant_console_renders(app, client, sample, login):
    login("merchant@test.ng")
    assert client.get("/merchant/").status_code == 200
    assert client.get("/merchant/orders").status_code == 200
    assert client.get("/merchant/inventory").status_code == 200
    assert client.get("/merchant/dispatch").status_code == 200


def test_admin_console_renders(app, client, sample, login):
    login("admin@test.ng")
    for path in ("/admin/dashboard", "/admin/partners", "/admin/finance", "/admin/orders"):
        assert client.get(path).status_code == 200, path


def test_unknown_page_returns_the_404_shell(client):
    assert client.get("/this-page-does-not-exist").status_code == 404


def test_data_backed_pages_render_the_shared_macros(app, client, sample, login, make_order):
    """Macros are pulled in with `{% from %}`, which is evaluated without the
    render context - so the label tables they read must live in the Jinja
    environment, not a context processor. An empty database hides this."""
    make_order([("tomatoes", 2), ("rice", 1)])

    storefront = client.get("/").get_data(as_text=True)
    assert 'class="product-card"' in storefront
    assert "Tomatoes (basket)" in storefront
    assert 'class="product-card"' in client.get("/catalog").get_data(as_text=True)

    login("consumer@test.ng")
    assert "Awaiting payment" in client.get("/orders/").get_data(as_text=True)

    # /auth/login redirects an already-authenticated visitor, so sign out first.
    client.post("/auth/logout")
    login("admin@test.ng")
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    dashboard = response.get_data(as_text=True)
    assert 'class="mode-banner' in dashboard
    assert "Recovery funnel" in dashboard


def test_catalogue_photography_is_publicly_hosted(app):
    """No catalogue asset may depend on an internal image generator."""
    from app.seed import PRODUCTS

    photos = 0
    for items in PRODUCTS.values():
        for name, _price, _unit, _nafdac, photo in items:
            assert photo.startswith("https://"), name
            assert "text_to_image" not in photo, name
            photos += 1
    assert photos == 33
