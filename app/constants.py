"""Domain vocabulary: categories, safety classes, order/escrow state machines."""

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
CAT_FRESH = "fresh_produce"
CAT_DRY = "dry_goods"
CAT_CARE = "personal_care"
CAT_CLEAN = "household_cleaners"

CATEGORIES = (CAT_FRESH, CAT_DRY, CAT_CARE, CAT_CLEAN)

CATEGORY_LABELS = {
    CAT_FRESH: "Fresh Produce",
    CAT_DRY: "Dry Goods",
    CAT_CARE: "Personal Care",
    CAT_CLEAN: "Household Cleaners",
}

# Fresh produce is the PRD's 52%-of-revenue highlight category.
CATEGORY_REVENUE_SHARE = {
    CAT_FRESH: 0.52,
    CAT_DRY: 0.21,
    CAT_CARE: 0.14,
    CAT_CLEAN: 0.13,
}

# ---------------------------------------------------------------------------
# Food safety (FR-05). Chemicals must never share a pick lane with food.
# ---------------------------------------------------------------------------
SAFETY_PERISHABLE = "perishable"
SAFETY_DRY_FOOD = "dry_food"
SAFETY_CARE = "personal_care"
SAFETY_CHEMICAL = "chemical"

SAFETY_CATEGORIES = (
    SAFETY_PERISHABLE,
    SAFETY_DRY_FOOD,
    SAFETY_CARE,
    SAFETY_CHEMICAL,
)

SAFETY_LABELS = {
    SAFETY_PERISHABLE: "Perishable · chilled lane",
    SAFETY_DRY_FOOD: "Dry food · food lane",
    SAFETY_CARE: "Personal care · beauty lane",
    SAFETY_CHEMICAL: "Chemical · sealed lane",
}

CATEGORY_SAFETY = {
    CAT_FRESH: SAFETY_PERISHABLE,
    CAT_DRY: SAFETY_DRY_FOOD,
    CAT_CARE: SAFETY_CARE,
    CAT_CLEAN: SAFETY_CHEMICAL,
}

#: Safety classes that count as food and therefore require a NAFDAC number.
FOOD_SAFETY_CATEGORIES = (SAFETY_PERISHABLE, SAFETY_DRY_FOOD)
#: Classes that must be segregated away from food in a pick list.
INCOMPATIBLE_WITH_FOOD = (SAFETY_CHEMICAL,)

PICK_LANE_ORDER = (
    SAFETY_PERISHABLE,
    SAFETY_DRY_FOOD,
    SAFETY_CARE,
    SAFETY_CHEMICAL,
)

# ---------------------------------------------------------------------------
# Order lifecycle
# ---------------------------------------------------------------------------
ST_PENDING_PAYMENT = "pending_payment"
ST_PAID_ESCROW = "paid_escrow"
ST_PACKING = "packing"
ST_PACKED = "packed"
ST_DISPATCHED = "dispatched"
ST_EN_ROUTE = "en_route"
ST_DELIVERED = "delivered"
ST_SLA_BREACH = "sla_breach"
ST_CANCELLED = "cancelled"
ST_REFUNDED = "refunded"

ORDER_STATUSES = (
    ST_PENDING_PAYMENT,
    ST_PAID_ESCROW,
    ST_PACKING,
    ST_PACKED,
    ST_DISPATCHED,
    ST_EN_ROUTE,
    ST_DELIVERED,
    ST_SLA_BREACH,
    ST_CANCELLED,
    ST_REFUNDED,
)

ORDER_STATUS_LABELS = {
    ST_PENDING_PAYMENT: "Awaiting payment",
    ST_PAID_ESCROW: "Paid · funds in escrow",
    ST_PACKING: "Merchant packing",
    ST_PACKED: "Packed · sealing",
    ST_DISPATCHED: "Courier assigned",
    ST_EN_ROUTE: "Courier en route",
    ST_DELIVERED: "Delivered",
    ST_SLA_BREACH: "SLA breached",
    ST_CANCELLED: "Cancelled",
    ST_REFUNDED: "Refunded",
}

#: Statuses that are still "live" for the merchant SLA clock.
SLA_TRACKED_STATUSES = (ST_PAID_ESCROW, ST_PACKING)

#: Terminal statuses.
CLOSED_STATUSES = (ST_DELIVERED, ST_CANCELLED, ST_REFUNDED)

