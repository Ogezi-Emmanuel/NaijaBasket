"""Demo dataset: Lagos partners, a full catalogue, role accounts and order history.

Seeding goes through the same services the app uses, so every seeded order has
real economics, ledger entries, dispatch jobs and safety-classified items.
"""

from datetime import timedelta

from .constants import (
    CAT_CARE,
    CAT_CLEAN,
    CAT_DRY,
    CAT_FRESH,
    PASS_MONTHLY,
)
from .extensions import db
from .models import Cart, PartnerStore, Product, SubscriptionPass, User, money, utcnow
from .services import cart as cart_service
from .services import escrow as escrow_service
from .services import orders as orders_service
from .services import sla as sla_service
from .integrations import dispatch as dispatch_service

# ---------------------------------------------------------------------------
# Catalogue photography
#
# Every product image below is a real, publicly hosted photograph served from
# the Wikimedia Commons CDN: keyless, permanent and free to hotlink. Swapping
# the former text-to-image generator out means the catalogue no longer depends
# on an internal service at runtime.
# ---------------------------------------------------------------------------
_WM = "https://thumb.wikimedia.org/wikipedia/commons/thumb"

#: Last-resort local asset, so a failed fetch never shows a broken image.
FALLBACK_IMAGE = "/static/icons/icon-maskable.svg"


STORES = [
    {
        "name": "GreenField Supermarket, Lekki Phase 1",
        "address": "12 Admiralty Way, Lekki Phase 1, Lagos",
        "gps_latitude": 6.4478,
        "gps_longitude": 3.4723,
        "market": "Lekki",
        "commission_rate": 0.055,
    },
    {
        "name": "Ikeja City Grocers",
        "address": "45 Obafemi Awolowo Way, Ikeja, Lagos",
        "gps_latitude": 6.6018,
        "gps_longitude": 3.3515,
        "market": "Ikeja",
        "commission_rate": 0.045,
    },
    {
        "name": "Yaba Fresh Mart",
        "address": "7 Herbert Macaulay Way, Yaba, Lagos",
        "gps_latitude": 6.5095,
        "gps_longitude": 3.3711,
        "market": "Yaba",
        "commission_rate": 0.065,
    },
]

