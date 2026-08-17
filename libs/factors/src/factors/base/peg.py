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

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from re import compile as re_compile

from factors.registry import factor
from factors.types import Fact, FactorResult, GrowthConvention, UnitFamily

_EPS_DILUTED = "eps_diluted"
_NET_INCOME = "net_income"
_PRICE = "price"
_SHARES = "shares_outstanding"

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


@factor("peg", kind="base", module=1)
def peg(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    growth_convention: GrowthConvention,
    as_of: datetime,
    cagr_years: int,
) -> FactorResult:
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
        raise ValueError("cagr_years must be at least 1 — a CAGR needs two dated observations")
    if growth_convention is not GrowthConvention.HISTORICAL_CAGR:
        return unavailable([f"growth_convention_unsourced:{growth_convention.value}"])

    price = _sole(facts, entity_id, _PRICE)
    earnings = _annual_series(facts, entity_id, _EPS_DILUTED)
    series = _annual_series(facts, entity_id, _NET_INCOME)

    flags: list[str] = []
    if price is None or price.value is None:
        flags.append("missing_price")
    elif price.value <= 0:
        flags.append("non_positive_price")
    if not earnings:
        flags.append("missing_eps_diluted")
    if len(series) < 2:
        flags.append("insufficient_earnings_history")
    if flags:
        return unavailable(flags)

    assert price is not None  # the flag pass above returns when it is missing
    eps_year, eps = earnings[-1]
    latest_year, latest = series[-1]
    base_year = latest_year - cagr_years
    base = next((fact for year, fact in series if year == base_year), None)
    if base is None:
        # Refuse to stretch the window to whatever history happens to exist: a CAGR
        # over a different number of years is a different number, and silently
        # substituting one makes issuers incomparable inside the same ranking.
        return unavailable([f"missing_base_year:{base_year}", f"cagr_years:{cagr_years}"])

    assert latest.value is not None and base.value is not None and price.value is not None
    assert eps.value is not None
    if eps.value <= 0 or latest.value <= 0:
        return unavailable(["non_positive_earnings", f"cagr_years:{cagr_years}"])
    if eps_year != latest_year:
        # The multiple and the growth must end on the same year or the ratio
        # compares a stale price-to-earnings against a fresher growth rate.
        return unavailable([f"earnings_period_mismatch:{eps_year}!={latest_year}"])
    if base.value <= 0:
        # A CAGR across a sign change is not a growth rate: the root of a negative
        # ratio is undefined, and a company that swung from loss to profit has no
        # meaningful compound rate to divide a multiple by.
        return unavailable(["non_positive_base_earnings", f"cagr_years:{cagr_years}"])

    with localcontext() as context:
        context.prec = 28
        try:
            growth = (latest.value / base.value) ** (Decimal(1) / Decimal(cagr_years)) - Decimal(1)
        except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - guarded above
            return unavailable(["growth_rate_undefined", f"cagr_years:{cagr_years}"])
        if growth <= 0:
            # PEG is only interpretable for positive growth; a negative denominator
            # would rank a shrinking company as "cheap".
            return unavailable(["non_positive_growth", f"cagr_years:{cagr_years}"])
        price_to_earnings = price.value / eps.value
        # Growth enters as percentage points, the convention PEG is quoted in: a 20%
        # grower on a 20x multiple is 1.0, not 100.
        value = price_to_earnings / (growth * Decimal(100))

    return FactorResult(
        factor="peg",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.RATIO,
        confidence=min(price.confidence, eps.confidence, latest.confidence, base.confidence),
        as_of=as_of,
        data_availability="unverified",
        flags=[f"cagr_years:{cagr_years}", f"base_fiscal_year:{base_year}"],
    )


def peg_from_rate(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
    growth_rate: Decimal | None,
) -> FactorResult:
    """PEG when the growth rate was already derived upstream.

    `peg()` above owns the whole computation, including reducing an annual series to a
    compound rate, and is the form to use where the series is available. The deployed
    capture path derives that rate at the source instead — company-facts already carries
    every annual period, so the adapter reduces it there and lands one scalar rather than
    a series, which is what lets module 1 run without the multi-period read path that does
    not exist in Production (#530).

    The rate arrives as a parameter rather than a `Fact` for the same reason
    `risk_free_rate` does in module 2: it is a declared policy input, not an observation
    of the issuer, and the metric registry is the source of truth for things that ARE
    observations. Both forms share the degenerate-case rules below so the two entry points
    cannot disagree about when PEG is undefined.

    The multiple here is `market cap / net income` rather than `price / eps_diluted`,
    which is the reverse of the choice `peg()` makes, and the reason is measured:
    `NetIncomeLoss` is present for 20 of 20 TOPT issuers while `EarningsPerShareDiluted`
    is present for 18. It also puts BOTH halves of the ratio on one earnings basis, so a
    restatement moves the multiple and the growth together. The share count re-enters, but
    it brings no new exposure — `current_price_to_sales` in the same decision already
    depends on it, and #529's staleness guard turns a stale count into an absent one, so
    the failure mode is an unavailable PEG rather than a wrong one.
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

    price = _sole(facts, entity_id, _PRICE)
    shares = _sole(facts, entity_id, _SHARES)
    earnings = _sole(facts, entity_id, _NET_INCOME)

    flags: list[str] = []
    if price is None or price.value is None:
        flags.append("missing_price")
    elif price.value <= 0:
        flags.append("non_positive_price")
    if shares is None or shares.value is None:
        flags.append("missing_shares_outstanding")
    elif shares.value <= 0:
        flags.append("non_positive_shares_outstanding")
    if earnings is None or earnings.value is None:
        flags.append("missing_net_income")
    if growth_rate is None:
        flags.append("missing_growth_rate")
    if flags:
        return unavailable(flags)

    assert price is not None and price.value is not None
    assert shares is not None and shares.value is not None
    assert earnings is not None and earnings.value is not None and growth_rate is not None
    if earnings.value <= 0:
        return unavailable(["non_positive_earnings"])
    if growth_rate <= 0:
        # A shrinking issuer must not read as cheap through a negative denominator.
        return unavailable(["non_positive_growth"])

    with localcontext() as context:
        context.prec = 28
        price_to_earnings = (price.value * shares.value) / earnings.value
        value = price_to_earnings / (growth_rate * Decimal(100))

    return FactorResult(
        factor="peg",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.RATIO,
        confidence=min(price.confidence, shares.confidence, earnings.confidence),
        as_of=as_of,
        data_availability="unverified",
        flags=[],
    )
