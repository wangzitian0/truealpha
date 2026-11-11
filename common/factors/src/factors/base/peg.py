"""Module 1: PEG with switchable growth-rate conventions (#284)."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from truealpha_contracts.fiscal_period import parse_annual

from factors.measure import UnitMismatchError, times
from factors.registry import factor
from factors.types import Fact, FactorResult, GrowthConvention, UnitFamily, refused_factor_result, sole_fact

_NET_INCOME = "net_income"
_PRICE = "price"
_SHARES = "shares_outstanding"
_CONSENSUS_GROWTH = "consensus_profit_growth"
_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_WINDOW_TOLERANCE_DAYS = 45


def _annual_by_period_end(facts: Sequence[Fact], entity_id: str, metric: str) -> dict[date, Decimal]:
    by_end: dict[date, Decimal] = {}
    for fact in facts:
        if (
            fact.entity_id != entity_id
            or fact.metric != metric
            or fact.value is None
            or fact.fiscal_period is None
            or (period := parse_annual(fact.fiscal_period)) is None
        ):
            continue
        if period.end in by_end:
            raise ValueError(f"{entity_id}: multiple PIT-resolved {metric!r} facts for the period ending {period.end}")
        by_end[period.end] = fact.value
    return by_end


def _recency_weighted_growth(
    values: Mapping[date, Decimal], years: int
) -> tuple[Decimal | None, date | None, date | None]:
    if not values or years < 1:
        return None, None, None
    latest_end = max(values)

    def nearest(target_days: int) -> date | None:
        best = min(
            (end for end in values if end <= latest_end),
            key=lambda end: abs((latest_end - end).days - target_days),
            default=None,
        )
        return (
            best if best is not None and abs((latest_end - best).days - target_days) <= _WINDOW_TOLERANCE_DAYS else None
        )

    steps = [nearest(round(offset * 365.25)) for offset in range(years + 1)]
    if any(end is None for end in steps) or len(set(steps)) != years + 1:
        return None, None, None
    ordered = [values[end] for end in reversed(steps)]  # type: ignore[index]
    base_end, latest_end_used = steps[-1], steps[0]
    if any(value <= 0 for value in ordered):
        return None, base_end, latest_end_used

    with localcontext(_DECIMAL_CONTEXT):
        weighted = sum(Decimal(i) * (ordered[i] / ordered[i - 1] - Decimal(1)) for i in range(1, len(ordered)))
        total_weight = sum((Decimal(i) for i in range(1, len(ordered))), Decimal(0))
        return weighted / total_weight, base_end, latest_end_used


def _compute_peg_ratio(
    *,
    entity_id: str,
    as_of: datetime,
    price: Fact,
    shares: Fact,
    latest_earnings: Decimal,
    growth_pct: Decimal,
    consumed_confidences: Sequence[Decimal],
    flags: list[str],
) -> FactorResult:
    try:
        market_cap = times(price.to_measure(), shares.to_measure()).value
    except UnitMismatchError as exc:
        return refused_factor_result("peg", entity_id, as_of, [exc.reason, *flags])
    with localcontext(_DECIMAL_CONTEXT):
        value = (market_cap / latest_earnings) / growth_pct
    return FactorResult(
        factor="peg",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.RATIO,
        confidence=min(consumed_confidences),
        as_of=as_of,
        data_availability="unverified",
        flags=flags,
    )


def _validate_base_peg_inputs(price: Fact | None, shares: Fact | None, series: Mapping[date, Decimal]) -> list[str]:
    flags: list[str] = []
    if price is None or price.value is None:
        flags.append("missing_price")
    elif price.value <= 0:
        flags.append("non_positive_price")
    if shares is None or shares.value is None:
        flags.append("missing_shares_outstanding")
    elif shares.value <= 0:
        flags.append("non_positive_shares_outstanding")
    if not series:
        flags.append("missing_net_income")
    return flags


@factor("peg", kind="base", module=1, inputs=("price", "shares_outstanding", "net_income"))
def peg(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    growth_convention: GrowthConvention,
    as_of: datetime,
    cagr_years: int,
) -> FactorResult:
    if cagr_years < 1:
        raise ValueError("cagr_years must be at least 1 — a growth rate needs two dated observations")
    if growth_convention is GrowthConvention.COMPANY_GUIDANCE:
        return refused_factor_result(
            "peg", entity_id, as_of, [f"growth_convention_unsourced:{growth_convention.value}"]
        )

    consensus_facts = [
        f for f in facts if f.entity_id == entity_id and f.metric == _CONSENSUS_GROWTH and f.value is not None
    ]
    if growth_convention is GrowthConvention.ANALYST_CONSENSUS and not consensus_facts:
        return refused_factor_result(
            "peg", entity_id, as_of, [f"growth_convention_unsourced:{growth_convention.value}"]
        )

    price = sole_fact(facts, entity_id, _PRICE)
    shares = sole_fact(facts, entity_id, _SHARES)
    series = _annual_by_period_end(facts, entity_id, _NET_INCOME)
    flags = _validate_base_peg_inputs(price, shares, series)
    if flags:
        return refused_factor_result("peg", entity_id, as_of, flags)
    assert price is not None and price.value is not None and shares is not None and shares.value is not None

    latest_earnings = series[max(series)]
    ni_confs = [
        f.confidence for f in facts if f.entity_id == entity_id and f.metric == _NET_INCOME and f.value is not None
    ]

    if growth_convention is GrowthConvention.ANALYST_CONSENSUS:
        conv_flags = [f"growth_convention:{GrowthConvention.ANALYST_CONSENSUS.value}"]
        if latest_earnings <= 0:
            return refused_factor_result("peg", entity_id, as_of, ["non_positive_earnings", *conv_flags])
        consensus_series = _annual_by_period_end(facts, entity_id, _CONSENSUS_GROWTH)
        if consensus_series:
            latest_cg_end = max(consensus_series)
            rate_pct = consensus_series[latest_cg_end]
            conv_flags.append(f"period_end:{latest_cg_end.isoformat()}")
        else:
            scalar_cg = sole_fact(facts, entity_id, _CONSENSUS_GROWTH)
            assert scalar_cg is not None and scalar_cg.value is not None
            rate_pct = scalar_cg.value
        if rate_pct <= 0:
            return refused_factor_result("peg", entity_id, as_of, ["non_positive_growth", *conv_flags])
        return _compute_peg_ratio(
            entity_id=entity_id,
            as_of=as_of,
            price=price,
            shares=shares,
            latest_earnings=latest_earnings,
            growth_pct=rate_pct,
            consumed_confidences=[
                price.confidence,
                shares.confidence,
                *(f.confidence for f in consensus_facts),
                *ni_confs,
            ],
            flags=conv_flags,
        )

    window = [f"cagr_years:{cagr_years}"]
    growth, base_end, latest_end = _recency_weighted_growth(series, cagr_years)
    if base_end is not None and latest_end is not None:
        window.append(f"window:{base_end.isoformat()}..{latest_end.isoformat()}")
    if latest_earnings <= 0:
        return refused_factor_result("peg", entity_id, as_of, ["non_positive_earnings", *window])
    if growth is None:
        return refused_factor_result("peg", entity_id, as_of, ["insufficient_earnings_history", *window])
    if growth <= 0:
        return refused_factor_result("peg", entity_id, as_of, ["non_positive_growth", *window])
    with localcontext(_DECIMAL_CONTEXT):
        growth_pct = growth * Decimal(100)
    return _compute_peg_ratio(
        entity_id=entity_id,
        as_of=as_of,
        price=price,
        shares=shares,
        latest_earnings=latest_earnings,
        growth_pct=growth_pct,
        consumed_confidences=[price.confidence, shares.confidence, *ni_confs],
        flags=window,
    )