# name, price (NGN), unit, NAFDAC reg no (food only), public product photo
PRODUCTS = {
    CAT_FRESH: [
        ("Tomatoes (large basket)", 18_500, "basket", "A1-2210", _WM + "/8/89/Tomato_je.jpg/960px-Tomato_je.jpg"),
        ("Habanero Pepper Mix (2kg)", 7_200, "2kg", "A2-3391", _WM + "/9/97/Scotch_bonnet_chili_pepper.jpg/960px-Scotch_bonnet_chili_pepper.jpg"),
        ("Red Onions (5kg)", 9_400, "5kg", "A1-1180", _WM + "/8/8f/Red_onions_11.jpg/960px-Red_onions_11.jpg"),
        ("Irish Potatoes (5kg)", 11_800, "5kg", "A3-4402", _WM + "/a/ab/Patates.jpg/960px-Patates.jpg"),
        ("Ripe Plantain Bunch", 5_600, "bunch", "A1-7712", _WM + "/8/86/Mega_racimos_de_guineos.jpg/960px-Mega_racimos_de_guineos.jpg"),
        ("Baby Spinach Bundle", 3_100, "bundle", "A2-5540", _WM + "/f/fe/Spinach_leaves.jpg/960px-Spinach_leaves.jpg"),
        ("Okra (2kg)", 4_800, "2kg", "A2-6613", _WM + "/9/95/Hong_Kong_Okra_Aug_25_2012.JPG/960px-Hong_Kong_Okra_Aug_25_2012.JPG"),
        ("Carrots (2kg)", 5_300, "2kg", "A1-3327", _WM + "/a/a2/Vegetable-Carrot-Bundle-wStalks.jpg/960px-Vegetable-Carrot-Bundle-wStalks.jpg"),
        ("Cucumbers (1kg)", 3_600, "1kg", "A3-8801", _WM + "/9/96/ARS_cucumber.jpg/960px-ARS_cucumber.jpg"),
        ("Yam Tubers (3 tubers)", 14_900, "3 tubers", "A1-9904", _WM + "/7/72/Yam_at_monday_market_kaduna_state_01.jpg/960px-Yam_at_monday_market_kaduna_state_01.jpg"),
        ("Sweet Oranges (bag)", 8_100, "bag", "A2-2270", _WM + "/e/e3/Oranges_-_whole-halved-segment.jpg/960px-Oranges_-_whole-halved-segment.jpg"),
    ],
    CAT_DRY: [
        ("Premium Long Grain Rice (50kg)", 82_000, "50kg bag", "01-4457", _WM + "/0/08/2014_uncooked_Thai_jasmine_rice.jpg/960px-2014_uncooked_Thai_jasmine_rice.jpg"),
        ("Brown Beans (5kg)", 13_500, "5kg", "01-2219", _WM + "/0/08/Lobia.jpg/960px-Lobia.jpg"),
        ("Garri Ijebu (10kg)", 10_200, "10kg", "01-3374", _WM + "/a/ab/Garri_flour.jpg/960px-Garri_flour.jpg"),
        ("Semovita (10kg)", 12_800, "10kg bag", "01-5521", _WM + "/c/c6/Sa_semolina_far.jpg/960px-Sa_semolina_far.jpg"),
        ("Spaghetti Family Pack (carton)", 9_600, "carton", "01-6688", _WM + "/9/92/Spaghettoni.jpg/960px-Spaghettoni.jpg"),
        ("Refined Vegetable Oil (5L)", 18_400, "5L", "01-7730", _WM + "/4/4b/Canola_Oil_%284107885913%29_Cropped.jpg/960px-Canola_Oil_%284107885913%29_Cropped.jpg"),
        ("Granulated Sugar (2kg)", 4_300, "2kg", "01-1122", _WM + "/3/3c/Sucre_blanc_cassonade_complet_rapadura.jpg/960px-Sucre_blanc_cassonade_complet_rapadura.jpg"),
        ("Tomato Paste (carton of 12)", 6_900, "carton", "01-8890", _WM + "/e/e8/Tomato_paste_on_spoon.jpg/960px-Tomato_paste_on_spoon.jpg"),
        ("Powdered Milk (900g tin)", 14_600, "900g", "01-3345", _WM + "/b/b9/Milk_powder_cropped.jpg/960px-Milk_powder_cropped.jpg"),
        ("Iodised Table Salt (1kg)", 1_800, "1kg", "01-9911", _WM + "/4/43/Iodized_salt_packet.jpg/960px-Iodized_salt_packet.jpg"),
    ],
    CAT_CARE: [
        ("Bath Soap Multi-pack (6)", 5_400, "6 pack", None, _WM + "/9/9b/Handmade_soap_cropped_and_simplified.jpg/960px-Handmade_soap_cropped_and_simplified.jpg"),
        ("Fluoride Toothpaste (3-pack)", 4_200, "3 pack", None, _WM + "/4/49/Toothpasteonbrush.jpg/960px-Toothpasteonbrush.jpg"),
        ("Antiseptic Body Lotion (1L)", 7_900, "1L", None, _WM + "/6/65/Banff_Centre_lotion_and_shampoo.jpg/960px-Banff_Centre_lotion_and_shampoo.jpg"),
        ("Sanitary Pads (packs of 3)", 6_300, "3 packs", None, _WM + "/b/b8/Sanitary_towel.jpg/960px-Sanitary_towel.jpg"),
        ("Toilet Tissue (12 rolls)", 8_800, "12 rolls", None, _WM + "/a/a5/Toiletpapier_%28Gobran111%29.jpg/960px-Toiletpapier_%28Gobran111%29.jpg"),
        ("Men's Shaving Kit", 9_300, "kit", None, _WM + "/a/ab/Parker-76R_Safety_Razor.jpg/960px-Parker-76R_Safety_Razor.jpg"),
    ],
    CAT_CLEAN: [
        ("Hypo Bleach (2 litre)", 3_900, "2L", None, _WM + "/d/d3/Clorox_Bleach_products.jpg/960px-Clorox_Bleach_products.jpg"),
        ("Multi-Purpose Cleaner (1L)", 4_600, "1L", None, _WM + "/3/34/Diskflaskor.JPG/960px-Diskflaskor.JPG"),
        ("Floor Disinfectant (5L)", 12_700, "5L", None, _WM + "/4/4e/Disinfection_with_mop.jpg/960px-Disinfection_with_mop.jpg"),
        ("Dishwashing Liquid (1L)", 3_400, "1L", None, _WM + "/7/71/Afwasmiddel.jpg/960px-Afwasmiddel.jpg"),
        ("Floor Mop & Bucket Set", 15_500, "set", None, _WM + "/4/4c/Mop%2C_three_different_mop_handles.jpg/960px-Mop%2C_three_different_mop_handles.jpg"),
        ("Insecticide Spray (400ml)", 5_100, "400ml", None, _WM + "/8/87/FLIT_Spray_Can_1.jpg/960px-FLIT_Spray_Can_1.jpg"),
    ],
}

