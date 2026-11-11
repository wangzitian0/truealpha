"""Feeder: issuer price-to-gross-profit (P/GP), `(price * shares_outstanding) / gross_profit`."""

from collections.abc import Sequence
from datetime import datetime

from factors.registry import factor
from factors.types import Fact, FactorResult, market_multiple_factor


@factor("price_to_gross_profit", kind="feeder", module=7, inputs=("price", "shares_outstanding", "gross_profit"))
def price_to_gross_profit(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    return market_multiple_factor(
        facts,
        factor_name="price_to_gross_profit",
        entity_id=entity_id,
        as_of=as_of,
        denominator_metric="gross_profit",
        missing_denominator_flag="missing_gross_profit_fact",
        nonpositive_denominator_flag="nonpositive_gross_profit",
    )
