"""Module 7: three-tier valuation composite (#21, #1235)."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.strategy import (
    LargeModelValueV0Definition,
    PriceToGrossProfitTierBand,
    ProvisionalTierBand,
)

from factors.registry import factor
from factors.types import DataAvailability, FactorResult, UnitFamily, refused_factor_result

_LABOR_EFFICIENCY_FACTORS = ("gross_profit_per_employee", "gppe_uniform_charge_v0", "gppe_banking_tce_v1")


def _select_result(names: Sequence[str], inputs: Sequence[FactorResult], entity_id: str) -> FactorResult | None:
    matches = [item for item in inputs if item.factor in names and item.entity_id == entity_id]
    if len(matches) > 1:
        label = names[0] if len(names) == 1 else ", ".join(sorted({item.factor for item in matches}))
        raise ValueError(f"{entity_id}: multiple factor results for factor {label!r}")
    return matches[0] if matches else None


def tier_band(
    definition: LargeModelValueV0Definition,
    labor_efficiency: Decimal,
) -> ProvisionalTierBand | PriceToGrossProfitTierBand:
    return definition.tier_valuation.band_for(labor_efficiency)


@factor("three_tier_valuation", kind="composite", module=7)
def three_tier_valuation(
    inputs: Sequence[FactorResult],
    *,
    entity_id: str,
    as_of: datetime,
    definition: LargeModelValueV0Definition,
) -> FactorResult:
    gppe = _select_result(_LABOR_EFFICIENCY_FACTORS, inputs, entity_id)
    basis = definition.valuation_basis.value
    multiple = _select_result((basis,), inputs, entity_id)

    if gppe is None or gppe.value is None:
        return refused_factor_result("three_tier_valuation", entity_id, as_of, ["missing_gross_profit_per_employee"])
    if multiple is None or multiple.value is None:
        return refused_factor_result("three_tier_valuation", entity_id, as_of, [f"missing_{basis}"])
    if multiple.value <= 0:
        return refused_factor_result("three_tier_valuation", entity_id, as_of, [f"nonpositive_{basis}"])

    band = tier_band(definition, gppe.value)
    midpoint = (band.target_lower_bound + band.target_upper_bound) / Decimal(2)
    data_availability: DataAvailability = (
        "verified"
        if gppe.data_availability == "verified" and multiple.data_availability == "verified"
        else "unverified"
    )
    return FactorResult(
        factor="three_tier_valuation",
        entity_id=entity_id,
        value=midpoint / multiple.value - Decimal(1),
        unit_family=UnitFamily.RATIO,
        confidence=min(gppe.confidence, multiple.confidence),
        as_of=as_of,
        data_availability=data_availability,
    )