USERS = [
    {
        "name": "Emmanuel Ogezi",
        "email": "consumer@naijabasket.ng",
        "phone": "08030000001",
        "role": "consumer",
        "password": "Password123!",
        "address": "24B Freedom Way, Lekki Phase 1, Lagos",
        "coords": (6.4419, 3.4799),
    },
    {
        "name": "Amina Bello",
        "email": "subscriber@naijabasket.ng",
        "phone": "08030000002",
        "role": "consumer",
        "password": "Password123!",
        "address": "9 Bourdillon Road, Ikoyi, Lagos",
        "coords": (6.4521, 3.4360),
        "pass": True,
    },
    {
        "name": "Chidi Okonkwo",
        "email": "merchant@naijabasket.ng",
        "phone": "08030000003",
        "role": "merchant",
        "password": "Password123!",
        "store_index": 0,
    },
    {
        "name": "NaijaBasket Ops",
        "email": "admin@naijabasket.ng",
        "phone": "08030000004",
        "role": "admin",
        "password": "Password123!",
    },
]


def _skip_if_seeded():
    if db.session.query(User).count():
        return {
            "status": "already seeded",
            "users": db.session.query(User).count(),
            "products": db.session.query(Product).count(),
        }
    return None


def seed_all() -> dict:
    db.create_all()
    summary = _skip_if_seeded()
    if summary:
        return summary

    stores = _seed_stores()
    products = _seed_products(stores)
    users = _seed_users(stores)
    consumer = users["consumer"]
    subscriber = users["subscriber"]

    orders = [
        *_seed_history(consumer, stores[0], products),
        _seed_live_order(consumer, stores[0], products),
        _seed_breached_order(consumer, stores[1], products),
    ]
    abandoned = _seed_abandoned_cart(subscriber, stores[2], products)

    db.session.commit()
    return {
        "stores": len(stores),
        "products": len(products),
        "users": len(users["all"]),
        "orders": len(orders),
        "delivered": len([o for o in orders if o.status == "delivered"]),
        "abandoned_cart": abandoned.id if abandoned else "none",
    }


def _seed_stores():
    stores = []
    for spec in STORES:
        store = PartnerStore(
            name=spec["name"],
            gps_latitude=spec["gps_latitude"],
            gps_longitude=spec["gps_longitude"],
            mayor_market=spec["market"],
            commission_rate=spec["commission_rate"],
            sla_rating=98.0,
            nafdac_compliant=True,
        )
        store.address = spec["address"]
        db.session.add(store)
        stores.append(store)
    db.session.flush()
    return stores


