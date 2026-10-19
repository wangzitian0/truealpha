"""Feeder: issuer price-to-gross-profit (P/GP), the valuation basis of strategy definition 1.0.0.

price_to_gross_profit = (price * shares_outstanding) / gross_profit

Owner decision 2026-10-12 (#1235): the large_model_value_v0 strategy values an issuer by
market value over gross profit. Gross profit is the same latest-complete-fiscal-year input
that the labor-efficiency numerator consumes, so the tier and the multiple read one figure.
P/GP removes the gross-margin effect that P/S carries, which is why the former 0.35
gross-margin gate is gone.

This factor follows `price_to_sales` exactly: the same market-value inputs, the same
single-fact selection, the same refusal flags, and the same `module=7` feeder registration
(it is an input that `three_tier_valuation` reduces, not an answer to a question of its
own). It returns the unrounded ratio. The caller quantizes it with the definition's
`price_to_gross_profit.quantization`, so no rounding rule lives here.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.ast import Div, Feature, Mul

from factors.registry import factor
from factors.types import Fact, FactorResult, UnitFamily

#: `(price * shares_outstanding) / gross_profit` as a `truealpha_contracts.ast` expression,
#: the same matrix-compatible shape as `PRICE_TO_SALES_EXPRESSION_DEFINITION`.
PRICE_TO_GROSS_PROFIT_EXPRESSION_DEFINITION = Div(
    left=Mul(left=Feature(name="price"), right=Feature(name="shares_outstanding")),
    right=Feature(name="gross_profit"),
)

_MISSING_REASON = {
    "price": "missing_market_value_input",
    "shares_outstanding": "missing_market_value_input",
    "gross_profit": "missing_gross_profit_fact",
}


def _find(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    # One PIT-resolved fact per metric (init.md Section 6); two candidates are a fusion
    # error upstream, never a choice to make here.
    matches = [f for f in facts if f.entity_id == entity_id and f.metric == metric]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple PIT-resolved facts for metric {metric!r}")
    return matches[0] if matches else None


def _refused(entity_id: str, as_of: datetime, flags: Sequence[str]) -> FactorResult:
    return FactorResult(
        factor="price_to_gross_profit",
        entity_id=entity_id,
        value=None,
        unit_family=UnitFamily.RATIO,
        confidence=Decimal(0),
        as_of=as_of,
        data_availability="unverified",
        flags=sorted(set(flags)),
    )


@factor("price_to_gross_profit", kind="feeder", module=7, inputs=("price", "shares_outstanding", "gross_profit"))
def price_to_gross_profit(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    price_fact = _find(facts, entity_id, "price")
    shares_fact = _find(facts, entity_id, "shares_outstanding")
    gross_profit_fact = _find(facts, entity_id, "gross_profit")

    flags = [
        _MISSING_REASON[metric]
        for metric, fact in (
            ("price", price_fact),
            ("shares_outstanding", shares_fact),
            ("gross_profit", gross_profit_fact),
        )
        if fact is None or fact.value is None
    ]
    if flags:
        return _refused(entity_id, as_of, flags)

    assert price_fact is not None and shares_fact is not None and gross_profit_fact is not None
    assert price_fact.value is not None and shares_fact.value is not None and gross_profit_fact.value is not None

    if price_fact.value <= 0 or shares_fact.value <= 0:
        return _refused(entity_id, as_of, ["nonpositive_market_value"])
    # A zero or negative gross profit gives no multiple: dividing by it would rank a loss
    # as the cheapest issuer in the universe. The strategy excludes it with its own reason.
    if gross_profit_fact.value <= 0:
        return _refused(entity_id, as_of, ["nonpositive_gross_profit"])

    market_value = price_fact.value * shares_fact.value
    return FactorResult(
        factor="price_to_gross_profit",
        entity_id=entity_id,
        value=market_value / gross_profit_fact.value,
        unit_family=UnitFamily.RATIO,
        confidence=min(price_fact.confidence, shares_fact.confidence, gross_profit_fact.confidence),
        as_of=as_of,
        data_availability="unverified",
    )
