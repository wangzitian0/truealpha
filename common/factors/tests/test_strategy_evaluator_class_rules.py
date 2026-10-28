"""#1518 step 4: the input rules of the composite follow the tree that the definition pins for the class.

The missing-input rule, the input-age limit and the confidence floor read the inputs that the pinned tree
consumes for the issuer class (`required_input_keys`). A bank consumes its four tangible-common-equity
leaves and no total assets. Every other class consumes total assets and no leaf. The TOPT core reads the
same tree, so one issuer is available in both engines or in neither.

Each test changes one fact and holds the rest fixed. So that one fact is the only cause of the result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factors.composite.strategy_evaluator import EvaluatedDecision, IssuerInput, evaluate_cutoff
from factors.forest import IssuerClass
from truealpha_contracts.strategy import ExclusionReason, load_live_strategy_definition

_CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
_FRESH = timedelta(days=3)
_RATE = Decimal("0.05")
_TCE_LEAVES = ("stockholders_equity", "preferred_stock_value", "goodwill", "intangible_assets_net_excluding_goodwill")

#: A bank whose labor efficiency and P/GP rank. Values from the JPM FY2025 10-K, as in `test_strategy_evaluator`.
_BANK = {
    "gross_profit": "86807000000",
    "revenue": "150000000000",
    "total_assets": "4424900000000",
    "headcount": "318512",
    "shares_outstanding": "2800000000",
    "last_close": "200",
    "stockholders_equity": "362438000000",
    "preferred_stock_value": "20045000000",
    "goodwill": "52731000000",
    "intangible_assets_net_excluding_goodwill": "2600000000",
}
_OTHER = {key: value for key, value in _BANK.items() if key not in _TCE_LEAVES}


def _issuer(
    values: dict[str, str],
    issuer_class: IssuerClass,
    *,
    drop: tuple[str, ...] = (),
    confidence: dict[str, str] | None = None,
    age: dict[str, timedelta] | None = None,
    **overrides: str,
) -> IssuerInput:
    held = {key: value for key, value in {**values, **overrides}.items() if key not in drop}
    confidence = confidence or {}
    age = age or {}
    return IssuerInput(
        issuer_id="issuer:class-rules",
        records={key: (Decimal(value), Decimal(confidence.get(key, "0.9"))) for key, value in held.items()},
        knowable_at={key: _CUTOFF - age.get(key, _FRESH) for key in held},
        issuer_class=issuer_class,
    )


def _decide(issuer: IssuerInput) -> EvaluatedDecision:
    [decision] = evaluate_cutoff(
        [issuer], definition=load_live_strategy_definition(), cutoff_at=_CUTOFF, risk_free_rate=_RATE
    )
    return decision


def test_the_controls_are_eligible() -> None:
    """The two fixtures rank when nothing is changed. Every test below changes one fact of them."""
    assert _decide(_issuer(_BANK, IssuerClass.FINANCIAL)).exclusion_reason is None
    assert _decide(_issuer(_OTHER, IssuerClass.NON_FINANCIAL)).exclusion_reason is None


def test_a_bank_without_total_assets_is_computed_like_the_core_computes_it() -> None:
    """The banking tree consumes no total assets. Red on main: the composite gave `missing_total_assets_fact`."""
    decision = _decide(_issuer(_BANK, IssuerClass.FINANCIAL, drop=("total_assets",)))

    assert decision.exclusion_reason is None, decision
    assert (
        decision.capital_adjusted_labor_efficiency
        == _decide(_issuer(_BANK, IssuerClass.FINANCIAL)).capital_adjusted_labor_efficiency
    )


def test_a_non_financial_issuer_without_total_assets_is_excluded_with_its_reason() -> None:
    decision = _decide(_issuer(_OTHER, IssuerClass.NON_FINANCIAL, drop=("total_assets",)))

    assert decision.exclusion_reason is ExclusionReason.MISSING_TOTAL_ASSETS_FACT


@pytest.mark.parametrize(
    ("leaf", "reason"),
    [
        ("stockholders_equity", ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
        ("goodwill", ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
        ("intangible_assets_net_excluding_goodwill", ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
        ("preferred_stock_value", ExclusionReason.MISSING_PREFERRED_STOCK_VALUE),
    ],
)
def test_a_bank_without_a_leaf_is_excluded_with_the_reason_of_that_leaf(leaf: str, reason: ExclusionReason) -> None:
    assert _decide(_issuer(_BANK, IssuerClass.FINANCIAL, drop=(leaf,))).exclusion_reason is reason


def test_a_total_assets_value_older_than_the_limit_does_not_exclude_a_bank() -> None:
    """The age rule tests the inputs of the pinned tree only. A bank does not consume total assets."""
    limit = timedelta(days=load_live_strategy_definition().eligibility.maximum_input_age_days)
    decision = _decide(_issuer(_BANK, IssuerClass.FINANCIAL, age={"total_assets": limit + timedelta(days=1)}))

    assert decision.exclusion_reason is None, decision


@pytest.mark.parametrize("leaf", _TCE_LEAVES)
def test_a_leaf_older_than_the_limit_excludes_a_bank_as_stale(leaf: str) -> None:
    """Red on main: the age rule skipped the four leaves, so a leaf of any age ranked the bank."""
    limit = timedelta(days=load_live_strategy_definition().eligibility.maximum_input_age_days)

    decision = _decide(_issuer(_BANK, IssuerClass.FINANCIAL, age={leaf: limit + timedelta(days=1)}))

    assert decision.exclusion_reason is ExclusionReason.STALE_REQUIRED_INPUT, decision


@pytest.mark.parametrize("leaf", _TCE_LEAVES)
def test_a_leaf_exactly_at_the_limit_does_not_exclude_a_bank(leaf: str) -> None:
    limit = timedelta(days=load_live_strategy_definition().eligibility.maximum_input_age_days)

    assert _decide(_issuer(_BANK, IssuerClass.FINANCIAL, age={leaf: limit})).exclusion_reason is None


def test_a_low_confidence_total_assets_value_does_not_floor_a_bank() -> None:
    """The floor reads the consumed inputs only. Red on main: the minimum ran over every record."""
    floor = load_live_strategy_definition().eligibility.minimum_confidence
    below = str(floor - Decimal("0.1"))

    decision = _decide(_issuer(_BANK, IssuerClass.FINANCIAL, confidence={"total_assets": below}))

    assert decision.exclusion_reason is None, decision
    assert decision.confidence == Decimal("0.9")


@pytest.mark.parametrize("leaf", _TCE_LEAVES)
def test_a_low_confidence_leaf_floors_a_bank(leaf: str) -> None:
    floor = load_live_strategy_definition().eligibility.minimum_confidence
    below = str(floor - Decimal("0.1"))

    decision = _decide(_issuer(_BANK, IssuerClass.FINANCIAL, confidence={leaf: below}))

    assert decision.exclusion_reason is ExclusionReason.BELOW_CONFIDENCE_FLOOR, decision
    assert decision.confidence == Decimal(below)


def test_a_low_confidence_record_that_no_tree_consumes_does_not_floor_an_issuer() -> None:
    """A bank leaf on a non-financial issuer, and the series of a recorded-only factor, decide nothing."""
    floor = load_live_strategy_definition().eligibility.minimum_confidence
    below = str(floor - Decimal("0.1"))
    stray = _issuer(
        _OTHER, IssuerClass.NON_FINANCIAL, confidence={"goodwill": below, "revenue": below}, goodwill="1000"
    )

    decision = _decide(stray)

    assert decision.exclusion_reason is None, decision


def test_a_zero_headcount_gives_the_reason_that_the_core_gives() -> None:
    """The core says `nonpositive_headcount`. Red on main: the composite said `stale_required_input`."""
    for issuer in (
        _issuer(_BANK, IssuerClass.FINANCIAL, headcount="0"),
        _issuer(_OTHER, IssuerClass.NON_FINANCIAL, headcount="0"),
    ):
        assert _decide(issuer).exclusion_reason is ExclusionReason.NONPOSITIVE_HEADCOUNT


def test_an_issuer_without_a_class_is_excluded_for_its_class_before_any_input_rule() -> None:
    """No class means no pinned tree and so no list of consumed inputs. The class reason comes first."""
    decision = _decide(IssuerInput(issuer_id="issuer:none", records={}, knowable_at={}))

    assert decision.exclusion_reason is ExclusionReason.MISSING_ISSUER_CLASS
    assert decision.labor_efficiency_metric is None


@pytest.mark.parametrize(
    ("issuer_class", "values", "metric"),
    [
        (IssuerClass.FINANCIAL, _BANK, "gppe_banking_tce_v1"),
        (IssuerClass.NON_FINANCIAL, _OTHER, "gppe_uniform_charge_v0"),
    ],
)
def test_an_exclusion_by_an_input_rule_names_the_metric_of_the_pinned_tree(
    issuer_class: IssuerClass, values: dict[str, str], metric: str
) -> None:
    """The class is known when the input rules run, so the decision names the metric of its tree."""
    limit = timedelta(days=load_live_strategy_definition().eligibility.maximum_input_age_days)
    missing = _decide(_issuer(values, issuer_class, drop=("headcount",)))
    stale = _decide(_issuer(values, issuer_class, age={"headcount": limit + timedelta(days=1)}))
    floored = _decide(_issuer(values, issuer_class, confidence={"headcount": "0.1"}))

    assert missing.exclusion_reason is ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE
    assert stale.exclusion_reason is ExclusionReason.STALE_REQUIRED_INPUT
    assert floored.exclusion_reason is ExclusionReason.BELOW_CONFIDENCE_FLOOR
    assert {missing.labor_efficiency_metric, stale.labor_efficiency_metric, floored.labor_efficiency_metric} == {metric}