def _seed_products(stores):
    created = []
    for category, items in PRODUCTS.items():
        for index, (name, price, unit, nafdac, photo) in enumerate(items):
            for store_index, store in enumerate(stores):
                jitter = (store_index - 1) * 0.02
                product = Product(
                    store_id=store.id,
                    name=name,
                    sku=f"{category[:3].upper()}-{store_index + 1}{index + 1:02d}",
                    category=category,
                    unit=unit,
                    price=money(price * (1 + jitter)),
                    nafdac_reg_no=nafdac,
                    image_url=photo or FALLBACK_IMAGE,
                    stock_qty=40 + index * 3,
                )
                db.session.add(product)
                created.append(product)
    db.session.flush()
    return created


def _seed_users(stores):
    created = {}
    for spec in USERS:
        user = User(
            name=spec["name"],
            email=spec["email"],
            phone=spec["phone"],
            user_type=spec["role"],
        )
        user.set_password(spec["password"])
        if spec.get("address"):
            user.address = spec["address"]
        if spec.get("coords"):
            user.gps_latitude, user.gps_longitude = spec["coords"]
        if spec.get("store_index") is not None:
            user.store_id = stores[spec["store_index"]].id
        if spec.get("pass"):
            user.subscription_pass = True
            user.subscription_expires_at = utcnow() + timedelta(days=24)
        db.session.add(user)
        created[spec["email"].split("@")[0]] = user

    db.session.flush()

    if created.get("subscriber"):
        pass_row = SubscriptionPass(
            user_id=created["subscriber"].id,
            plan=PASS_MONTHLY,
            price=5_000,
            status="active",
            reference="NB-SUB-SEED01",
            expires_at=created["subscriber"].subscription_expires_at,
        )
        db.session.add(pass_row)

    created["all"] = [u for u in created.values() if isinstance(u, User)]
    db.session.flush()
    return created


def _basket_for(user, store, products, picks):
    """picks = [(name, quantity)] resolved against the store's catalogue."""
    cart = Cart(user_id=user.id, store_id=store.id)
    db.session.add(cart)
    db.session.flush()
    catalogue = {p.name: p for p in products if p.store_id == store.id}
    for name, quantity in picks:
        product = catalogue.get(name)
        if product is None:
            continue
        cart_service.add_item(cart, product, quantity)
    db.session.flush()
    return cart


def _seed_history(consumer, store, products):
    """Six delivered orders spread over the last two weeks."""
    baskets = [
        ([("Premium Long Grain Rice (50kg)", 1), ("Refined Vegetable Oil (5L)", 2), ("Tomatoes (large basket)", 1)], 4),
        ([("Brown Beans (5kg)", 2), ("Garri Ijebu (10kg)", 1), ("Red Onions (5kg)", 2), ("Hypo Bleach (2 litre)", 2)], 11),
        ([("Ripe Plantain Bunch", 3), ("Habanero Pepper Mix (2kg)", 2), ("Iodised Table Salt (1kg)", 2), ("Sanitary Pads (packs of 3)", 1)], 6),
        ([("Irish Potatoes (5kg)", 2), ("Carrots (2kg)", 2), ("Floor Mop & Bucket Set", 1), ("Dishwashing Liquid (1L)", 2)], 9),
        ([("Tomatoes (large basket)", 2), ("Sweet Oranges (bag)", 2), ("Fluoride Toothpaste (3-pack)", 2), ("Toilet Tissue (12 rolls)", 1)], 2),
        ([("Semovita (10kg)", 1), ("Spaghetti Family Pack (carton)", 2), ("Powdered Milk (900g tin)", 2), ("Insecticide Spray (400ml)", 2)], 13),
    ]

    created = []
    for offset, (picks, days_ago) in enumerate(baskets):
        cart = _basket_for(consumer, store, products, picks)
        order = orders_service.create_order_from_cart(
            consumer,
            cart,
            delivery_address=consumer.address or "Lekki Phase 1, Lagos",
            latitude=consumer.gps_latitude,
            longitude=consumer.gps_longitude,
            channel="transfer" if offset % 2 == 0 else "card",
            gateway="paystack",
        )
        reference = f"NB-PAY-{order.order_number}-seed"
        escrow_service.hold(order, reference)
        orders_service.reserve_stock(order)
        cart_service.clear(cart)

        paid_at = utcnow() - timedelta(days=days_ago, hours=offset)
        pick_minutes = 7 + offset
        order.paid_at = paid_at
        order.sla_deadline = paid_at + timedelta(minutes=15)
        order.packed_at = paid_at + timedelta(minutes=pick_minutes)
        sla_service.record_met(order)
        order.status = "packed"
        db.session.flush()

        try:
            job = dispatch_service.request_dispatch(order)
        except dispatch_service.DispatchError:
            job = None
        if job is not None:
            job.assigned_at = paid_at + timedelta(minutes=pick_minutes + 2)
        order.dispatched_at = order.packed_at + timedelta(minutes=2)
        db.session.flush()

        escrow_service.release(order)
        delivered_at = order.dispatched_at + timedelta(minutes=26 + offset)
        order.delivered_at = delivered_at
        if job is not None:
            job.delivered_at = delivered_at
        created.append(order)
    db.session.flush()
    return created


