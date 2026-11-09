"""SQLAlchemy data model.

Mirrors the PRD ERD (users, partner_stores, products, orders, order_items) and
adds the operational tables required by the flows it specifies: carts, escrow
ledger, dispatch jobs, SLA audit events and subscription passes.
"""

from datetime import datetime, timezone, timedelta

from flask_login import UserMixin

from .constants import (
    CATEGORY_SAFETY,
    ORDER_STATUS_LABELS,
    PICK_LANE_ORDER,
    SAFETY_CHEMICAL,
    TRACKER_STEPS,
    WA_STATUS_LABELS,
)
from .extensions import db, login_manager
from .security import (
    decrypt_pii,
    encrypt_pii,
    hash_delivery_pin,
    hash_password,
    pin_matches,
    verify_password,
)


def utcnow() -> datetime:
    """Naive UTC - keeps SQLite and Postgres comparisons identical."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(value: float) -> float:
    """Two-decimal NGN rounding used by every financial calculation."""
    return round(float(value or 0) + 1e-9, 2)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(32), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    user_type = db.Column(db.String(20), nullable=False, default="consumer", index=True)
    store_id = db.Column(db.Integer, db.ForeignKey("partner_stores.id"), nullable=True)

    subscription_pass = db.Column(db.Boolean, nullable=False, default=False)
    subscription_expires_at = db.Column(db.DateTime, nullable=True)

    #: Delivery address is PII -> AES-256-GCM at rest (NDPA 2023).
    address_encrypted = db.Column(db.Text, nullable=True)
    gps_latitude = db.Column(db.Float, nullable=True)
    gps_longitude = db.Column(db.Float, nullable=True)

    is_active_flag = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    store = db.relationship("PartnerStore", backref="staff", foreign_keys=[store_id])
    carts = db.relationship("Cart", back_populates="user", cascade="all, delete-orphan")
    orders = db.relationship(
        "Order", back_populates="user", cascade="all, delete-orphan",
        foreign_keys="Order.user_id",
    )
    passes = db.relationship(
        "SubscriptionPass", back_populates="user", cascade="all, delete-orphan"
    )

    # -- credentials --------------------------------------------------------
    def set_password(self, raw: str) -> None:
        self.password_hash = hash_password(raw)

    def check_password(self, raw: str) -> bool:
        return verify_password(self.password_hash, raw)

    # -- PII ----------------------------------------------------------------
    @property
    def address(self):
        return decrypt_pii(self.address_encrypted)

    @address.setter
    def address(self, value):
        self.address_encrypted = encrypt_pii(value)

    # -- roles --------------------------------------------------------------
    @property
    def is_consumer(self) -> bool:
        return self.user_type == "consumer"

    @property
    def is_merchant(self) -> bool:
        return self.user_type == "merchant"

    @property
    def is_admin(self) -> bool:
        return self.user_type == "admin"

    @property
    def is_active(self) -> bool:  # Flask-Login
        return bool(self.is_active_flag)

    @property
    def subscription_active(self) -> bool:
        return bool(
            self.subscription_pass
            and self.subscription_expires_at
            and self.subscription_expires_at > utcnow()
        )

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in (self.name or "?").split()[:2]).upper()

    def has_role(self, *roles: str) -> bool:
        return self.user_type in roles

    def __repr__(self):
        return f"<User {self.email} ({self.user_type})>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Supply side
# ---------------------------------------------------------------------------
class PartnerStore(db.Model):
    __tablename__ = "partner_stores"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    address_encrypted = db.Column(db.Text, nullable=True)
    gps_latitude = db.Column(db.Float, nullable=False)
    gps_longitude = db.Column(db.Float, nullable=False)
    sla_rating = db.Column(db.Float, nullable=False, default=100.0)
    sla_target_minutes = db.Column(db.Integer, nullable=False, default=15)
    commission_rate = db.Column(db.Float, nullable=False, default=0.055)
    mayor_market = db.Column(db.String(120), nullable=True)
    nafdac_compliant = db.Column(db.Boolean, nullable=False, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    products = db.relationship("Product", back_populates="store", cascade="all, delete-orphan")
    orders = db.relationship("Order", back_populates="store")
    sla_events = db.relationship("SlaEvent", back_populates="store", cascade="all, delete-orphan")

    @property
    def address(self):
        return decrypt_pii(self.address_encrypted)

    @address.setter
    def address(self, value):
        self.address_encrypted = encrypt_pii(value)

    @property
    def coordinates(self) -> tuple[float, float]:
        return (self.gps_latitude, self.gps_longitude)

    def __repr__(self):
        return f"<PartnerStore {self.name}>"


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("partner_stores.id"), nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    sku = db.Column(db.String(48), nullable=True)
    category = db.Column(db.String(40), nullable=False, index=True)
    unit = db.Column(db.String(24), nullable=False, default="unit")
    price = db.Column(db.Float, nullable=False)
    nafdac_reg_no = db.Column(db.String(64), nullable=True)
    image_url = db.Column(db.String(600), nullable=True)
    stock_qty = db.Column(db.Integer, nullable=False, default=100)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    store = db.relationship("PartnerStore", back_populates="products")

    @property
    def safety_category(self) -> str:
        # Unknown categories fail safe into the sealed lane (never beside food).
        return CATEGORY_SAFETY.get(self.category, SAFETY_CHEMICAL)

    @property
    def requires_nafdac(self) -> bool:
        return self.safety_category in ("perishable", "dry_food")

    @property
    def nafdac_verified(self) -> bool:
        return bool(self.nafdac_reg_no) or not self.requires_nafdac

    @property
    def in_stock(self) -> bool:
        return self.stock_qty > 0 and self.is_active

    def __repr__(self):
        return f"<Product {self.name} NGN {self.price}>"


# ---------------------------------------------------------------------------
# Demand side
# ---------------------------------------------------------------------------
class Cart(db.Model):
    __tablename__ = "carts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey("partner_stores.id"), nullable=True)

    #: Cart recovery (FR-06)
    recovery_token = db.Column(db.String(64), nullable=True)
    recovery_discount = db.Column(db.Float, nullable=False, default=0.0)
    recovery_sent_at = db.Column(db.DateTime, nullable=True)
    recovery_channel = db.Column(db.String(24), nullable=True)

    #: Delivery state of the recovery message, as reported by the BSP.
    recovery_provider = db.Column(db.String(24), nullable=True)
    recovery_message_id = db.Column(db.String(128), nullable=True, index=True)
    recovery_status = db.Column(db.String(16), nullable=True)
    recovery_delivered_at = db.Column(db.DateTime, nullable=True)
    recovery_read_at = db.Column(db.DateTime, nullable=True)
    recovery_error = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", back_populates="carts")
    items = db.relationship("CartItem", back_populates="cart", cascade="all, delete-orphan")

    # -- derived ------------------------------------------------------------
    @property
    def subtotal(self) -> float:
        return money(sum(item.line_total for item in self.items))

    @property
    def recovery_status_label(self) -> str:
        return WA_STATUS_LABELS.get(self.recovery_status or "", "Not sent")

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def store(self):
        if self.store_id:
            return db.session.get(PartnerStore, self.store_id)
        if self.items:
            return self.items[0].product.store
        return None

    @property
    def is_empty(self) -> bool:
        return not self.items

    @property
    def is_abandoned(self) -> bool:
        from flask import current_app

        if self.is_empty or self.recovery_sent_at:
            return False
        idle = utcnow() - self.updated_at
        return idle >= timedelta(minutes=current_app.config["CART_RECOVERY_MINUTES"])

    def find_item(self, product_id: int):
        return next((i for i in self.items if i.product_id == product_id), None)

    def __repr__(self):
        return f"<Cart #{self.id} user={self.user_id} items={len(self.items)}>"


class CartItem(db.Model):
    __tablename__ = "cart_items"

    id = db.Column(db.Integer, primary_key=True)
    cart_id = db.Column(db.Integer, db.ForeignKey("carts.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    added_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    cart = db.relationship("Cart", back_populates="items")
    product = db.relationship("Product")

    @property
    def line_total(self) -> float:
        return money(self.product.price * self.quantity)

    @property
    def safety_category(self) -> str:
        return self.product.safety_category


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(24), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey("partner_stores.id"), nullable=False, index=True)

    gmv_amount = db.Column(db.Float, nullable=False, default=0.0)
    commission_rate = db.Column(db.Float, nullable=False, default=0.055)
    commission_amount = db.Column(db.Float, nullable=False, default=0.0)
    convenience_fee = db.Column(db.Float, nullable=False, default=0.0)
    subscription_allocation = db.Column(db.Float, nullable=False, default=0.0)
    gross_revenue = db.Column(db.Float, nullable=False, default=0.0)
    gateway_fee = db.Column(db.Float, nullable=False, default=0.0)
    processing_cost = db.Column(db.Float, nullable=False, default=0.0)
    logistics_cost = db.Column(db.Float, nullable=False, default=0.0)
    net_contribution_margin = db.Column(db.Float, nullable=False, default=0.0)
    recovery_discount = db.Column(db.Float, nullable=False, default=0.0)

    delivery_address_encrypted = db.Column(db.Text, nullable=True)
    delivery_latitude = db.Column(db.Float, nullable=True)
    delivery_longitude = db.Column(db.Float, nullable=True)
    delivery_window = db.Column(db.String(40), nullable=False, default="asap")

    status = db.Column(db.String(24), nullable=False, default="pending_payment", index=True)
    payment_escrow_status = db.Column(db.String(16), nullable=False, default="pending")
    payment_gateway = db.Column(db.String(24), nullable=True)
    payment_channel = db.Column(db.String(24), nullable=True)
    payment_reference = db.Column(db.String(80), nullable=True, index=True)

    delivery_pin_hash = db.Column(db.String(128), nullable=True)
    delivery_pin_salt = db.Column(db.String(32), nullable=True)
    delivery_pin_encrypted = db.Column(db.Text, nullable=True)
    delivery_pin_attempts = db.Column(db.Integer, nullable=False, default=0)
    delivery_pin_locked = db.Column(db.Boolean, nullable=False, default=False)

    sla_deadline = db.Column(db.DateTime, nullable=True)
    sla_breached = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)
    packed_at = db.Column(db.DateTime, nullable=True)
    dispatched_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", back_populates="orders", foreign_keys=[user_id])
    store = db.relationship("PartnerStore", back_populates="orders")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    dispatch_jobs = db.relationship(
        "DispatchJob", back_populates="order", cascade="all, delete-orphan",
        order_by="DispatchJob.id",
    )
    ledger_entries = db.relationship(
        "LedgerEntry", back_populates="order", cascade="all, delete-orphan",
        order_by="LedgerEntry.id",
    )
    sla_events = db.relationship(
        "SlaEvent", back_populates="order", cascade="all, delete-orphan",
        order_by="SlaEvent.id",
    )

    # -- PII ----------------------------------------------------------------
    @property
    def delivery_address(self):
        return decrypt_pii(self.delivery_address_encrypted)

    @delivery_address.setter
    def delivery_address(self, value):
        self.delivery_address_encrypted = encrypt_pii(value)

    # -- escrow PIN ---------------------------------------------------------
    def issue_delivery_pin(self, pin: str) -> None:
        import secrets as _secrets

        self.delivery_pin_salt = _secrets.token_hex(8)
        self.delivery_pin_hash = hash_delivery_pin(pin, self.delivery_pin_salt)
        self.delivery_pin_encrypted = encrypt_pii(pin)
        self.delivery_pin_attempts = 0
        self.delivery_pin_locked = False

    @property
    def delivery_pin(self):
        """Only ever rendered back to the order owner / admin."""
        return decrypt_pii(self.delivery_pin_encrypted)

    def verify_delivery_pin(self, candidate: str) -> bool:
        return pin_matches(candidate, self.delivery_pin_salt or "", self.delivery_pin_hash or "")

    # -- derived ------------------------------------------------------------
    @property
    def status_label(self) -> str:
        return ORDER_STATUS_LABELS.get(self.status, self.status)

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def latest_dispatch(self):
        return self.dispatch_jobs[-1] if self.dispatch_jobs else None

    @property
    def sla_seconds_remaining(self) -> int:
        if not self.sla_deadline or self.status not in ("paid_escrow", "packing"):
            return 0
        return max(0, int((self.sla_deadline - utcnow()).total_seconds()))

    @property
    def sla_state(self) -> str:
        from flask import current_app

        if self.sla_breached or self.status == "sla_breach":
            return "danger"
        if not self.sla_deadline or self.status not in ("paid_escrow", "packing"):
            return "muted"
        remaining = self.sla_seconds_remaining
        if remaining <= current_app.config["SLA_WARNING_MINUTES"] * 60:
            return "danger"
        if remaining <= current_app.config["MERCHANT_SLA_MINUTES"] * 60 / 2:
            return "warning"
        return "success"

    @property
    def tracker_steps(self):
        """[(label, reached_bool)] for the 4-step consumer tracker."""
        if self.status == "sla_breach":
            current = 1
        else:
            current = max(
                (idx for idx, (_, statuses) in enumerate(TRACKER_STEPS) if self.status in statuses),
                default=0,
            )
        return [(label, idx <= current) for idx, (label, _) in enumerate(TRACKER_STEPS)]

    @property
    def safety_lanes(self):
        """Pick lanes ordered so chemicals load last, away from food (FR-05)."""
        lanes: dict[str, list[OrderItem]] = {}
        for item in self.items:
            lanes.setdefault(item.safety_category, []).append(item)
        return [
            {
                "safety_category": lane,
                "items": lanes[lane],
                "line_total": money(sum(i.line_total for i in lanes[lane])),
            }
            for lane in PICK_LANE_ORDER
            if lane in lanes
        ]

    @property
    def cross_contamination_risk(self) -> bool:
        classes = {item.safety_category for item in self.items}
        return "chemical" in classes and bool({"perishable", "dry_food"} & classes)

    def __repr__(self):
        return f"<Order {self.order_number} {self.status} NGN {self.gmv_amount}>"


class OrderItem(db.Model):
    __tablename__ = "order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0.0)
    safety_category = db.Column(db.String(24), nullable=False)
    nafdac_reg_no = db.Column(db.String(64), nullable=True)

    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product")

    @property
    def line_total(self) -> float:
        return money(self.unit_price * self.quantity)

    @property
    def requires_nafdac(self) -> bool:
        return self.safety_category in ("perishable", "dry_food")


# ---------------------------------------------------------------------------
# Escrow ledger
# ---------------------------------------------------------------------------
class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    entry_type = db.Column(db.String(32), nullable=False)
    party = db.Column(db.String(24), nullable=False)  # platform | merchant | rider | buyer
    amount = db.Column(db.Float, nullable=False, default=0.0)
    reference = db.Column(db.String(80), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    order = db.relationship("Order", back_populates="ledger_entries")

    def __repr__(self):
        return f"<Ledger {self.entry_type} {self.party} NGN {self.amount}>"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
class DispatchJob(db.Model):
    __tablename__ = "dispatch_jobs"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    provider = db.Column(db.String(24), nullable=False)
    provider_ref = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(24), nullable=False, default="requested")
    mode = db.Column(db.String(16), nullable=False, default="sandbox")  # live | sandbox
    rider_name = db.Column(db.String(80), nullable=True)
    rider_phone = db.Column(db.String(32), nullable=True)
    vehicle = db.Column(db.String(40), nullable=True)
    distance_km = db.Column(db.Float, nullable=True)
    eta_minutes = db.Column(db.Integer, nullable=True)
    quote_amount = db.Column(db.Float, nullable=False, default=0.0)
    thermal_bag = db.Column(db.Boolean, nullable=False, default=True)
    payload = db.Column(db.Text, nullable=True)
    error = db.Column(db.String(255), nullable=True)

    requested_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    assigned_at = db.Column(db.DateTime, nullable=True)
    picked_up_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)

    order = db.relationship("Order", back_populates="dispatch_jobs")

    def __repr__(self):
        return f"<DispatchJob {self.provider}:{self.status}>"


# ---------------------------------------------------------------------------
# SLA audit
# ---------------------------------------------------------------------------
class SlaEvent(db.Model):
    __tablename__ = "sla_events"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey("partner_stores.id"), nullable=False, index=True)
    event_type = db.Column(db.String(24), nullable=False)  # warning | breach | met
    minutes_elapsed = db.Column(db.Float, nullable=False, default=0.0)
    detail = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    order = db.relationship("Order", back_populates="sla_events")
    store = db.relationship("PartnerStore", back_populates="sla_events")

    def __repr__(self):
        return f"<SlaEvent {self.event_type} order={self.order_id}>"


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
class SubscriptionPass(db.Model):
    __tablename__ = "subscription_passes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan = db.Column(db.String(32), nullable=False, default="monthly")
    price = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(16), nullable=False, default="active")
    reference = db.Column(db.String(80), nullable=True)
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", back_populates="passes")

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.expires_at > utcnow()

    def __repr__(self):
        return f"<SubscriptionPass user={self.user_id} {self.status}>"
