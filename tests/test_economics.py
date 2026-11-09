"""Unit economics: the PRD's tiered model, gateway cap and take-rate band."""

from app.services import economics as economics_service


def test_benchmark_reproduces_prd_target_model(app):
    """PRD section 7: AOV 35,000 @ 5.5% -> 2,225 gross -> 700 overhead -> 1,525 net."""
    result = economics_service.benchmark_assertion()

    assert result["aov"] == 35_000.0
    assert result["take_rate_pct"] == 5.5
    assert result["commission"] == 1_925.0
    assert result["convenience_fee"] == 300.0
    assert result["gross_revenue"] == 2_225.0
    assert result["gateway_and_processing"] == 700.0
    assert result["net_contribution_margin"] == 1_525.0
    assert result["margin_pct_of_gmv"] == 4.36


def test_gateway_fee_is_capped(app):
    # 1.5% of a small basket, then the 2,000 NGN cap on a very large one.
    assert economics_service.gateway_fee_for(10_000) == 150.0
    assert economics_service.gateway_fee_for(1_000_000) == 2_000.0


def test_take_rate_is_clamped_to_contract_band(app):
    assert economics_service.clamp_take_rate(0.01) == 0.03
    assert economics_service.clamp_take_rate(0.99) == 0.08
    assert economics_service.clamp_take_rate(0.055) == 0.055


def test_net_margin_is_gross_revenue_minus_variable_costs(app):
    econ = economics_service.compute_order_economics(35_000, 0.055, logistics_cost=0.0)

    assert econ.gross_revenue == (
        econ.commission_amount + econ.convenience_fee + econ.subscription_allocation
    )
    assert econ.net_contribution_margin == round(
        econ.gross_revenue - econ.direct_variable_costs, 2
    )


def test_subscriber_allocation_lifts_gross_revenue(app):
    econ = economics_service.compute_order_economics(
        35_000, 0.055, is_subscriber=True, logistics_cost=0.0
    )
    # 5,000 NGN pass recognised across 10 orders.
    assert econ.subscription_allocation == 500.0
    assert econ.gross_revenue == 1_925.0 + 300.0 + 500.0
