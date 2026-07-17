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
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import Fact, FactorResult

_REQUIRED_METRICS = ("price", "shares_outstanding", "revenue")

_MISSING_REASON = {
    "price": "missing_market_value_input",
    "shares_outstanding": "missing_market_value_input",
    "revenue": "missing_revenue_fact",
}


@factor("price_to_sales", kind="base", module=6)
def price_to_sales(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    by_metric = {fact.metric: fact for fact in facts if fact.entity_id == entity_id}
    missing = [
        metric for metric in _REQUIRED_METRICS if by_metric.get(metric) is None or by_metric[metric].value is None
    ]
    if missing:
        flags = sorted({_MISSING_REASON[metric] for metric in missing})
        return FactorResult(
            factor="price_to_sales",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=flags,
        )

    revenue = by_metric["revenue"].value
    assert revenue is not None
    if revenue <= 0:
        return FactorResult(
            factor="price_to_sales",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["nonpositive_revenue"],
        )

    price = by_metric["price"].value
    shares_outstanding = by_metric["shares_outstanding"].value
    assert price is not None
    assert shares_outstanding is not None

    market_cap = price * shares_outstanding
    value = market_cap / revenue
    confidence = min(by_metric[metric].confidence for metric in _REQUIRED_METRICS)
    return FactorResult(
        factor="price_to_sales",
        entity_id=entity_id,
        value=value,
        confidence=confidence,
        as_of=as_of,
        data_availability="verified",
    )
