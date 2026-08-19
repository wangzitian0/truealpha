"""Module 1: PEG with switchable growth-rate conventions.

PEG divides the price/earnings multiple by an earnings growth rate, so a company
growing fast enough to justify its multiple lands near 1. The growth rate is the
contested half — analyst consensus, historical CAGR and company guidance can point
at different conclusions for the same issuer, which is why the convention is an
explicit argument rather than a constant (init.md Section 0, Section 7 module 1).

Only `HISTORICAL_CAGR` is computable today. The other two conventions are declared
in `GrowthConvention` but have no source behind them: analyst consensus needs the
moomoo rating feed (`staging.api_call_ledger` has never held a row) and company
guidance needs `staging.company_guidance` (empty in both environments). Asking for
either returns an explicit `growth_convention_unsourced` flag rather than a
silently substituted number.

The two halves take their earnings from different metrics, and the reason is
measured rather than stylistic:

- the MULTIPLE is `price / eps_diluted` of the same period. Both are per-share and
  contemporaneous, so no share count is compared across time and the input this
  repo has most often had wrong (a 2010 cover-page figure was driving 50% of the
  live portfolio as recently as this month) cannot distort it.
- the GROWTH is the CAGR of `net_income`, because a per-share series is not
  comparable across a stock split and nothing in this warehouse adjusts for one:
  `staging.mvp_corporate_actions` is empty. Netflix's stored diluted EPS runs
  11.24, 9.95, 1.20, 1.98, 2.53 for 2021-2025 — the 10-for-1 split restated the
  recent years and left the older filings on the pre-split basis, so a three-year
  EPS CAGR reads as a 37% annual DECLINE for a company whose net income went from
  $4.49B to $10.98B over the same window. Net income is a company-level figure a
  split cannot touch.

Every degenerate case resolves to `value=None` with a named flag instead of a
number. PEG is undefined for non-positive earnings and for non-positive growth,
and both are common enough in a real universe that returning 0, a large sentinel,
or a silently signed value would put a meaningless figure into a ranking. Module 2
sets the precedent: explicit gaps beat silent drops.

Unresolved and deliberately not decided here: the CAGR window length, whether a
sign change mid-window may ever be interpolated, and whether trailing or forward
earnings anchor the multiple. `cagr_years` is a required argument so no default
can quietly become the convention, and the window actually used is reported in the
flags. Freezing these is a versioned owner decision (#284), not a factor-local one.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from re import compile as re_compile

from truealpha_contracts.qlib_expression import (
    QlibCallNode,
    QlibFactorExpressionDefinition,
    QlibFeatureBinding,
    QlibFeatureNode,
    QlibNumericNode,
)

from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY
from factors.registry import factor
from factors.types import Fact, FactorResult, GrowthConvention, UnitFamily

_EPS_DILUTED = "eps_diluted"
_NET_INCOME = "net_income"
_PRICE = "price"
_SHARES = "shares_outstanding"

# The `peg_from_rate` arithmetic as a matrix-compatible Qlib expression, built only from
# the approved Div/Mul operators — the same shape modules 2 and 7 already carry, and the
# one `qlib_engine`'s own docstring says the engine exists to carry ("init.md Section 7
# modules 1-6"; module 1 is this one). init.md rule 25 makes Qlib the factor-expression
# engine; module 1 shipped without its expression, so a pinned-Qlib run could reproduce
# GPPE and P/S but had nothing to reproduce here.
#
# As in module 2, the Decimal path below stays the source of truth — fast and
# dependency-light — and this definition is the reproducibility proof, cross-checked by
# test rather than invoked per call. `growth_rate` binds as a feature exactly as
# `risk_free_rate` does for GPPE: a declared input the caller supplies, not an
# observation the factor looks up.
#
# The literal 100 is the percentage-point convention, not a tuning constant: PEG divides
# the multiple by the growth rate expressed in points (0.0821 -> 8.21), so it belongs in
# the expression rather than in a caller that could pick a different scale.
PEG_EXPRESSION_DEFINITION = QlibFactorExpressionDefinition(
    factor_id="factor.peg.from_rate",
    factor_version="0.1.0",
    operator_registry_id=BUILTIN_OPERATOR_REGISTRY.operator_registry_id,
    feature_bindings=(
        QlibFeatureBinding(feature_binding_id="feature.price", qlib_field_name="price"),
        QlibFeatureBinding(feature_binding_id="feature.shares_outstanding", qlib_field_name="shares_outstanding"),
        QlibFeatureBinding(feature_binding_id="feature.net_income", qlib_field_name="net_income"),
        QlibFeatureBinding(feature_binding_id="feature.growth_rate", qlib_field_name="growth_rate"),
    ),
    root=QlibCallNode(
        operator_id="truealpha.qlib.div.v1",
        arguments=(
            # market capitalisation / net income — the multiple
            QlibCallNode(
                operator_id="truealpha.qlib.div.v1",
                arguments=(
                    QlibCallNode(
                        operator_id="truealpha.qlib.mul.v1",
                        arguments=(
                            QlibFeatureNode(feature_binding_id="feature.price"),
                            QlibFeatureNode(feature_binding_id="feature.shares_outstanding"),
                        ),
                    ),
                    QlibFeatureNode(feature_binding_id="feature.net_income"),
                ),
            ),
            # the growth rate in percentage points
            QlibCallNode(
                operator_id="truealpha.qlib.mul.v1",
                arguments=(
                    QlibFeatureNode(feature_binding_id="feature.growth_rate"),
                    QlibNumericNode(value=Decimal(100)),
                ),
            ),
        ),
    ),
    # Zero, like module 2: every input is one PIT-resolved value at the cutoff, and the
    # growth rate arrives already reduced. The window this factor divides by spans three
    # years, but that reduction happens upstream — nothing here reads a prior session, and
    # claiming a lookback would misdescribe what a pinned run needs to feed it.
    maximum_lookback_sessions=0,
)

# Staging encodes a fiscal period as "FY2025:FY:2024-01-29:2025-01-26" —
# "<filing fiscal year>:<period kind>:<period start>:<period end>".
#
# The leading FY tag is the FISCAL YEAR OF THE FILING, not of the period the value
# describes. One 10-K carries its comparatives under the same tag, so NVDA's FY2025
# filing yields three annual rows all prefixed FY2025, describing periods ending
# 2025-01-26, 2024-01-28 and 2023-01-29. Keying a series on that prefix would put
# three different years' earnings into one bucket and then compare whichever
# survived against itself. The period END is the only field that says which year a
# number is about, so the series keys on it.
#
# `:FY:` also selects annual rows, but the tag alone is not trusted: JNJ carries a
# `:FY:` row whose period runs to 2022-07-03 — half a year, for an issuer whose
# fiscal year ends in December, with the real FY2022 absent entirely. Taken at face
# value it made a three-year window read 4.81B -> 26.80B, a 77%/yr CAGR, and JNJ
# came out at PEG 0.31 as though it were a hypergrowth name. So the DURATION is
# checked against the same 350-day floor the SEC adapter already uses for exactly
# this reason (`_ANNUAL_MINIMUM_DAYS`), which also absorbs 52/53-week calendars.
_ANNUAL_PERIOD = re_compile(r":FY:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")
_ANNUAL_MINIMUM_DAYS = 350

# The context the growth arithmetic ran under in `sec_financial_adapter`. Kept
# identical on the move so the published values do not shift by a rounding change
# dressed up as a refactor.
_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)

# How far a candidate period may sit from the requested window and still be used.
# 52/53-week calendars drift up to a week a year and a fiscal year end moves within
# a month after a calendar change; 45 days absorbs both while still refusing a
# 4-year gap standing in for a 3-year window (365 days out, not 45).
_WINDOW_TOLERANCE_DAYS = 45


def _sole(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    """The one PIT-resolved fact for a single-period metric (module 2's convention)."""
    matches = [f for f in facts if f.entity_id == entity_id and f.metric == metric]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple PIT-resolved facts for metric {metric!r}")
    return matches[0] if matches else None


def _annual_series(facts: Sequence[Fact], entity_id: str, metric: str) -> list[tuple[int, Fact]]:
    """Every ANNUAL period this metric was PIT-resolved for, keyed by the year the
    period ENDED in, oldest first.

    Unlike module 2, PEG consumes a SERIES of one metric, so multiple facts for the
    same metric are expected rather than an error. One fact per period is still
    required: two observations of the same period end would mean the caller passed
    more than one vintage, and choosing between vintages is the staging layer's job,
    not a factor's (init.md Section 6). Quarterly rows are skipped rather than
    rejected — a filing legitimately carries both, and only the annual ones are
    comparable across a multi-year window.
    """
    by_year: dict[int, Fact] = {}
    for fact in facts:
        if fact.entity_id != entity_id or fact.metric != metric or fact.value is None:
            continue
        if fact.fiscal_period is None:
            continue
        matched = _ANNUAL_PERIOD.search(fact.fiscal_period)
        if matched is None:
            continue
        start, end = date.fromisoformat(matched.group(1)), date.fromisoformat(matched.group(2))
        if (end - start).days < _ANNUAL_MINIMUM_DAYS:
            # Tagged annual, but shorter than a year. Silently averaging it into a
            # CAGR is how a half-year became a growth rate.
            continue
        year = end.year
        if year in by_year:
            raise ValueError(f"{entity_id}: multiple PIT-resolved {metric!r} facts for a period ending in {year}")
        by_year[year] = fact
    return sorted(by_year.items())


def _annual_by_period_end(facts: Sequence[Fact], entity_id: str, metric: str) -> dict[date, Decimal]:
    """Every ANNUAL period this metric was PIT-resolved for, keyed by the period's END DATE.

    Keyed by the end DATE, never by `end.year`. The calendar year of a period end is not
    the fiscal year: JNJ's FY2021 ends 2022-01-02, so a year key selected a period 1456
    days back and reported it as a three-year window. Amazon fails the other way — its
    10-Qs publish trailing-twelve-month spans, so several "annual" periods share one
    calendar year and a year key picked among them arbitrarily.

    One fact per period end is still required: two observations of the same period would
    mean the caller passed more than one vintage, and choosing between vintages is the
    staging layer's job, not a factor's (init.md rule 3). Quarterly rows are skipped rather
    than rejected — a filing legitimately carries both, and only annual periods are
    comparable across a multi-year window.
    """
    by_end: dict[date, Decimal] = {}
    for fact in facts:
        if fact.entity_id != entity_id or fact.metric != metric or fact.value is None:
            continue
        if fact.fiscal_period is None:
            continue
        matched = _ANNUAL_PERIOD.search(fact.fiscal_period)
        if matched is None:
            continue
        start, end = date.fromisoformat(matched.group(1)), date.fromisoformat(matched.group(2))
        if (end - start).days < _ANNUAL_MINIMUM_DAYS:
            # Tagged annual, shorter than a year. Averaging it into a growth rate is how a
            # six-month period became one (#572).
            continue
        if end in by_end:
            raise ValueError(f"{entity_id}: multiple PIT-resolved {metric!r} facts for the period ending {end}")
        by_end[end] = fact.value
    return by_end


def _recency_weighted_growth(
    values: Mapping[date, Decimal], years: int
) -> tuple[Decimal | None, date | None, date | None]:
    """The recency-weighted annual growth rate over `years`, plus the window's endpoints.

    Owner decision (2026-08-17): three years, weights rising toward the present. So this is
    NOT an endpoint CAGR. An endpoint rate is not equal-weighted, it is zero-weighted on
    everything between the two ends — a collapse and recovery inside the window leaves it
    unchanged, which is exactly the shape a recency preference is meant to distinguish. The
    rate is the weighted mean of the year-over-year rates, weighted 1..n oldest to newest.

    Every year boundary must be present. Dropping a missing one would reweight the
    survivors without saying so. A non-positive observation anywhere in the window resolves
    to None: a rate across a sign change is not a growth rate, and here it would
    additionally dominate the mean.

    Windows are selected in ELAPSED DAYS for the reason `_annual_by_period_end` is keyed by
    date. This function ran in `sec_financial_adapter` until the period axis existed
    (migration 0043) — factor arithmetic in the capture layer, which init.md rule 2 forbids
    and which only lived there because the transport could not carry a series.
    """
    if not values or years < 1:
        return None, None, None
    latest_end = max(values)

    def nearest(target_days: int) -> date | None:
        best = min(
            (end for end in values if end <= latest_end),
            key=lambda end: abs((latest_end - end).days - target_days),
            default=None,
        )
        if best is None or abs((latest_end - best).days - target_days) > _WINDOW_TOLERANCE_DAYS:
            return None
        return best

    # One observation per year boundary, newest first: 0, 1 .. years back.
    steps = [nearest(round(offset * 365.25)) for offset in range(years + 1)]
    if any(end is None for end in steps) or len(set(steps)) != years + 1:
        return None, None, None
    ordered = [values[end] for end in reversed(steps)]  # type: ignore[index]
    base_end, latest_end_used = steps[-1], steps[0]
    if any(value <= 0 for value in ordered):
        return None, base_end, latest_end_used

    with localcontext(_DECIMAL_CONTEXT):
        weighted, total_weight = Decimal(0), Decimal(0)
        for index in range(1, len(ordered)):
            weight = Decimal(index)  # 1 for the oldest step, `years` for the newest
            weighted += weight * (ordered[index] / ordered[index - 1] - Decimal(1))
            total_weight += weight
        rate = weighted / total_weight
    return rate, base_end, latest_end_used


@factor("peg", kind="base", module=1)
def peg(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    growth_convention: GrowthConvention,
    as_of: datetime,
    cagr_years: int,
) -> FactorResult:
    """PEG from the annual net-income series, the price, and the share count.

    One entry point. There used to be two — this one, which reduced a series but priced on
    `price / eps_diluted`, and `peg_from_rate`, which took a pre-computed rate and priced on
    `market cap / net income`. The deployed path called the second because the input
    transport carried no fiscal periods, so the rate had to be reduced upstream in
    `sec_financial_adapter`. Two implementations of one factor, only one of them ever run,
    and the arithmetic living where the red line forbids it. Migration 0043 gives the
    transport a period axis, so the series reaches the factor and the split collapses.

    The multiple is `market cap / net income`, the surviving choice, and the reason is
    measured: `NetIncomeLoss` is present for 20 of 20 TOPT issuers where
    `EarningsPerShareDiluted` is present for 18, and it puts BOTH halves of the ratio on
    one earnings basis, so a restatement moves the multiple and the growth together. The
    share count it reintroduces brings no new exposure — `current_price_to_sales` in the
    same decision already depends on it, and #529's staleness guard turns a stale count
    into an absent one, so the failure mode is an unavailable PEG rather than a wrong one.

    Every degenerate case resolves to `value=None` with a named flag. PEG is undefined for
    non-positive earnings and for non-positive growth, and both are common in a real
    universe: returning 0, a sentinel, or a signed value would put a meaningless figure
    into a ranking.
    """

    def unavailable(flags: list[str]) -> FactorResult:
        return FactorResult(
            factor="peg",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal("0"),
            as_of=as_of,
            data_availability="unverified",
            flags=flags,
        )

    if cagr_years < 1:
        raise ValueError("cagr_years must be at least 1 — a growth rate needs two dated observations")
    if growth_convention is not GrowthConvention.HISTORICAL_CAGR:
        # The other two conventions are declared but unsourced: analyst consensus needs the
        # moomoo rating feed (`staging.api_call_ledger` has never held a row) and company
        # guidance needs `staging.company_guidance` (empty in both environments). Saying so
        # beats silently serving the historical number under another name.
        return unavailable([f"growth_convention_unsourced:{growth_convention.value}"])

    price = _sole(facts, entity_id, _PRICE)
    shares = _sole(facts, entity_id, _SHARES)
    series = _annual_by_period_end(facts, entity_id, _NET_INCOME)

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
    if flags:
        return unavailable(flags)

    assert price is not None and price.value is not None
    assert shares is not None and shares.value is not None

    window = [f"cagr_years:{cagr_years}"]
    growth, base_end, latest_end = _recency_weighted_growth(series, cagr_years)
    if base_end is not None and latest_end is not None:
        window += [f"window:{base_end.isoformat()}..{latest_end.isoformat()}"]

    latest_earnings = series[max(series)]
    if latest_earnings <= 0:
        return unavailable(["non_positive_earnings", *window])
    if growth is None:
        # Either the window has no observation at some year boundary, or a year inside it
        # is a loss. Both are refusals, not gaps to interpolate across.
        return unavailable(["insufficient_earnings_history", *window])
    if growth <= 0:
        # PEG is only interpretable for positive growth; a negative denominator would rank
        # a shrinking company as "cheap".
        return unavailable(["non_positive_growth", *window])

    with localcontext(_DECIMAL_CONTEXT):
        multiple = (price.value * shares.value) / latest_earnings
        # Growth enters as percentage points, the convention PEG is quoted in: a 20% grower
        # on a 20x multiple is 1.0, not 100.
        value = multiple / (growth * Decimal(100))

    consumed = [price.confidence, shares.confidence]
    consumed += [
        fact.confidence
        for fact in facts
        if fact.entity_id == entity_id and fact.metric == _NET_INCOME and fact.value is not None
    ]
    return FactorResult(
        factor="peg",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.RATIO,
        # init.md L2: a factor's confidence cannot exceed the minimum it consumed. Every
        # period in the window counts — the rate rests on all of them, not just the ends.
        confidence=min(consumed),
        as_of=as_of,
        data_availability="unverified",
        flags=window,
    )