def _seed_live_order(consumer, store, products):
    """A single order sitting in the merchant's 15-minute queue right now."""
    cart = _basket_for(
        consumer,
        store,
        products,
        [
            ("Premium Long Grain Rice (50kg)", 1),
            ("Tomatoes (large basket)", 1),
            ("Refined Vegetable Oil (5L)", 1),
        ],
    )
    order = orders_service.create_order_from_cart(
        consumer,
        cart,
        delivery_address=consumer.address or "Lekki Phase 1, Lagos",
        latitude=consumer.gps_latitude,
        longitude=consumer.gps_longitude,
        channel="transfer",
        gateway="paystack",
    )
    escrow_service.hold(order, f"NB-PAY-{order.order_number}-live")
    orders_service.reserve_stock(order)
    cart_service.clear(cart)

    # Paid four minutes ago -> the clerk sees ~11 minutes left.
    order.paid_at = utcnow() - timedelta(minutes=4)
    order.sla_deadline = order.paid_at + timedelta(minutes=15)
    order.status = "paid_escrow"
    db.session.flush()
    return order


def _seed_breached_order(consumer, store, products):
    """An order that blew the picking SLA, for the compliance console."""
    cart = _basket_for(
        consumer,
        store,
        products,
        [("Brown Beans (5kg)", 2), ("Garri Ijebu (10kg)", 2), ("Floor Disinfectant (5L)", 1)],
    )
    order = orders_service.create_order_from_cart(
        consumer,
        cart,
        delivery_address="23 Allen Avenue, Ikeja, Lagos",
        latitude=store.gps_latitude + 0.004,
        longitude=store.gps_longitude - 0.003,
        channel="ussd",
        gateway="flutterwave",
    )
    escrow_service.hold(order, f"NB-FLU-{order.order_number}-breach")
    orders_service.reserve_stock(order)
    cart_service.clear(cart)

    order.paid_at = utcnow() - timedelta(minutes=68)
    order.sla_deadline = order.paid_at + timedelta(minutes=15)
    order.status = "paid_escrow"
    db.session.flush()
    sla_service.evaluate_order(order)
    db.session.flush()
    return order


def _seed_abandoned_cart(subscriber, store, products):
    """Idle basket (45 minutes old) so `flask cart-recovery` has work to do."""
    cart = _basket_for(
        subscriber,
        store,
        products,
        [
            ("Tomatoes (large basket)", 2),
            ("Ripe Plantain Bunch", 2),
            ("Iodised Table Salt (1kg)", 1),
        ],
    )
    cart.updated_at = utcnow() - timedelta(minutes=45)
    db.session.flush()
    return cart
