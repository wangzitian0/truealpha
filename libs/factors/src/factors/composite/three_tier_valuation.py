"""Module 7: three-tier valuation (traditional / tech / large-model-native P/S tier).

Composes one issuer's already-materialized `gross_profit_per_employee` (module
2) and `price_to_sales` (module 6) `FactorResult`s into a valuation gap:
``target_ps_midpoint / current_price_to_sales - 1`` (#21's `ValuationGapRule`
ranks candidates by descending valuation gap). Confidence is the minimum of
the two consumed inputs, per the composite-factor rule (CLAUDE.md: "composite
factors... confidence = min() of inputs").

Tier band thresholds and target P/S ranges are **not** hardcoded here — the
caller supplies an explicit, versioned
`truealpha_contracts.strategy.ThreeTierValuationDefinition` (the locked v0
bands from #21/#335's golden fixture, or any later versioned revision),
matching #21's "no implicit defaults" rule. Band lookup and target-midpoint
arithmetic port the logic proven by the S6 tiny kernel
(`factors.batches.issuer_tier_valuation_tiny.kernel`, #169, closed).

`FactorResult` carries one scalar `value`; it cannot also carry the tier
label, target P/S bounds, or current P/S. Callers that need those for
reporting (e.g. the #26 preview replay) call `definition.band_for(gppe.value)`
directly — the same cheap, deterministic lookup this function already uses —
rather than this module inventing a richer, one-off return shape.

`data_availability` is "verified" only when both consumed inputs are
"verified" -- a composite cannot claim stronger verification than what it was
built from, mirroring how confidence is bounded by min() of inputs.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.strategy import ThreeTierValuationDefinition

from factors.registry import factor
from factors.types import DataAvailability, FactorResult


def _result(name: str, inputs: Sequence[FactorResult]) -> FactorResult | None:
    return next((item for item in inputs if item.factor == name), None)


@factor("three_tier_valuation", kind="composite", module=7)
def three_tier_valuation(
    inputs: Sequence[FactorResult],
    *,
    entity_id: str,
    as_of: datetime,
    definition: ThreeTierValuationDefinition,
) -> FactorResult:
    gppe = _result("gross_profit_per_employee", inputs)
    price_to_sales = _result("price_to_sales", inputs)

    if gppe is None or gppe.value is None:
        return FactorResult(
            factor="three_tier_valuation",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["missing_gross_profit_per_employee"],
        )
    if price_to_sales is None or price_to_sales.value is None:
        return FactorResult(
            factor="three_tier_valuation",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["missing_price_to_sales"],
        )

    current_ps = price_to_sales.value
    if current_ps <= 0:
        return FactorResult(
            factor="three_tier_valuation",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["nonpositive_price_to_sales"],
        )

    band = definition.band_for(gppe.value)
    midpoint = (band.target_ps_lower_bound + band.target_ps_upper_bound) / Decimal(2)
    valuation_gap = midpoint / current_ps - Decimal(1)
    confidence = min(gppe.confidence, price_to_sales.confidence)
    data_availability: DataAvailability = (
        "verified"
        if gppe.data_availability == "verified" and price_to_sales.data_availability == "verified"
        else "unverified"
    )

    return FactorResult(
        factor="three_tier_valuation",
        entity_id=entity_id,
        value=valuation_gap,
        confidence=confidence,
        as_of=as_of,
        data_availability=data_availability,
    )
