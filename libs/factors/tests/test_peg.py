from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.peg import peg
from factors.types import Fact, GrowthConvention, UnitFamily

_AS_OF = datetime(2026, 6, 30, tzinfo=UTC)
_ENTITY = "company:cik:1045810"
_UNIT_FAMILY = {
    "eps_diluted": UnitFamily.PER_SHARE,
    "net_income": UnitFamily.CURRENCY,
    "price": UnitFamily.PER_SHARE,
}


def _fact(metric: str, value, *, fiscal_year: int | None = None, confidence="0.9") -> Fact:
    return Fact(
        entity_id=_ENTITY,
        metric=metric,
        value=None if value is None else Decimal(str(value)),
        unit_family=_UNIT_FAMILY[metric],
        confidence=Decimal(confidence),
        as_of=_AS_OF,
        # The leading FY tag is the FILING's fiscal year and is deliberately WRONG here:
        # real filings stamp their comparatives with the filing's tag, so keying on it
        # would bucket three years together. The period end is what identifies the year.
        fiscal_period=None if fiscal_year is None else f"FY2099:FY:{fiscal_year - 1}-01-01:{fiscal_year}-12-31",
    )


def _series(*pairs: tuple[int, str], price: str = "100", eps: str | None = None) -> list[Fact]:
    """Growth comes from net_income; the multiple from the latest year's EPS.

    Each pair is (fiscal year, net income). `eps` defaults to the final pair's value
    so a test that only cares about growth still gets a well-formed multiple.
    """
    latest_year, latest_value = pairs[-1]
    return [
        _fact("price", price),
        _fact("eps_diluted", eps if eps is not None else latest_value, fiscal_year=latest_year),
        *[_fact("net_income", value, fiscal_year=year) for year, value in pairs],
    ]


def _call(facts, *, years=3, convention=GrowthConvention.HISTORICAL_CAGR):
    return peg(facts, entity_id=_ENTITY, growth_convention=convention, as_of=_AS_OF, cagr_years=years)


def test_peg_divides_the_multiple_by_growth_in_percentage_points() -> None:
    # EPS doubles over three years: CAGR = 2^(1/3) - 1 = 25.99%. P/E = 100/8 = 12.5.
    # PEG = 12.5 / 25.99 = 0.481.
    result = _call(_series((2022, "4"), (2025, "8")))
    assert result.value is not None
    assert result.value.quantize(Decimal("0.001")) == Decimal("0.481")
    assert result.unit_family is UnitFamily.RATIO
    assert result.confidence == Decimal("0.9")
    # The window is reported, so two results are never silently incomparable.
    assert "cagr_years:3" in result.flags
    assert "base_fiscal_year:2022" in result.flags


def test_a_grower_priced_in_line_lands_near_one() -> None:
    # The property that makes PEG worth computing: 20% growth on a 20x multiple = 1.0.
    # EPS 1.00 -> 1.728 over 3 years is exactly 20%/yr; price 20 gives P/E 11.574.
    result = _call(_series((2022, "1.00"), (2025, "1.728"), price="34.56"))
    assert result.value is not None
    assert result.value.quantize(Decimal("0.01")) == Decimal("1.00")


def test_confidence_cannot_exceed_the_weakest_input() -> None:
    facts = [
        _fact("price", "100", confidence="0.95"),
        _fact("eps_diluted", "8", fiscal_year=2025, confidence="0.90"),
        _fact("net_income", "4", fiscal_year=2022, confidence="0.60"),
        _fact("net_income", "8", fiscal_year=2025, confidence="0.90"),
    ]
    assert _call(facts).confidence == Decimal("0.60")


@pytest.mark.parametrize(
    ("pairs", "expected_flag"),
    [
        # PEG has no meaning for a loss-maker: the multiple itself is undefined.
        ((("2022", "4"), (2025, "-2")), "non_positive_earnings"),
        # A swing from loss to profit has no compound rate — the root of a negative
        # ratio is undefined, and interpolating one would invent the growth.
        (((2022, "-1"), (2025, "8")), "non_positive_base_earnings"),
        # A shrinking company must not be ranked "cheap" by a negative denominator.
        (((2022, "8"), (2025, "4")), "non_positive_growth"),
        # Flat earnings divide by zero.
        (((2022, "5"), (2025, "5")), "non_positive_growth"),
    ],
)
def test_degenerate_cases_resolve_to_a_named_flag_not_a_number(pairs, expected_flag) -> None:
    normalised = [(int(year), eps) for year, eps in pairs]
    result = _call(_series(*normalised))
    assert result.value is None, f"{expected_flag} must not produce a number"
    assert result.confidence == Decimal("0")
    assert expected_flag in result.flags


