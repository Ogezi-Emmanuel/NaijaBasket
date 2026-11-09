"""Public storefront: landing page and catalogue browsing."""

from flask import Blueprint, render_template, request
from sqlalchemy import func, or_, select

from ..constants import (
    CATEGORIES,
    CATEGORY_LABELS,
    CATEGORY_SAFETY,
)
from ..extensions import db
from ..models import Order, OrderItem, PartnerStore, Product

bp = Blueprint("storefront", __name__)

PAGE_SIZE = 24


def _active_stores():
    return list(
        db.session.execute(
            select(PartnerStore).where(PartnerStore.is_active.is_(True)).order_by(PartnerStore.name)
        ).scalars()
    )


def browse_products(
    *,
    query: str | None = None,
    category: str | None = None,
    store_id: int | None = None,
    sort: str = "popular",
    limit: int = PAGE_SIZE,
    offset: int = 0,
):
    stmt = select(Product).where(Product.is_active.is_(True))
    if category and category in CATEGORIES:
        stmt = stmt.where(Product.category == category)
    if store_id:
        stmt = stmt.where(Product.store_id == store_id)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(like),
                Product.nafdac_reg_no.ilike(like),
                Product.category.ilike(like),
            )
        )

    if sort == "price_asc":
        stmt = stmt.order_by(Product.price.asc())
    elif sort == "price_desc":
        stmt = stmt.order_by(Product.price.desc())
    else:
        # Popularity proxy: units sold across historical orders.
        sold = (
            select(OrderItem.product_id, func.sum(OrderItem.quantity).label("units"))
            .group_by(OrderItem.product_id)
            .subquery()
        )
        stmt = stmt.outerjoin(sold, sold.c.product_id == Product.id).order_by(
            func.coalesce(sold.c.units, 0).desc(), Product.name.asc()
        )

    stmt = stmt.limit(limit).offset(offset)
    return list(db.session.execute(stmt).scalars())


def category_counts() -> dict:
    rows = db.session.execute(
        select(Product.category, func.count(Product.id))
        .where(Product.is_active.is_(True))
        .group_by(Product.category)
    ).all()
    return {category: count for category, count in rows}


@bp.get("/")
def index():
    stores = _active_stores()
    featured = browse_products(limit=8)
    counts = category_counts()
    delivered = db.session.query(func.count(Order.id)).filter(Order.status == "delivered").scalar() or 0

    return render_template(
        "storefront/index.html",
        stores=stores,
        featured=featured,
        counts=counts,
        stats={
            "products": sum(counts.values()),
            "partners": len(stores),
            "orders_delivered": delivered,
            "merchant_sla": 15,
            "transit_sla": 45,
            "radius_km": 8,
        },
    )


@bp.get("/catalog")
def catalog():
    query = request.args.get("q", "").strip()
    category = request.args.get("category")
    sort = request.args.get("sort", "popular")
    store_id = request.args.get("store", type=int)

    products = browse_products(
        query=query, category=category, store_id=store_id, sort=sort, limit=48
    )
    stores = _active_stores()

    store_names = {store.id: store.name for store in stores}
    for product in products:
        product.store_name = store_names.get(product.store_id)

    return render_template(
        "storefront/catalog.html",
        products=products,
        stores=stores,
        counts=category_counts(),
        query=query,
        category=category,
        sort=sort,
        store_id=store_id,
        safety_labels={c: CATEGORY_SAFETY.get(c, "") for c in CATEGORIES},
        category_labels=CATEGORY_LABELS,
    )


@bp.get("/how-it-works")
def how_it_works():
    return render_template("storefront/how_it_works.html")


@bp.get("/offline")
def offline():
    """Service-worker fallback shell for dropped connections."""
    return render_template("storefront/offline.html")
