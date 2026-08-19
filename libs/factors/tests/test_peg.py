"""Module 1 (PEG) — one entry point, over the annual net-income series.

Rewritten when #284's two entry points collapsed into one. The old suite drove
`peg()`'s `price / eps_diluted` multiple and an endpoint CAGR; production ran the other
form (`market cap / net income`, recency-weighted) and these tests never touched it. Now
there is one function and this exercises it.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.peg import peg
from factors.types import Fact, GrowthConvention, UnitFamily

_AS_OF = datetime(2026, 6, 30, tzinfo=UTC)
_ENTITY = "company:cik:1045810"
_UNIT_FAMILY = {
    "net_income": UnitFamily.CURRENCY,
    "price": UnitFamily.PER_SHARE,
    "shares_outstanding": UnitFamily.COUNT,
}


def _fact(metric: str, value, *, period: str | None = None, confidence="0.9") -> Fact:
    return Fact(
        entity_id=_ENTITY,
        metric=metric,
        value=None if value is None else Decimal(str(value)),
        unit_family=_UNIT_FAMILY[metric],
        confidence=Decimal(confidence),
        as_of=_AS_OF,
        fiscal_period=period,
    )


def _annual(end: str, value, *, confidence="0.9") -> Fact:
    """One annual net-income observation, tagged the way staging tags periods.

    The leading FY tag is the FILING's fiscal year and is deliberately wrong here: real
    filings stamp their comparatives with the filing's own tag, so keying on it would
    bucket several years together. The period END identifies the observation.
    """
    year = int(end[:4])
    return _fact("net_income", value, period=f"FY2099:FY:{year - 1}{end[4:]}:{end}", confidence=confidence)


def _inputs(*ends_and_values, price="100", shares="1000"):
    return [
        _fact("price", price),
        _fact("shares_outstanding", shares),
        *[_annual(end, value) for end, value in ends_and_values],
    ]


def _call(facts, *, years=3, convention=GrowthConvention.HISTORICAL_CAGR):
    return peg(facts, entity_id=_ENTITY, growth_convention=convention, as_of=_AS_OF, cagr_years=years)


_STEADY = (("2022-12-31", "100"), ("2023-12-31", "120"), ("2024-12-31", "150"), ("2025-12-31", "200"))


def test_peg_divides_the_market_cap_multiple_by_growth_in_percentage_points() -> None:
    # market cap 100 x 1000 = 100,000; / 200 net income = 500x multiple.
    # yearly rates 20%, 25%, 33.33%; weighted 1:2:3 -> 28.333%. 500 / 28.333 = 17.647.
    result = _call(_inputs(*_STEADY))
    assert result.value is not None
    assert result.value.quantize(Decimal("0.001")) == Decimal("17.647")
    assert result.unit_family is UnitFamily.RATIO
    # The window travels with the number so two results are never silently incomparable.
    assert "cagr_years:3" in result.flags
    assert "window:2022-12-31..2025-12-31" in result.flags


def test_recent_years_weigh_more_than_old_ones() -> None:
    """The owner decision that makes this not an endpoint CAGR (#284).

    Same endpoints, different middle: an endpoint rate cannot tell these apart because it
    is zero-weighted on everything between the ends.
    """
    front_loaded = _call(
        _inputs(("2022-12-31", "100"), ("2023-12-31", "190"), ("2024-12-31", "195"), ("2025-12-31", "200"))
    )
    back_loaded = _call(
        _inputs(("2022-12-31", "100"), ("2023-12-31", "105"), ("2024-12-31", "110"), ("2025-12-31", "200"))
    )
    assert front_loaded.value is not None and back_loaded.value is not None
    # Back-loaded growth is weighted highest, so its rate is larger and its PEG smaller.
    assert back_loaded.value < front_loaded.value


def test_the_window_is_measured_in_elapsed_time_not_calendar_years() -> None:
    """JNJ's real shape: a fiscal year ending 2022-01-02 is FY2021, not FY2022.

    Keying the series on `end.year` made a 1456-day span read as three years. The window
    must be the period closest to three years back in ELAPSED days.
    """
    result = _call(
        _inputs(
            ("2022-01-02", "20880"),
            ("2023-01-01", "17940"),
            ("2023-12-31", "35150"),
            ("2024-12-29", "14070"),
            ("2025-12-28", "26800"),
        )
    )
    assert "window:2023-01-01..2025-12-28" in result.flags, result.flags


def test_a_period_too_far_from_the_window_is_refused_rather_than_stretched() -> None:
    # Only a five-year-old base exists; reporting it as a three-year rate is a different
    # number, and silently substituting one makes issuers incomparable in a ranking.
    result = _call(_inputs(("2020-12-31", "100"), ("2025-12-31", "200")))
    assert result.value is None
    assert "insufficient_earnings_history" in result.flags


def test_a_gap_at_a_year_boundary_refuses_rather_than_reweighting_the_survivors() -> None:
    result = _call(_inputs(("2022-12-31", "100"), ("2023-12-31", "120"), ("2025-12-31", "200")))
    assert result.value is None
    assert "insufficient_earnings_history" in result.flags


def test_a_loss_year_inside_the_window_makes_the_rate_undefined() -> None:
    # A rate across a sign change is not a growth rate, and here it would dominate the mean.
    result = _call(_inputs(("2022-12-31", "100"), ("2023-12-31", "-58"), ("2024-12-31", "150"), ("2025-12-31", "200")))
    assert result.value is None
    assert "insufficient_earnings_history" in result.flags


def test_declining_earnings_resolve_to_a_named_flag_not_a_negative_peg() -> None:
    result = _call(_inputs(("2022-12-31", "200"), ("2023-12-31", "180"), ("2024-12-31", "160"), ("2025-12-31", "150")))
    assert result.value is None
    assert "non_positive_growth" in result.flags


def test_a_loss_making_issuer_has_no_multiple() -> None:
    result = _call(_inputs(("2022-12-31", "100"), ("2023-12-31", "120"), ("2024-12-31", "150"), ("2025-12-31", "-10")))
    assert result.value is None
    assert "non_positive_earnings" in result.flags


@pytest.mark.parametrize(
    ("missing", "flag"),
    [("price", "missing_price"), ("shares_outstanding", "missing_shares_outstanding")],
)
def test_a_missing_market_value_input_is_named_rather_than_defaulted(missing: str, flag: str) -> None:
    facts = [fact for fact in _inputs(*_STEADY) if fact.metric != missing]
    result = _call(facts)
    assert result.value is None
    assert flag in result.flags


def test_no_earnings_at_all_is_distinguishable_from_a_short_history() -> None:
    result = _call([_fact("price", "100"), _fact("shares_outstanding", "1000")])
    assert result.value is None
    assert "missing_net_income" in result.flags


def test_confidence_cannot_exceed_the_weakest_period_in_the_window() -> None:
    """init.md L2: a factor's confidence cannot exceed the minimum it consumed — and the
    rate rests on every period, not just the two ends."""
    facts = _inputs(*_STEADY)
    facts[3] = _annual("2023-12-31", "120", confidence="0.4")  # a middle year, not an endpoint
    result = _call(facts)
    assert result.value is not None
    assert result.confidence == Decimal("0.4")


def test_a_period_tagged_annual_but_shorter_than_a_year_is_not_a_growth_observation() -> None:
    """JNJ published a `:FY:` row spanning six months with the real year absent (#572)."""
    facts = _inputs(*_STEADY)
    facts.append(_fact("net_income", "999", period="FY2099:FY:2025-07-01:2025-12-31"))
    result = _call(facts)
    # The six-month row is skipped, so the four annual periods still describe the window.
    assert result.value is not None
    assert "window:2022-12-31..2025-12-31" in result.flags


def test_two_vintages_of_one_period_are_rejected_rather_than_picked_between() -> None:
    facts = _inputs(*_STEADY)
    facts.append(_annual("2025-12-31", "999"))
    with pytest.raises(ValueError, match="multiple PIT-resolved"):
        _call(facts)


@pytest.mark.parametrize("convention", [GrowthConvention.ANALYST_CONSENSUS, GrowthConvention.COMPANY_GUIDANCE])
def test_an_unsourced_convention_says_so_instead_of_serving_the_historical_number(convention) -> None:
    result = _call(_inputs(*_STEADY), convention=convention)
    assert result.value is None
    assert f"growth_convention_unsourced:{convention.value}" in result.flags


def test_a_window_shorter_than_one_year_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _call(_inputs(*_STEADY), years=0)


def test_the_pinned_qlib_expression_reproduces_the_decimal_peg() -> None:
    """init.md rule 25: Qlib is the factor-expression engine, and the Decimal path is the
    source of truth it must agree with. Module 2 has carried this cross-check since it
    landed; module 1 shipped without one."""
    qlib = pytest.importorskip("qlib")
    del qlib

    from datetime import date

    from factors.base.peg import PEG_EXPRESSION_DEFINITION
    from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY, evaluate_expression
    from truealpha_contracts.qlib_expression import QlibExpressionExecutionBinding

    native = _call(_inputs(*_STEADY))
    assert native.value is not None, native.flags

    session = date(2026, 6, 30)
    _, outputs, _ = evaluate_expression(
        PEG_EXPRESSION_DEFINITION,
        BUILTIN_OPERATOR_REGISTRY,
        panel={
            "price": {_ENTITY: (100.0,)},
            "shares_outstanding": {_ENTITY: (1000.0,)},
            "net_income": {_ENTITY: (200.0,)},
            # the rate the Decimal path derived, fed in as the declared input it is
            "growth_rate": {_ENTITY: (float(Decimal("17") / Decimal(60)),)},
        },
        instruments=(_ENTITY,),
        sessions=(session,),
        execution_binding=QlibExpressionExecutionBinding(
            version="0.9.7",
            release_commit="a" * 40,
            runtime_artifact_sha256="b" * 64,
            runtime_lock_sha256="c" * 64,
            adapter_id="factors.qlib_engine.test",
            adapter_implementation_sha256="d" * 64,
        ),
    )
    assert outputs[(_ENTITY, session)] == pytest.approx(float(native.value), rel=1e-9)
