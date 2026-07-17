"""Module: issuer price-to-sales (P/S), simplified v0 preview.

price_to_sales = (price * shares_outstanding) / revenue

This is a **simplified preview** of the fuller S5 kernel proven in
`factors.batches.issuer_price_to_sales_tiny.kernel` (issue #161, closed):
dual-class share-class aggregation, per-listing multi-currency FX conversion,
and quarterly/fiscal-year revenue-window completeness checks are proven there
but deliberately not ported here yet — the flat `Fact` interface (one scalar
value per metric) has no place to carry multiple listings/securities per
issuer. Single-listing, reporting-currency-only issuers get a correct number
from this module; dual-class issuers need the richer S5-style aggregation,
which is real future work once factor inputs carry per-component structure
(tracked alongside the Gate-1 execution-spine migration, not attempted here).

`data_availability` is "unverified" even on success, matching
`gross_profit_per_employee`'s deliberate choice: neither factor is fed by an
accepted capture/snapshot pipeline yet in this preview round.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import Fact, FactorResult, UnitFamily

_REQUIRED_METRICS = ("price", "shares_outstanding", "revenue")

_MISSING_REASON = {
    "price": "missing_market_value_input",
    "shares_outstanding": "missing_market_value_input",
    "revenue": "missing_revenue_fact",
}


def _find(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    # Facts already reflect one PIT-resolved vintage per metric; a factor never
    # re-selects among candidates (init.md Section 6) — take the sole match.
    # Mirrors gross_profit_per_employee._find: source fusion never silently
    # picks the most recently supplied row (CLAUDE.md architecture red line).
    matches = [f for f in facts if f.entity_id == entity_id and f.metric == metric]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple PIT-resolved facts for metric {metric!r}")
    return matches[0] if matches else None


@factor("price_to_sales", kind="base", module=6)
def price_to_sales(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    price_fact = _find(facts, entity_id, "price")
    shares_fact = _find(facts, entity_id, "shares_outstanding")
    revenue_fact = _find(facts, entity_id, "revenue")

    flags: list[str] = []
    if price_fact is None or price_fact.value is None:
        flags.append(_MISSING_REASON["price"])
    if shares_fact is None or shares_fact.value is None:
        flags.append(_MISSING_REASON["shares_outstanding"])
    if revenue_fact is None or revenue_fact.value is None:
        flags.append(_MISSING_REASON["revenue"])
    if flags:
        return FactorResult(
            factor="price_to_sales",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=sorted(set(flags)),
        )

    assert price_fact is not None and shares_fact is not None and revenue_fact is not None
    assert price_fact.value is not None and shares_fact.value is not None and revenue_fact.value is not None

    if revenue_fact.value <= 0:
        return FactorResult(
            factor="price_to_sales",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["nonpositive_revenue"],
        )

    market_cap = price_fact.value * shares_fact.value
    value = market_cap / revenue_fact.value
    confidence = min(price_fact.confidence, shares_fact.confidence, revenue_fact.confidence)
    return FactorResult(
        factor="price_to_sales",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.RATIO,
        confidence=confidence,
        as_of=as_of,
        data_availability="unverified",
    )
