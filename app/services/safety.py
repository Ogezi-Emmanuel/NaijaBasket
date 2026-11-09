"""Food safety segregation (FR-05) and NAFDAC validation (PRD section 9)."""

import re

from ..constants import (
    CATEGORY_LABELS,
    CATEGORY_SAFETY,
    FOOD_SAFETY_CATEGORIES,
    PICK_LANE_ORDER,
    SAFETY_CHEMICAL,
    SAFETY_LABELS,
)

_LANE_INSTRUCTIONS = {
    "perishable": "Pick last from chilled display. Keep in thermal liner.",
    "dry_food": "Pick from dry aisles. Check seal integrity before bagging.",
    "personal_care": "Pick from beauty aisle. Bag separately from food.",
    "chemical": "Pick from sealed cleaning lane. Double-bag; never beside food.",
}

#: NAFDAC registration numbers look like `A1-1234` / `01-2345` style codes.
NAFDAC_PATTERN = re.compile(r"^[A-Z0-9]{1,4}-\d{3,6}$", re.IGNORECASE)


def safety_class_for(category: str) -> str:
    return CATEGORY_SAFETY.get(category, SAFETY_CHEMICAL)


def validate_nafdac(category: str, reg_no: str | None) -> tuple[bool, str | None]:
    """Food items cannot be listed without a valid NAFDAC registration."""
    safety = safety_class_for(category)
    if safety not in FOOD_SAFETY_CATEGORIES:
        return True, None
    if not reg_no:
        return False, f"{CATEGORY_LABELS.get(category, category)} items require a NAFDAC registration number."
    if not NAFDAC_PATTERN.match(reg_no.strip()):
        return False, "NAFDAC registration number format looks invalid (e.g. A1-1234)."
    return True, None


def build_pick_list(items) -> dict:
    """Group items into segregated pick lanes, chemicals last.

    `items` is any iterable exposing: name, quantity, safety_category and
    either line_total or unit_price.
    """
    lanes: dict[str, list] = {}
    for item in items:
        lanes.setdefault(getattr(item, "safety_category", SAFETY_CHEMICAL), []).append(item)

    ordered = []
    for lane in PICK_LANE_ORDER:
        if lane not in lanes:
            continue
        bucket = lanes[lane]
        subtotal = 0.0
        for entry in bucket:
            subtotal += float(
                getattr(entry, "line_total", 0)
                or (getattr(entry, "unit_price", 0) * getattr(entry, "quantity", 1))
            )
        ordered.append(
            {
                "safety_category": lane,
                "label": SAFETY_LABELS.get(lane, lane),
                "instruction": _LANE_INSTRUCTIONS.get(lane, ""),
                "items": bucket,
                "line_total": round(subtotal, 2),
                "count": sum(int(getattr(e, "quantity", 1) or 1) for e in bucket),
            }
        )

    food_lanes = [lane for lane in ordered if lane["safety_category"] in FOOD_SAFETY_CATEGORIES]
    chemical_lanes = [lane for lane in ordered if lane["safety_category"] == SAFETY_CHEMICAL]
    cross_risk = bool(food_lanes) and bool(chemical_lanes)

    segregation_rules = []
    if cross_risk:
        segregation_rules.append(
            "Separate lanes detected: bag chemicals in a sealed liner and place them "
            "below food items in the crate."
        )
    if any(lane["safety_category"] == "perishable" for lane in ordered):
        segregation_rules.append("Cold chain: use the thermal-insulated transit bag.")
    if any(lane["safety_category"] == "dry_food" for lane in ordered):
        segregation_rules.append("Check every dry-food seal; flag replacements before packing.")

    return {
        "lanes": ordered,
        "cross_contamination_risk": cross_risk,
        "segregation_rules": segregation_rules,
        "total_line_items": sum(lane["count"] for lane in ordered),
    }
