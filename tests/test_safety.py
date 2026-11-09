"""Food-safety segregation (FR-05) and NAFDAC validation."""

from app.constants import (
    CAT_CARE,
    CAT_CLEAN,
    CAT_DRY,
    CAT_FRESH,
    SAFETY_CHEMICAL,
    SAFETY_CARE,
    SAFETY_DRY_FOOD,
    SAFETY_PERISHABLE,
)
from app.services import safety as safety_service


def test_safety_class_mapping():
    assert safety_service.safety_class_for(CAT_FRESH) == SAFETY_PERISHABLE
    assert safety_service.safety_class_for(CAT_DRY) == SAFETY_DRY_FOOD
    assert safety_service.safety_class_for(CAT_CARE) == SAFETY_CARE
    assert safety_service.safety_class_for(CAT_CLEAN) == SAFETY_CHEMICAL
    # Unknown categories fail safe into the sealed lane.
    assert safety_service.safety_class_for("mystery") == SAFETY_CHEMICAL


def test_nafdac_is_required_for_food_only():
    ok, err = safety_service.validate_nafdac(CAT_FRESH, None)
    assert ok is False and "NAFDAC" in err

    ok, err = safety_service.validate_nafdac(CAT_DRY, "A1-2210")
    assert ok is True and err is None

    ok, err = safety_service.validate_nafdac(CAT_DRY, "not-a-number")
    assert ok is False

    # Household cleaners are not food -> no NAFDAC registration needed.
    ok, err = safety_service.validate_nafdac(CAT_CLEAN, None)
    assert ok is True and err is None


def test_pick_list_loads_chemicals_last(app, sample, make_order):
    order = make_order([("tomatoes", 1), ("rice", 1), ("soap", 1), ("bleach", 1)])
    pick = safety_service.build_pick_list(order.items)

    lanes = [lane["safety_category"] for lane in pick["lanes"]]
    assert lanes[-1] == SAFETY_CHEMICAL
    assert pick["cross_contamination_risk"] is True
    assert any("sealed liner" in rule for rule in pick["segregation_rules"])


def test_no_cross_risk_when_no_chemicals(app, sample, make_order):
    order = make_order([("tomatoes", 1), ("rice", 1)])
    pick = safety_service.build_pick_list(order.items)

    assert pick["cross_contamination_risk"] is False


def test_order_safety_lanes_keep_chemicals_apart(app, sample, make_order):
    order = make_order([("tomatoes", 2), ("bleach", 1)])

    assert order.cross_contamination_risk is True
    assert order.safety_lanes[-1]["safety_category"] == SAFETY_CHEMICAL
