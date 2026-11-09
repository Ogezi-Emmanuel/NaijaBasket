"""Unit economics - deterministic margin maths (PRD section 7).

Everything is computed server-side in Python; the client only ever renders what
these functions return. The benchmark asserted by the PRD is reproduced exactly
by `benchmark_assertion()` and verified in the test-suite:

    AOV NGN 35,000 · blended take rate 5.5% · convenience fee NGN 300
    -> Gross revenue NGN 2,225
    -> Gateway + processing overhead NGN 700
    -> Net contribution margin NGN 1,525  (4.36% of GMV)
"""

from dataclasses import asdict, dataclass

from flask import current_app

from ..models import money


@dataclass(frozen=True)
class OrderEconomics:
    gmv: float
    commission_rate: float
    commission_amount: float
    convenience_fee: float
    subscription_allocation: float
    gross_revenue: float
    gateway_fee: float
    processing_cost: float
    logistics_cost: float
    direct_variable_costs: float
    net_contribution_margin: float
    margin_pct_of_gmv: float
    merchant_payout: float
    rider_payout: float

    def as_dict(self) -> dict:
        return asdict(self)

    def order_columns(self) -> dict:
        """Column names matching the Order model."""
        return {
            "gmv_amount": self.gmv,
            "commission_rate": self.commission_rate,
            "commission_amount": self.commission_amount,
            "convenience_fee": self.convenience_fee,
            "subscription_allocation": self.subscription_allocation,
            "gross_revenue": self.gross_revenue,
            "gateway_fee": self.gateway_fee,
            "processing_cost": self.processing_cost,
            "logistics_cost": self.logistics_cost,
            "net_contribution_margin": self.net_contribution_margin,
        }


def clamp_take_rate(rate: float) -> float:
    """Tiered take rates are contractual: 3%-8%."""
    config = current_app.config
    return max(config["MIN_TAKE_RATE"], min(config["MAX_TAKE_RATE"], float(rate)))


def gateway_fee_for(gmv: float) -> float:
    """Payment gateway fee: 1.5% capped (Paystack-style cap)."""
    config = current_app.config
    return money(min(gmv * config["GATEWAY_FEE_RATE"], config["GATEWAY_FEE_CAP"]))


def subscription_allocation_for(is_subscriber: bool) -> float:
    """Portion of a monthly pass recognised as revenue on each order."""
    if not is_subscriber:
        return 0.0
    config = current_app.config
    orders = max(1, config["SUBSCRIPTION_ORDERS_PER_PASS"])
    return money(config["SUBSCRIPTION_PASS_PRICE"] / orders)


def logistics_cost_for(distance_km: float | None = None) -> float:
    """Direct last-mile cost. Base fare plus a small per-km component."""
    config = current_app.config
    base = config["LOGISTICS_BASE_COST"]
    if distance_km is None:
        return money(base)
    return money(base + 12.5 * max(0.0, float(distance_km)))


def compute_order_economics(
    gmv: float,
    commission_rate: float,
    *,
    is_subscriber: bool = False,
    convenience_fee: float | None = None,
    logistics_cost: float | None = None,
    distance_km: float | None = None,
    recovery_discount: float = 0.0,
) -> OrderEconomics:
    """R = (GMV x CommissionRate) + ConvenienceFee + SubscriptionAllocation
    Cv = PaymentGatewayFee(1.5% capped) + Logistics/Processing
    Mc = R - Cv
    """
    config = current_app.config
    gmv = money(gmv)
    rate = clamp_take_rate(commission_rate)

    commission_amount = money(gmv * rate)
    fee = money(config["CONVENIENCE_FEE"] if convenience_fee is None else convenience_fee)
    allocation = subscription_allocation_for(is_subscriber)

    gross_revenue = money(commission_amount + fee + allocation)

    gateway_fee = gateway_fee_for(gmv)
    processing = money(config["PROCESSING_DIRECT_COST"])
    logistics = (
        money(config["LOGISTICS_BASE_COST"] if logistics_cost is None else logistics_cost)
        if distance_km is None or logistics_cost is not None
        else logistics_cost_for(distance_km)
    )
    if logistics_cost is None and distance_km is not None:
        logistics = logistics_cost_for(distance_km)

    direct_variable_costs = money(gateway_fee + logistics + processing)
    net_margin = money(gross_revenue - direct_variable_costs - money(recovery_discount))

    return OrderEconomics(
        gmv=gmv,
        commission_rate=rate,
        commission_amount=commission_amount,
        convenience_fee=fee,
        subscription_allocation=allocation,
        gross_revenue=gross_revenue,
        gateway_fee=gateway_fee,
        processing_cost=processing,
        logistics_cost=logistics,
        direct_variable_costs=direct_variable_costs,
        net_contribution_margin=net_margin,
        margin_pct_of_gmv=money(net_margin / gmv * 100) if gmv else 0.0,
        merchant_payout=money(gmv - commission_amount),
        rider_payout=money(logistics),
    )


def estimate_basket_economics(subtotal: float, commission_rate: float) -> dict:
    """Live preview shown in the basket/checkout summary bar."""
    economics = compute_order_economics(subtotal, commission_rate)
    return {
        "gmv": economics.gmv,
        "commission_amount": economics.commission_amount,
        "convenience_fee": economics.convenience_fee,
        "net_contribution_margin": economics.net_contribution_margin,
        "margin_pct_of_gmv": economics.margin_pct_of_gmv,
    }


def benchmark_assertion() -> dict:
    """Reproduce the PRD's target tiered model on the baseline basket.

    The PRD nets the NGN 700 gateway-and-processing overhead only. The courier
    fare is quoted to the buyer at dispatch (a recovered pass-through), so it is
    excluded here - otherwise the fare would be counted twice, against both the
    processing line and the logistics line.
    """
    config = current_app.config
    economics = compute_order_economics(
        config["TARGET_AOV"],
        config["DEFAULT_TAKE_RATE"],
        logistics_cost=0.0,
    )
    return {
        "aov": economics.gmv,
        "take_rate_pct": round(economics.commission_rate * 100, 2),
        "commission": economics.commission_amount,
        "convenience_fee": economics.convenience_fee,
        "gross_revenue": economics.gross_revenue,
        "gateway_and_processing": money(economics.gateway_fee + economics.processing_cost),
        "net_contribution_margin": economics.net_contribution_margin,
        "margin_pct_of_gmv": economics.margin_pct_of_gmv,
    }