def test_the_window_is_not_stretched_to_whatever_history_exists() -> None:
    # Only 2023 and 2025 are on file; a 3-year window has no 2022 base. Computing a
    # 2-year CAGR instead would make this issuer incomparable with the rest of a
    # ranking that used 3.
    result = _call(_series((2023, "4"), (2025, "8")), years=3)
    assert result.value is None
    assert "missing_base_year:2022" in result.flags


def test_missing_or_non_positive_price_is_flagged_separately_from_earnings() -> None:
    assert "missing_price" in _call([_fact("net_income", "8", fiscal_year=2025)]).flags
    negative = _call(_series((2022, "4"), (2025, "8"), price="-1"))
    assert negative.value is None
    assert "non_positive_price" in negative.flags


def test_one_observation_cannot_produce_a_growth_rate() -> None:
    assert "insufficient_earnings_history" in _call(_series((2025, "8"))).flags


@pytest.mark.parametrize("convention", [GrowthConvention.ANALYST_CONSENSUS, GrowthConvention.COMPANY_GUIDANCE])
def test_an_unsourced_convention_says_so_rather_than_substituting_historical(convention) -> None:
    # Both are declared in the enum and neither has a feed behind it. Quietly serving
    # the historical number under a consensus label would be the worst outcome.
    result = _call(_series((2022, "4"), (2025, "8")), convention=convention)
    assert result.value is None
    assert f"growth_convention_unsourced:{convention.value}" in result.flags


def test_two_vintages_of_one_fiscal_year_are_rejected_rather_than_picked_between() -> None:
    # Choosing among vintages is the staging layer's job (init.md Section 6).
    facts = _series((2022, "4"), (2025, "8")) + [_fact("net_income", "9", fiscal_year=2025)]
    with pytest.raises(ValueError, match="period ending in 2025"):
        _call(facts)


def test_a_window_shorter_than_one_year_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _call(_series((2022, "4"), (2025, "8")), years=0)


def test_a_period_tagged_annual_but_shorter_than_a_year_is_not_a_growth_observation() -> None:
    """JNJ's real shape: a `:FY:` row covering six months, and no true FY2022.

    Trusting the tag made a 4.81B half-year the base of a three-year window against
    a 26.80B full year — a 77%/yr CAGR that put a slow grower at PEG 0.31.
    """
    half_year = Fact(
        entity_id=_ENTITY,
        metric="net_income",
        value=Decimal("4.81"),
        unit_family=UnitFamily.CURRENCY,
        confidence=Decimal("0.9"),
        as_of=_AS_OF,
        fiscal_period="FY2022:FY:2022-01-02:2022-07-03",
    )
    result = _call([*_series((2019, "15.12"), (2025, "26.80")), half_year], years=3)
    assert result.value is None
    # The half-year is skipped, so 2022 has no observation at all — an honest gap
    # rather than a fabricated growth rate.
    assert "missing_base_year:2022" in result.flags


def test_the_pinned_qlib_expression_reproduces_the_decimal_peg() -> None:
    """init.md rule 25: Qlib is the factor-expression engine, and the Decimal path is the
    source of truth it must agree with.

    Module 2 and module 7 have carried this cross-check since they landed; module 1
    shipped without it, so a pinned-Qlib execution could reproduce every base factor
    except the one `qlib_engine`'s docstring names first. Same panel through both paths,
    same number, or this fails.
    """
    qlib = pytest.importorskip("qlib")
    del qlib

    from datetime import date

    from factors.base.peg import PEG_EXPRESSION_DEFINITION, peg_from_rate
    from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY, evaluate_expression
    from truealpha_contracts.qlib_expression import QlibExpressionExecutionBinding

    price, shares, net_income, growth = Decimal("170.00"), Decimal("2000000"), Decimal("40000000"), Decimal("0.35")

    native = peg_from_rate(
        [
            _fact("price", price),
            Fact(
                entity_id=_ENTITY,
                metric="shares_outstanding",
                value=shares,
                unit_family=UnitFamily.COUNT,
                confidence=Decimal("0.9"),
                as_of=_AS_OF,
            ),
            _fact("net_income", net_income),
        ],
        entity_id=_ENTITY,
        as_of=_AS_OF,
        growth_rate=growth,
    )
    assert native.value is not None, native.flags

    session = date(2026, 6, 30)
    _, outputs, _ = evaluate_expression(
        PEG_EXPRESSION_DEFINITION,
        BUILTIN_OPERATOR_REGISTRY,
        panel={
            "price": {_ENTITY: (float(price),)},
            "shares_outstanding": {_ENTITY: (float(shares),)},
            "net_income": {_ENTITY: (float(net_income),)},
            "growth_rate": {_ENTITY: (float(growth),)},
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

    # (170 * 2,000,000) / 40,000,000 = 8.5 multiple; / (0.35 * 100) = 0.242857…
    assert outputs[(_ENTITY, session)] == pytest.approx(float(native.value), rel=1e-9)
