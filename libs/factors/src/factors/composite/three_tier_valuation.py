"""Module 7: three-tier valuation (traditional / tech / large-model-native tier).

Composes one issuer's already-materialized `gross_profit_per_employee` (module
2) and one valuation multiple (a feeder that itself registers module 7, since it
exists only to feed this composite) `FactorResult`s into a valuation gap:
``target_multiple_midpoint / current_multiple - 1`` (#21's `ValuationGapRule`
ranks candidates by descending valuation gap). Confidence is the minimum of
the two consumed inputs, per the composite-factor rule (CLAUDE.md: "composite
factors... confidence = min() of inputs").

The multiple is the definition's valuation basis: `price_to_sales` for strategy
definition 0.1.0, `price_to_gross_profit` for definition 1.0.0 (#1235). The basis
names the feeder whose result this function reads, so a P/GP band is never
compared with a P/S value.

Tier band thresholds and target multiple ranges are **not** hardcoded here — the
caller supplies an explicit, versioned tier-valuation definition from
`truealpha_contracts.strategy`, matching #21's "no implicit defaults" rule. Band
lookup and target-midpoint arithmetic port the logic proven by the S6 tiny
kernel of the retired batch machine (#169, closed; the kernel itself was removed
with that machine in #791, and git history holds it).

`FactorResult` carries one scalar `value`; it cannot also carry the tier
label, target bounds, or current multiple. Callers that need those for
reporting call `definition.band_for(gppe.value)` directly — the same cheap,
deterministic lookup this function already uses — rather than this module
inventing a richer, one-off return shape.

`data_availability` is "verified" only when both consumed inputs are
"verified" -- a composite cannot claim stronger verification than what it was
built from, mirroring how confidence is bounded by min() of inputs.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.strategy import ThreeTierPgpValuationDefinition, ThreeTierValuationDefinition

from factors.registry import factor
from factors.types import DataAvailability, FactorResult, UnitFamily


def _result(name: str, inputs: Sequence[FactorResult], entity_id: str) -> FactorResult | None:
    matches = [item for item in inputs if item.factor == name and item.entity_id == entity_id]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple factor results for factor {name!r}")
    return matches[0] if matches else None


#: The labor-efficiency results the composite accepts: one per named GPPE metric (#1176).
_LABOR_EFFICIENCY_FACTORS = ("gross_profit_per_employee", "gppe_uniform_charge_v0", "gppe_banking_tce_v1")


def _labor_efficiency(inputs: Sequence[FactorResult], entity_id: str) -> FactorResult | None:
    matches = [item for item in inputs if item.factor in _LABOR_EFFICIENCY_FACTORS and item.entity_id == entity_id]
    if len(matches) > 1:
        names = ", ".join(sorted({item.factor for item in matches}))
        raise ValueError(f"{entity_id}: multiple factor results for factor {names!r}")
    return matches[0] if matches else None


@factor("three_tier_valuation", kind="composite", module=7)
def three_tier_valuation(
    inputs: Sequence[FactorResult],
    *,
    entity_id: str,
    as_of: datetime,
    definition: ThreeTierValuationDefinition | ThreeTierPgpValuationDefinition,
) -> FactorResult:
    gppe = _labor_efficiency(inputs, entity_id)
    basis = definition.valuation_basis.value
    multiple = _result(basis, inputs, entity_id)

    if gppe is None or gppe.value is None:
        return FactorResult(
            factor="three_tier_valuation",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["missing_gross_profit_per_employee"],
        )
    if multiple is None or multiple.value is None:
        return FactorResult(
            factor="three_tier_valuation",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=[f"missing_{basis}"],
        )

    current_multiple = multiple.value
    if current_multiple <= 0:
        return FactorResult(
            factor="three_tier_valuation",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=[f"nonpositive_{basis}"],
        )

    band = definition.band_for(gppe.value)
    midpoint = (band.target_lower_bound + band.target_upper_bound) / Decimal(2)
    valuation_gap = midpoint / current_multiple - Decimal(1)
    confidence = min(gppe.confidence, multiple.confidence)
    data_availability: DataAvailability = (
        "verified"
        if gppe.data_availability == "verified" and multiple.data_availability == "verified"
        else "unverified"
    )

    return FactorResult(
        factor="three_tier_valuation",
        entity_id=entity_id,
        value=valuation_gap,
        unit_family=UnitFamily.RATIO,
        confidence=confidence,
        as_of=as_of,
        data_availability=data_availability,
    )
