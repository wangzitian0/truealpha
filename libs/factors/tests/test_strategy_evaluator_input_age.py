"""The evaluator enforces `eligibility.maximum_input_age_days` of the strategy definition (#1255).

The rule tests the latest observation of each REQUIRED input, and nothing else. A periodic input
such as `net_income` has old periods by nature, and a recorded-only input decides nothing. A rule
that read every record or every period would exclude every issuer. Each test below changes one
`knowable_at` and holds every other fact fixed, so the age is the only cause of a result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factors.composite.strategy_evaluator import (
    UNIFORM_LABOR_EFFICIENCY_BINDING,
    EvaluatedDecision,
    IssuerInput,
    _required_inputs,
    evaluate_cutoff,
)
from truealpha_contracts.strategy import (
    ExclusionReason,
    GoldenDecisionOutcome,
    LargeModelValueV0Definition,
    ValuationBasis,
    load_live_strategy_definition,
)

_CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
_FRESH = timedelta(days=3)
#: The inputs that exclude an issuer under the P/GP basis of the live definition (P/S inputs only record).
_REQUIRED = ("gross_profit", "total_assets", "headcount", "shares_outstanding", "last_close")
#: The inputs of the live definition that decide nothing.
_RECORDED_ONLY = ("revenue",)


def _definition() -> LargeModelValueV0Definition:
    return load_live_strategy_definition()


def _records(confidence: str = "0.9") -> dict[str, tuple[Decimal, Decimal]]:
    """Labor efficiency 1,000 (traditional band) and P/GP 5: a candidate below its band, so it ranks."""
    values = {
        "gross_profit": "1000",
        "total_assets": "0",
        "headcount": "1",
        "revenue": "2000",
        "shares_outstanding": "5000",
        "last_close": "1",
    }
    return {key: (Decimal(value), Decimal(confidence)) for key, value in values.items()}


def _issuer(
    ages: dict[str, timedelta] | None = None,
    *,
    records: dict[str, tuple[Decimal, Decimal]] | None = None,
    knowable_at: dict[str, datetime] | None = None,
) -> IssuerInput:
    """An issuer whose inputs all became knowable three days before the cutoff, except those in `ages`."""
    held = _records() if records is None else records
    ages = ages or {}
    return IssuerInput(
        issuer_id="issuer:age",
        records=held,
        knowable_at={key: _CUTOFF - ages.get(key, _FRESH) for key in held} if knowable_at is None else knowable_at,
    )


def _decide(issuer: IssuerInput, definition: LargeModelValueV0Definition | None = None) -> EvaluatedDecision:
    [decision] = evaluate_cutoff(
        [issuer],
        definition=_definition() if definition is None else definition,
        cutoff_at=_CUTOFF,
        risk_free_rate=Decimal("0"),
        labor_efficiency_by_class=UNIFORM_LABOR_EFFICIENCY_BINDING,
    )
    return decision


def _is_stale(decision: EvaluatedDecision) -> bool:
    return decision.exclusion_reason is ExclusionReason.STALE_REQUIRED_INPUT


def test_the_fixture_issuer_is_eligible_when_every_input_is_fresh() -> None:
    """The control of every test below: with fresh inputs the same issuer ranks."""
    decision = _decide(_issuer())

    assert decision.eligible is True
    assert decision.exclusion_reason is None
    assert decision.outcome is GoldenDecisionOutcome.SELECTED


def test_the_live_definition_declares_the_limit_these_tests_probe() -> None:
    assert _definition().eligibility.maximum_input_age_days == 455


@pytest.mark.parametrize("key", _REQUIRED)
@pytest.mark.parametrize(
    ("age", "stale"),
    [
        (timedelta(days=454), False),
        (timedelta(days=455), False),
        (timedelta(days=455, seconds=1), True),
        (timedelta(days=456), True),
    ],
    ids=["454-days", "455-days-limit", "455-days-plus-1-second", "456-days"],
)
def test_a_required_input_is_stale_only_when_older_than_the_limit(key: str, age: timedelta, stale: bool) -> None:
    """Red when the rule is absent (456 days stays eligible), when it uses `>=` (455 days is excluded)
    or when it compares dates instead of instants (455 days and 1 second keeps the same date count)."""
    decision = _decide(_issuer({key: age}))

    assert _is_stale(decision) is stale, decision
    assert decision.eligible is not stale
    if stale:
        assert decision.outcome is GoldenDecisionOutcome.EXCLUDED
        assert decision.confidence is None, "the floor is not evaluated for a stale issuer"
        assert decision.rank is None and decision.target_weight is None


def test_the_limit_is_read_from_the_definition() -> None:
    """A hard-coded 455 would keep an input of 31 days eligible under a 30-day definition."""
    definition = _definition()
    strict = definition.model_copy(
        update={"eligibility": definition.eligibility.model_copy(update={"maximum_input_age_days": 30})}
    )

    assert _is_stale(_decide(_issuer({"headcount": timedelta(days=31)}), strict))
    assert not _is_stale(_decide(_issuer({"headcount": timedelta(days=30)}), strict))
    assert not _is_stale(_decide(_issuer({"headcount": timedelta(days=31)}))), "the live limit is 455 days"


def test_one_stale_input_among_fresh_ones_excludes_the_issuer() -> None:
    ages = {key: timedelta(days=400) for key in _REQUIRED}

    assert not _is_stale(_decide(_issuer(ages)))
    assert _is_stale(_decide(_issuer(ages | {"gross_profit": timedelta(days=500)})))


@pytest.mark.parametrize("key", _RECORDED_ONLY)
def test_a_stale_recorded_only_input_does_not_exclude(key: str) -> None:
    """Under 1.0.0, P/S is recorded and decides nothing, so its own input cannot exclude by age."""
    decision = _decide(_issuer({key: timedelta(days=700)}))

    assert decision.eligible is True, decision
    assert decision.exclusion_reason is None


def test_a_periodic_input_with_ancient_periods_does_not_exclude() -> None:
    """`net_income` is a series whose old periods are 6,000 days old. It is no required input.

    Red when the rule reads every record or every period: every real issuer is then excluded.
    """
    records = _records() | {"net_income": (Decimal("100"), Decimal("0.9"))}
    ancient = _CUTOFF - timedelta(days=6000)
    issuer = IssuerInput(
        issuer_id="issuer:age",
        records=records,
        knowable_at={key: _CUTOFF - _FRESH for key in records} | {"net_income": ancient},
        periodic_records={
            "net_income": {
                f"FY{year}:FY:{year}-01-01:{year}-12-31": (Decimal(100 + year), Decimal("0.9"))
                for year in range(2008, 2012)
            }
        },
    )

    decision = _decide(issuer)

    assert decision.eligible is True, decision
    assert decision.exclusion_reason is None


def test_an_unknown_instant_of_a_required_input_raises_and_names_the_issuer_and_the_key() -> None:
    """A producer that skips a key fails loud. It never passes the issuer in silence."""
    for key in _REQUIRED:
        records = _records()
        knowable_at = {name: _CUTOFF - _FRESH for name in records if name != key}

        with pytest.raises(ValueError, match=rf"issuer:age.*{key}"):
            _decide(IssuerInput(issuer_id="issuer:age", records=records, knowable_at=knowable_at))


def test_an_unknown_instant_raises_even_when_another_required_input_is_stale() -> None:
    """The rule checks every required key before it excludes. A stale key cannot hide a skipped key."""
    records = _records()
    knowable_at = {name: _CUTOFF - _FRESH for name in records if name != "last_close"}
    knowable_at["gross_profit"] = _CUTOFF - timedelta(days=900)

    with pytest.raises(ValueError, match="last_close"):
        _decide(IssuerInput(issuer_id="issuer:age", records=records, knowable_at=knowable_at))


@pytest.mark.parametrize("key", [*_RECORDED_ONLY, "net_income"])
def test_an_unknown_instant_of_an_input_that_cannot_exclude_is_not_an_error(key: str) -> None:
    records = _records() | {"net_income": (Decimal("100"), Decimal("0.9"))}
    knowable_at = {name: _CUTOFF - _FRESH for name in records if name != key}

    decision = _decide(IssuerInput(issuer_id="issuer:age", records=records, knowable_at=knowable_at))

    assert decision.eligible is True, decision


def test_a_missing_input_is_excluded_for_its_absence_before_the_age_is_read() -> None:
    """The missing-input rule comes first. The absent key has no instant, and that raises nothing."""
    records = {key: value for key, value in _records().items() if key != "headcount"}
    knowable_at = {key: _CUTOFF - timedelta(days=900) for key in records}

    decision = _decide(IssuerInput(issuer_id="issuer:age", records=records, knowable_at=knowable_at))

    assert decision.exclusion_reason is ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE


def test_the_age_limit_comes_before_the_confidence_floor() -> None:
    """A stale issuer with low confidence is stale. The same issuer, fresh, is below the floor."""
    low = _records(confidence="0.10")

    stale = _decide(_issuer({"gross_profit": timedelta(days=600)}, records=low))
    fresh = _decide(_issuer(records=low))

    assert stale.exclusion_reason is ExclusionReason.STALE_REQUIRED_INPUT
    assert stale.confidence is None
    assert fresh.exclusion_reason is ExclusionReason.BELOW_CONFIDENCE_FLOOR


def test_a_stale_issuer_does_not_take_a_rank_from_a_fresh_one() -> None:
    fresh = _issuer()
    stale = IssuerInput(
        issuer_id="issuer:old",
        records=_records(),
        knowable_at={key: _CUTOFF - timedelta(days=456) for key in _records()},
    )

    decisions = {
        item.issuer_id: item
        for item in evaluate_cutoff(
            [fresh, stale],
            definition=_definition(),
            cutoff_at=_CUTOFF,
            risk_free_rate=Decimal("0"),
            labor_efficiency_by_class=UNIFORM_LABOR_EFFICIENCY_BINDING,
        )
    }

    assert decisions["issuer:age"].outcome is GoldenDecisionOutcome.SELECTED
    assert decisions["issuer:age"].rank == 1
    assert decisions["issuer:old"].exclusion_reason is ExclusionReason.STALE_REQUIRED_INPUT
    assert decisions["issuer:age"].target_weight == Decimal("1.000000"), "the weight goes to the one eligible issuer"


def test_the_age_rule_and_the_missing_input_rule_share_one_list_of_required_inputs() -> None:
    """The list follows the valuation basis: P/S needs revenue, P/GP records it."""
    by_gross_profit = [key for key, _reason in _required_inputs(ValuationBasis.PRICE_TO_GROSS_PROFIT)]
    by_sales = [key for key, _reason in _required_inputs(ValuationBasis.PRICE_TO_SALES)]

    assert by_gross_profit == list(_REQUIRED)
    assert by_sales == ["gross_profit", "total_assets", "headcount", "revenue", "shares_outstanding", "last_close"]