#: The four consumer-facing tracker steps.
TRACKER_STEPS = (
    ("Order Placed", (ST_PAID_ESCROW, ST_PACKING, ST_PACKED, ST_DISPATCHED, ST_EN_ROUTE, ST_DELIVERED)),
    ("Merchant Packing", (ST_PACKED, ST_DISPATCHED, ST_EN_ROUTE, ST_DELIVERED)),
    ("Courier En Route", (ST_DISPATCHED, ST_EN_ROUTE, ST_DELIVERED)),
    ("Delivered", (ST_DELIVERED,)),
)

# ---------------------------------------------------------------------------
# Escrow
# ---------------------------------------------------------------------------
ESCROW_PENDING = "pending"
ESCROW_HELD = "held"
ESCROW_RELEASED = "released"
ESCROW_REFUNDED = "refunded"

ESCROW_STATUS_LABELS = {
    ESCROW_PENDING: "Not funded",
    ESCROW_HELD: "Held in escrow",
    ESCROW_RELEASED: "Released on PIN verification",
    ESCROW_REFUNDED: "Refunded to buyer",
}

LEDGER_HOLD = "hold"
LEDGER_MERCHANT_PAYOUT = "merchant_payout"
LEDGER_RIDER_PAYOUT = "rider_payout"
LEDGER_PLATFORM_REVENUE = "platform_revenue"
LEDGER_REFUND = "refund"

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
DISPATCH_REQUESTED = "requested"
DISPATCH_ASSIGNED = "assigned"
DISPATCH_PICKED_UP = "picked_up"
DISPATCH_DELIVERED = "delivered"
DISPATCH_FAILED = "failed"

PROVIDER_KWIK = "kwik"
PROVIDER_GOKADA = "gokada"
PROVIDER_SANDBOX = "sandbox"

# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
PAY_PAYSTACK = "paystack"
PAY_FLUTTERWAVE = "flutterwave"
PAY_SANDBOX = "sandbox"

#: Channel -> preferred gateway, matching the PRD's Nigerian payment mix.
PAYMENT_CHANNELS = {
    "transfer": {
        "label": "Direct Bank Transfer",
        "hint": "44% of Nigerian online payments · account-to-account",
        "gateway": PAY_PAYSTACK,
    },
    "card": {
        "label": "Debit Card",
        "hint": "Verve · Mastercard · Visa",
        "gateway": PAY_PAYSTACK,
    },
    "ussd": {
        "label": "USSD",
        "hint": "Dial-to-pay for feature phones",
        "gateway": PAY_FLUTTERWAVE,
    },
}

# ---------------------------------------------------------------------------
# Subscription plans
# ---------------------------------------------------------------------------
PASS_MONTHLY = "monthly"
PASS_ANNUAL = "annual"

PASS_PLANS = {
    PASS_MONTHLY: {"label": "Monthly Pass", "months": 1, "multiplier": 1.0},
    PASS_ANNUAL: {"label": "Annual Pass", "months": 12, "multiplier": 10.0},
}

# ---------------------------------------------------------------------------
# WhatsApp Business (FR-06 delivery)
# ---------------------------------------------------------------------------
WA_PROVIDER_CLOUD = "cloud_api"
WA_PROVIDER_SANDBOX = "sandbox"

#: Delivery lifecycle reported by the BSP.
WA_STATUS_QUEUED = "queued"
WA_STATUS_SENT = "sent"
WA_STATUS_DELIVERED = "delivered"
WA_STATUS_READ = "read"
WA_STATUS_FAILED = "failed"

WA_STATUS_LABELS = {
    WA_STATUS_QUEUED: "Queued (sandbox)",
    WA_STATUS_SENT: "Sent",
    WA_STATUS_DELIVERED: "Delivered",
    WA_STATUS_READ: "Read",
    WA_STATUS_FAILED: "Failed",
}

#: Receipts arrive over HTTP and can overtake each other, so the state machine
#: only ever moves forward. `failed` is terminal either way.
WA_STATUS_RANK = {
    WA_STATUS_QUEUED: 0,
    WA_STATUS_SENT: 1,
    WA_STATUS_DELIVERED: 2,
    WA_STATUS_READ: 3,
    WA_STATUS_FAILED: 4,
}
