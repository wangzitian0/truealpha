"""The single-source decision evaluator reproduces the #21 golden exactly (#393).

This replaces the hand-written recompute that used to live in
`truealpha_contracts` `test_strategy.py`: the golden is now the frozen output of
the one evaluator both the replay and this test consume, so there is no second
implementation to drift from.
"""

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import resources

import pytest
from factors.composite.strategy_evaluator import (
    EvaluatedDecision,
    IssuerInput,
    evaluate_cutoff,
    rank_and_select,
)
from factors.forest import IssuerClass, bound_tree, uniform_tree_pins
from truealpha_contracts.strategy import (
    LIVE_STRATEGY_DEFINITION_RESOURCE,
    ExclusionReason,
    GoldenDecisionOutcome,
    LargeModelValueV0Definition,
    load_live_strategy_definition,
    load_strategy_definition,
    load_strategy_definition_corpus,
)

#: The frozen #21 file: definition 1.0.0 and its golden decisions. The live file is definition 1.1.0.
_FROZEN_RESOURCE = "large_model_value_v0_definition_1_0_0.json"

_CORPUS_SHA256 = "004e716a11f1a9604275d2da5b13160676411fbb8fc7846b25489b43aef31496"


def _s(value: object) -> str | None:
    return None if value is None else str(value)


def _known_before(records: dict, cutoff: datetime, *, days: int = 3) -> dict[str, datetime]:
    """The `knowable_at` of every input: `days` days before the cutoff, far inside the age limit."""
    return dict.fromkeys(records, cutoff - timedelta(days=days))


def test_evaluator_reproduces_every_golden_decision_exactly() -> None:
    raw = resources.files("truealpha_contracts.data").joinpath(_FROZEN_RESOURCE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _CORPUS_SHA256
    corpus = json.loads(raw)
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
    golden = corpus["golden_decision_set"]
    rates = {rate["cutoff_at"]: Decimal(rate["annualized_rate"]) for rate in golden["risk_free_rates"]}

    by_cutoff: dict[str, list[dict]] = {}
    for decision in golden["decisions"]:
        by_cutoff.setdefault(decision["cutoff_at"], []).append(decision)

    verified = 0
    for cutoff, decisions in by_cutoff.items():
        assert len(decisions) == 5
        issuers = [
            IssuerInput(
                issuer_id=decision["issuer"]["id"],
                records={
                    record["input_key"]: (Decimal(record["value"]), Decimal(record["confidence"]))
                    for record in decision["inputs"]
                },
                knowable_at={
                    record["input_key"]: datetime.fromisoformat(record["knowable_at"].replace("Z", "+00:00"))
                    for record in decision["inputs"]
                },
                issuer_class=IssuerClass(decision["issuer_branch"]),
            )
            for decision in decisions
        ]
        as_of = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        evaluated = {
            item.issuer_id: item
            for item in evaluate_cutoff(
                issuers,
                tree_pins=uniform_tree_pins(),
                definition=definition,
                cutoff_at=as_of,
                risk_free_rate=rates[cutoff],
            )
        }
        for decision in decisions:
            expected = decision["expected"]
            got = evaluated[decision["issuer"]["id"]]
            assert _s(got.capital_adjusted_labor_efficiency) == _s(expected["capital_adjusted_labor_efficiency"])
            assert got.tier == expected["tier"]
            assert _s(got.current_price_to_sales) == _s(expected["current_price_to_sales"])
            assert got.target_price_to_sales is None
            assert _s(got.current_price_to_gross_profit) == _s(expected.get("current_price_to_gross_profit"))
            assert _s(got.target_price_to_gross_profit) == _s(expected.get("target_price_to_gross_profit"))
            assert _s(got.valuation_gap) == _s(expected["valuation_gap"])
            assert got.eligible == expected["eligible"]
            assert got.outcome.value == expected["outcome"]
            assert (None if got.exclusion_reason is None else got.exclusion_reason.value) == expected[
                "exclusion_reason"
            ]
            assert got.rank == expected["rank"]
            assert _s(got.target_weight) == _s(expected["target_weight"])
            verified += 1
    assert verified == 10


def _definition() -> LargeModelValueV0Definition:
    return load_live_strategy_definition()


def _candidate(issuer_id: str, gap: str) -> EvaluatedDecision:
    return EvaluatedDecision(
        issuer_id=issuer_id,
        capital_adjusted_labor_efficiency=Decimal("100000"),
        tier="tech",
        current_price_to_sales=Decimal("1.0"),
        target_price_to_sales=Decimal("1.5"),
        valuation_gap=Decimal(gap),
        eligible=True,
        outcome=GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT,
        exclusion_reason=None,
        confidence=Decimal("0.9"),
    )


def test_rank_and_select_selects_top_n_by_descending_gap() -> None:
    # selection_count is 2 in the frozen definition; a third candidate is ranked
    # beyond it (a branch the 5-issuer golden never exercises).
    resolved = {
        d.issuer_id: d
        for d in rank_and_select(
            [_candidate("issuer:a", "0.50"), _candidate("issuer:b", "0.30"), _candidate("issuer:c", "0.10")],
            definition=_definition(),
        )
    }
    assert resolved["issuer:a"].outcome is GoldenDecisionOutcome.SELECTED
    assert resolved["issuer:a"].rank == 1
    assert resolved["issuer:a"].target_weight == Decimal("0.500000")
    assert resolved["issuer:b"].outcome is GoldenDecisionOutcome.SELECTED
    assert resolved["issuer:b"].rank == 2
    assert resolved["issuer:c"].outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT
    assert resolved["issuer:c"].rank == 3
    assert resolved["issuer:c"].target_weight is None


def test_rank_and_select_tie_breaks_by_ascending_issuer_id() -> None:
    resolved = {
        d.issuer_id: d
        for d in rank_and_select(
            [_candidate("issuer:zeta", "0.20"), _candidate("issuer:alpha", "0.20")],
            definition=_definition(),
        )
    }
    assert resolved["issuer:alpha"].rank == 1
    assert resolved["issuer:zeta"].rank == 2


def test_in_band_issuer_is_not_rejected_above_band() -> None:
    # Invariant guarding the #393 regression: an issuer whose P/GP sits between its
    # tier-band midpoint and upper bound must NOT be rejected — rejection is only
    # above the upper bound.
    records = {
        "gross_profit": (Decimal("2000000"), Decimal("0.9")),  # -> tech-tier labor efficiency 2000000
        "total_assets": (Decimal("0"), Decimal("0.9")),
        "headcount": (Decimal("1"), Decimal("0.9")),
        "revenue": (Decimal("1"), Decimal("0.9")),
        "shares_outstanding": (Decimal("26000000"), Decimal("0.9")),  # -> current P/GP = 13.0
        "last_close": (Decimal("1"), Decimal("0.9")),
    }
    cutoff = datetime.fromisoformat("2026-06-30T23:59:59+00:00")
    issuer = IssuerInput(
        issuer_id="issuer:inband",
        records=records,
        knowable_at=_known_before(records, cutoff),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    [decision] = evaluate_cutoff(
        [issuer],
        definition=_definition(),
        cutoff_at=cutoff,
        risk_free_rate=Decimal("0"),
    )

    # tech band [11.43, 14.29], midpoint 12.8600: current P/GP 13.0 is above the midpoint
    # (negative gap) but below the upper bound -> eligible and ranked, not rejected.
    assert decision.tier == "tech"
    assert decision.current_price_to_gross_profit == Decimal("13.0000")
    assert decision.valuation_gap is not None and decision.valuation_gap < 0
    assert decision.outcome is not GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND
    assert decision.eligible


# -- module 1 (PEG) recorded alongside the decision (#284) -----------------------------


_PEG_CUTOFF = datetime.fromisoformat("2026-03-31T00:00:00+00:00")


# A four-year net-income series growing 25% a year, so the recency-weighted rate is also
# 25% and the composed PEG stays the 0.5 these tests were written around. Since 0043 the
# series crosses as periods rather than as a pre-computed scalar.
_PEG_SERIES = {
    "FY2099:FY:2022-01-01:2022-12-31": (Decimal("4096"), Decimal("0.9")),
    "FY2099:FY:2023-01-01:2023-12-31": (Decimal("5120"), Decimal("0.9")),
    "FY2099:FY:2024-01-01:2024-12-31": (Decimal("6400"), Decimal("0.9")),
    "FY2099:FY:2025-01-01:2025-12-31": (Decimal("8000"), Decimal("0.9")),
}


def _peg_inputs(*, series: dict | None = None, **overrides: str) -> IssuerInput:
    """A complete issuer plus the inputs module 1 adds."""
    records = {
        "gross_profit": (Decimal("40000"), Decimal("0.9")),
        "total_assets": (Decimal("100000"), Decimal("0.9")),
        "headcount": (Decimal("100"), Decimal("0.9")),
        "revenue": (Decimal("50000"), Decimal("0.9")),
        "shares_outstanding": (Decimal("1000"), Decimal("0.9")),
        "last_close": (Decimal("100"), Decimal("0.85")),
        # Market cap 100 x 1000 = 100,000 over net income 8,000 is a P/E of 12.5;
        # a 25% grower puts PEG at 0.5.
        "net_income": (Decimal("8000"), Decimal("0.9")),
    }
    for key, value in overrides.items():
        if value == "__absent__":
            records.pop(key, None)
        else:
            records[key] = (Decimal(value), Decimal("0.9"))
    return IssuerInput(
        issuer_id="issuer:peg",
        records=records,
        knowable_at=_known_before(records, _PEG_CUTOFF),
        periodic_records={"net_income": _PEG_SERIES if series is None else series},
        issuer_class=IssuerClass.NON_FINANCIAL,
    )


# 5% a year: a slower grower on the same multiple, so a strictly larger PEG.
_SLOW_SERIES = {
    "FY2099:FY:2022-01-01:2022-12-31": (Decimal("6910.70"), Decimal("0.9")),
    "FY2099:FY:2023-01-01:2023-12-31": (Decimal("7256.24"), Decimal("0.9")),
    "FY2099:FY:2024-01-01:2024-12-31": (Decimal("7619.05"), Decimal("0.9")),
    "FY2099:FY:2025-01-01:2025-12-31": (Decimal("8000"), Decimal("0.9")),
}
# Shrinking earnings: PEG is undefined, not negative.
_DECLINING_SERIES = {
    "FY2099:FY:2022-01-01:2022-12-31": (Decimal("12000"), Decimal("0.9")),
    "FY2099:FY:2023-01-01:2023-12-31": (Decimal("10000"), Decimal("0.9")),
    "FY2099:FY:2024-01-01:2024-12-31": (Decimal("9000"), Decimal("0.9")),
    "FY2099:FY:2025-01-01:2025-12-31": (Decimal("8000"), Decimal("0.9")),
}


def test_the_decision_carries_peg_when_module_1s_inputs_are_present() -> None:
    decision = evaluate_cutoff(
        [_peg_inputs()],
        definition=_definition(),
        cutoff_at=_PEG_CUTOFF,
        risk_free_rate=Decimal("0"),
    )[0]
    assert decision.peg is not None
    assert decision.peg.quantize(Decimal("0.01")) == Decimal("0.50")
    assert decision.peg_reason_codes == (), "a present PEG's window flags are not reasons"


def test_a_missing_growth_rate_leaves_peg_absent_without_excluding_the_issuer() -> None:
    """PEG is recorded, not yet selecting (#284 step 4).

    Until the owner decides how it enters selection, a missing PEG must not change who is
    eligible — otherwise landing the factor would silently move the portfolio.
    """
    decision = evaluate_cutoff(
        [_peg_inputs(series={})],
        definition=_definition(),
        cutoff_at=_PEG_CUTOFF,
        risk_free_rate=Decimal("0"),
    )[0]
    assert decision.peg is None
    assert decision.peg_reason_codes == ("missing_net_income",), "the factor's own reason travels (#837)"
    assert decision.eligible is True
    assert decision.exclusion_reason is None
    # The rest of the decision is untouched by module 1's absence.
    assert decision.capital_adjusted_labor_efficiency is not None
    assert decision.tier is not None


def test_a_non_positive_growth_rate_yields_no_peg_rather_than_a_negative_one() -> None:
    # A shrinking issuer must not read as "cheap" through a negative denominator.
    decision = evaluate_cutoff(
        [_peg_inputs(series=_DECLINING_SERIES)],
        definition=_definition(),
        cutoff_at=_PEG_CUTOFF,
        risk_free_rate=Decimal("0"),
    )[0]
    assert decision.peg is None
    assert decision.peg_reason_codes == ("non_positive_growth",), "named, not a bare absence (#837)"
    assert decision.eligible is True


def test_peg_rank_orders_by_peg_without_touching_selection() -> None:
    """Owner decision (2026-08-17): ranking only, selection later.

    So PEG gets its OWN ordering, recorded alongside, and `rank` / `target_weight` /
    `outcome` stay exactly what the valuation gap made them. Folding PEG into the primary
    rank would move the portfolio, which is the one thing landing module 1 must not do
    before the selection rule is decided.
    """
    definition = _definition()
    issuers = [
        # Cheap on PEG, worse valuation gap.
        _peg_inputs(),
        # Same everything except a weaker growth rate, so a higher (worse) PEG.
        IssuerInput(
            issuer_id="issuer:slow",
            records=_peg_inputs().records,
            knowable_at=_peg_inputs().knowable_at,
            periodic_records={"net_income": _SLOW_SERIES},
            issuer_class=IssuerClass.NON_FINANCIAL,
        ),
    ]
    decisions = rank_and_select(
        evaluate_cutoff(
            issuers,
            definition=definition,
            cutoff_at=_PEG_CUTOFF,
            risk_free_rate=Decimal("0"),
        ),
        definition=definition,
    )
    by_issuer = {d.issuer_id: d for d in decisions}
    fast, slow = by_issuer["issuer:peg"], by_issuer["issuer:slow"]
    assert fast.peg is not None and slow.peg is not None
    assert fast.peg < slow.peg, "a faster grower on the same multiple has the lower PEG"
    # Lower PEG ranks first.
    assert fast.peg_rank == 1
    assert slow.peg_rank == 2


def test_an_issuer_without_a_peg_gets_no_peg_rank() -> None:
    definition = _definition()
    decisions = rank_and_select(
        evaluate_cutoff(
            [_peg_inputs(series={})],
            definition=definition,
            cutoff_at=_PEG_CUTOFF,
            risk_free_rate=Decimal("0"),
        ),
        definition=definition,
    )
    assert decisions[0].peg is None
    assert decisions[0].peg_rank is None, "an absent PEG must not be ranked as if it were the worst"


def test_the_peg_reason_keeps_parameterised_refusals_and_drops_only_window_metadata() -> None:
    """Review on #838: filtering by shape (`":" not in flag`) would have dropped
    `growth_convention_unsourced:<convention>`, a refusal the factor names for the two
    unsourced conventions. Only the window's own metadata prefixes are removed."""
    from datetime import UTC, datetime

    from factors.composite.strategy_evaluator import peg_reason_codes
    from factors.types import FactorResult, UnitFamily

    def result(value, flags):
        return FactorResult(
            factor="peg",
            entity_id="issuer:x",
            value=value,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal("0"),
            as_of=datetime(2026, 9, 15, tzinfo=UTC),
            data_availability="unverified",
            flags=flags,
        )

    assert peg_reason_codes(result(None, ["growth_convention_unsourced:analyst_consensus"])) == (
        "growth_convention_unsourced:analyst_consensus",
    )
    assert peg_reason_codes(result(None, ["non_positive_growth", "cagr_years:3", "window:2022-12-31..2025-12-31"])) == (
        "non_positive_growth",
    )
    assert peg_reason_codes(result(Decimal("0.5"), ["cagr_years:3"])) == ()


def test_the_evaluator_projects_the_keys_the_registry_declares() -> None:
    """#855 B2: the three key tuples this module kept per factor are gone; what is projected
    for a factor is its registered `inputs`, spelled in the strategy's input-key vocabulary
    through the one alias map. The same keys as before, from one declaration."""
    from factors.composite.strategy_evaluator import _input_keys

    assert _input_keys("price_to_sales") == ("last_close", "shares_outstanding", "revenue")
    assert _input_keys("price_to_gross_profit") == ("last_close", "shares_outstanding", "gross_profit")
    assert _input_keys("peg") == ("last_close", "shares_outstanding", "net_income")


def test_nonpositive_price_does_not_crash_evaluator() -> None:
    decisions = evaluate_cutoff(
        [_peg_inputs(last_close="0")],
        definition=_definition(),
        cutoff_at=_PEG_CUTOFF,
        risk_free_rate=Decimal("0"),
    )
    assert len(decisions) == 1
    assert decisions[0].eligible is False
    assert decisions[0].exclusion_reason in (
        ExclusionReason.STALE_REQUIRED_INPUT,
        ExclusionReason.MISSING_MARKET_VALUE_INPUT,
    )


def test_tier_result_none_value_returns_missing_market_value_input(monkeypatch) -> None:
    from factors.composite import strategy_evaluator
    from factors.types import FactorResult, UnitFamily

    def _mock_three_tier(*args, **kwargs):
        return FactorResult(
            factor="three_tier_valuation",
            entity_id="issuer:peg",
            value=None,
            unit_family=UnitFamily.RATIO,
            confidence=Decimal("0.85"),
            as_of=_PEG_CUTOFF,
            data_availability="unverified",
            flags=["nonpositive_price_to_sales"],
        )

    monkeypatch.setattr(strategy_evaluator, "three_tier_valuation", _mock_three_tier)

    decisions = evaluate_cutoff(
        [_peg_inputs()],
        definition=_definition(),
        cutoff_at=_PEG_CUTOFF,
        risk_free_rate=Decimal("0"),
    )
    assert len(decisions) == 1
    assert decisions[0].eligible is False
    assert decisions[0].exclusion_reason == ExclusionReason.MISSING_MARKET_VALUE_INPUT


def test_definition_1_0_0_tiers_a_low_margin_issuer_on_labor_efficiency_only() -> None:
    """1.0.0 has no margin gate: labor efficiency 1,500,000 is tech at a 0.10 margin."""
    records = {
        "gross_profit": (Decimal("1500000"), Decimal("0.9")),
        "total_assets": (Decimal("0"), Decimal("0.9")),
        "headcount": (Decimal("1"), Decimal("0.9")),
        "revenue": (Decimal("15000000"), Decimal("0.9")),
        "shares_outstanding": (Decimal("18000000"), Decimal("0.9")),
        "last_close": (Decimal("1"), Decimal("0.9")),
    }
    decision = _evaluate_v1(
        IssuerInput(
            issuer_id="issuer:thin",
            records=records,
            knowable_at=_known_before(records, _V1_CUTOFF),
            issuer_class=IssuerClass.NON_FINANCIAL,
        )
    )["issuer:thin"]
    assert decision.tier == "tech"
    assert decision.current_price_to_gross_profit == Decimal("12.0000")
    assert decision.target_price_to_gross_profit == Decimal("12.8600")


_TCE_KEYS = (
    "stockholders_equity",
    "preferred_stock_value",
    "goodwill",
    "intangible_assets_net_excluding_goodwill",
)

_BANK_RECORDS_WITH_TCE = {
    "gross_profit": (Decimal("86807000000"), Decimal("0.9")),
    "revenue": (Decimal("150000000000"), Decimal("0.9")),
    "total_assets": (Decimal("4424900000000"), Decimal("0.9")),
    "headcount": (Decimal("318512"), Decimal("0.9")),
    "shares_outstanding": (Decimal("2800000000"), Decimal("0.9")),
    "last_close": (Decimal("200"), Decimal("0.9")),
    "stockholders_equity": (Decimal("340000000000"), Decimal("0.9")),
    "preferred_stock_value": (Decimal("10000000000"), Decimal("0.9")),
    "goodwill": (Decimal("5000000000"), Decimal("0.9")),
    "intangible_assets_net_excluding_goodwill": (Decimal("1000000000"), Decimal("0.9")),
}


def _bank(records: dict) -> IssuerInput:
    return IssuerInput(
        issuer_id="issuer:bank",
        records=records,
        knowable_at=_known_before(records, _PEG_CUTOFF),
        issuer_class=IssuerClass.FINANCIAL,
    )


def test_evaluator_ranks_a_bank_on_measured_tangible_common_equity() -> None:
    # Banking v1 charges measured TCE: 340B equity - 10B preferred - 6B goodwill and intangibles = 324B.
    # (86.807B - 324B * 0.05) / 318,512 = about +$221.7k per employee on this base.
    issuer = _bank(_BANK_RECORDS_WITH_TCE)
    [decision] = evaluate_cutoff(
        [issuer], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    expected = (Decimal("86807000000") - Decimal("324000000000") * Decimal("0.05")) / Decimal("318512")
    assert decision.exclusion_reason is None
    assert decision.capital_adjusted_labor_efficiency == expected.quantize(Decimal("0.01"))


def test_evaluator_excludes_a_bank_without_tangible_common_equity() -> None:
    # No fallback value: the bank is excluded with the named reason, never ranked on total assets.
    records = {key: value for key, value in _BANK_RECORDS_WITH_TCE.items() if key != "goodwill"}
    issuer = _bank(records)
    [decision] = evaluate_cutoff(
        [issuer], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.eligible is False
    assert decision.capital_adjusted_labor_efficiency is None
    assert decision.exclusion_reason is ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY


def test_evaluator_charges_the_uniform_base_to_a_non_financial_issuer_above_fifteen_times() -> None:
    # Assets / gross profit = 50.9 > 15. The uniform charge applies: no 8% base for a non-financial issuer.
    records = {key: value for key, value in _BANK_RECORDS_WITH_TCE.items() if key not in _TCE_KEYS}
    issuer = IssuerInput(
        issuer_id="issuer:acme",
        records=records,
        knowable_at=_known_before(records, _PEG_CUTOFF),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    [decision] = evaluate_cutoff(
        [issuer], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    expected = (Decimal("86807000000") - Decimal("4424900000000") * Decimal("0.05")) / Decimal("318512")
    assert decision.capital_adjusted_labor_efficiency == expected.quantize(Decimal("0.01"))
    assert decision.capital_adjusted_labor_efficiency < 0


def test_the_leverage_flag_is_gone_from_the_evaluator() -> None:
    # The retired eight-percent leverage branch was a ratio test that applied to every issuer.
    # No flag may reach the evaluator: the capital base is chosen by the issuer class, never by a flag.
    import inspect

    assert "financial_leverage_adjusted" not in inspect.signature(evaluate_cutoff).parameters


def test_the_live_definition_ranks_financial_on_banking_and_every_other_class_on_uniform() -> None:
    live = _definition()
    assert bound_tree(live, IssuerClass.FINANCIAL).root == "gppe_banking_tce_v1"
    assert bound_tree(live, IssuerClass.NON_FINANCIAL).root == "gppe_uniform_charge_v0"
    assert bound_tree(live, IssuerClass.INSURANCE).root == "gppe_uniform_charge_v0"


# --- #1176 follow-up: the live path never ranks a classless issuer on a fallback metric -----------


def _without(keys: tuple[str, ...]) -> dict[str, tuple[Decimal, Decimal]]:
    return {key: value for key, value in _BANK_RECORDS_WITH_TCE.items() if key not in keys}


_CLASSLESS_SHAPES = {
    "bank_inputs_with_tce": _BANK_RECORDS_WITH_TCE,
    "bank_inputs_without_tce": _without(_TCE_KEYS),
    "retail_inputs": {
        "gross_profit": (Decimal("35000000000"), Decimal("0.9")),
        "revenue": (Decimal("250000000000"), Decimal("0.9")),
        "total_assets": (Decimal("70000000000"), Decimal("0.9")),
        "headcount": (Decimal("200000"), Decimal("0.9")),
        "shares_outstanding": (Decimal("443000000"), Decimal("0.9")),
        "last_close": (Decimal("500"), Decimal("0.9")),
    },
}


@pytest.mark.parametrize("shape", sorted(_CLASSLESS_SHAPES))
def test_a_live_issuer_without_a_class_is_unavailable_for_labor_efficiency(shape: str) -> None:
    # No class, no labor-efficiency metric. The live path does not fall back to the uniform charge.
    records = _CLASSLESS_SHAPES[shape]
    issuer = IssuerInput(
        issuer_id="issuer:unclassified", records=records, knowable_at=_known_before(records, _PEG_CUTOFF)
    )
    [decision] = evaluate_cutoff(
        [issuer], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.eligible is False
    assert decision.capital_adjusted_labor_efficiency is None
    assert decision.exclusion_reason == "missing_issuer_class"


def test_the_frozen_replay_pins_rank_a_bank_on_the_uniform_charge() -> None:
    # The #21 golden replay passes `uniform_tree_pins()` with the frozen definition 1.0.0. Its bank
    # keeps the total-assets charge that the golden was frozen with. Hand value:
    # (86,807,000,000 - 4,424,900,000,000 x 0.05) / 318,512 = -422,081.43 (2 dp, half-even).
    records = _without(_TCE_KEYS)
    issuer = _bank(records)
    [decision] = evaluate_cutoff(
        [issuer],
        definition=load_strategy_definition(_FROZEN_RESOURCE),
        cutoff_at=_PEG_CUTOFF,
        risk_free_rate=Decimal("0.05"),
        tree_pins=uniform_tree_pins(),
    )
    assert decision.capital_adjusted_labor_efficiency == Decimal("-422081.43")
    assert decision.labor_efficiency_metric == "gppe_uniform_charge_v0"


def test_a_definition_that_pins_no_tree_cannot_be_evaluated_without_the_replay_pins() -> None:
    issuer = IssuerInput(
        issuer_id="issuer:acme",
        records=_without(_TCE_KEYS),
        knowable_at=_known_before(_without(_TCE_KEYS), _PEG_CUTOFF),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    with pytest.raises(ValueError, match="pins no labor-efficiency tree"):
        evaluate_cutoff(
            [issuer],
            definition=load_strategy_definition(_FROZEN_RESOURCE),
            cutoff_at=_PEG_CUTOFF,
            risk_free_rate=Decimal("0.05"),
        )


def test_evaluator_excludes_a_bank_without_preferred_stock_value() -> None:
    # No zero stand-in: a bank without a measured preferred value is unavailable with its own reason.
    records = {key: value for key, value in _BANK_RECORDS_WITH_TCE.items() if key != "preferred_stock_value"}
    issuer = _bank(records)
    [decision] = evaluate_cutoff(
        [issuer], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.eligible is False
    assert decision.capital_adjusted_labor_efficiency is None
    assert decision.exclusion_reason == "missing_preferred_stock_value"


def test_each_decision_names_the_labor_efficiency_metric_it_was_computed_with() -> None:
    # #1176: the name comes from the same binding that selected the metric. A FINANCIAL issuer is named
    # for banking v1, a non-financial issuer for uniform v0, and an issuer with no class carries no name.
    bank = _bank(_BANK_RECORDS_WITH_TCE)
    acme_records = _without(_TCE_KEYS)
    acme = IssuerInput(
        issuer_id="issuer:acme",
        records=acme_records,
        knowable_at=_known_before(acme_records, _PEG_CUTOFF),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    unclassified = IssuerInput(
        issuer_id="issuer:unclassified", records=acme_records, knowable_at=_known_before(acme_records, _PEG_CUTOFF)
    )
    decisions = {
        d.issuer_id: d
        for d in evaluate_cutoff(
            [bank, acme, unclassified],
            definition=_definition(),
            cutoff_at=_PEG_CUTOFF,
            risk_free_rate=Decimal("0.05"),
        )
    }
    assert decisions["issuer:bank"].labor_efficiency_metric == "gppe_banking_tce_v1"
    assert decisions["issuer:acme"].labor_efficiency_metric == "gppe_uniform_charge_v0"
    assert decisions["issuer:unclassified"].labor_efficiency_metric is None


def test_an_exclusion_after_the_metric_is_chosen_still_names_it() -> None:
    # A bank with no preferred value is excluded after its metric was selected. The name is still recorded.
    records = {key: value for key, value in _BANK_RECORDS_WITH_TCE.items() if key != "preferred_stock_value"}
    issuer = _bank(records)
    [decision] = evaluate_cutoff(
        [issuer], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.exclusion_reason == "missing_preferred_stock_value"
    assert decision.labor_efficiency_metric == "gppe_banking_tce_v1"


# -- definition 1.0.0: the P/GP valuation basis (#1235) ---------------------------------


_V1_CORPUS = load_strategy_definition_corpus(_FROZEN_RESOURCE)
_V1_CUTOFF = datetime.fromisoformat("2026-10-08T00:00:00+00:00")


def test_evaluator_reproduces_every_v1_golden_decision_exactly() -> None:
    """The evaluator half of the agreement. The other half, the file against an independent
    Decimal oracle written from the owner's values, is common/contracts/tests/test_strategy_v1.py."""
    definition = load_strategy_definition(_FROZEN_RESOURCE)
    golden = _V1_CORPUS["golden_decision_set"]
    rates = {rate["cutoff_at"]: Decimal(rate["annualized_rate"]) for rate in golden["risk_free_rates"]}
    by_cutoff: dict[str, list[dict]] = {}
    for decision in golden["decisions"]:
        by_cutoff.setdefault(decision["cutoff_at"], []).append(decision)

    verified = 0
    for cutoff, decisions in by_cutoff.items():
        issuers = [
            IssuerInput(
                issuer_id=decision["issuer"]["id"],
                records={
                    record["input_key"]: (Decimal(record["value"]), Decimal(record["confidence"]))
                    for record in decision["inputs"]
                },
                knowable_at={
                    record["input_key"]: datetime.fromisoformat(record["knowable_at"].replace("Z", "+00:00"))
                    for record in decision["inputs"]
                },
                issuer_class=IssuerClass(decision["issuer_branch"]),
            )
            for decision in decisions
        ]
        evaluated = {
            item.issuer_id: item
            for item in evaluate_cutoff(
                issuers,
                tree_pins=uniform_tree_pins(),
                definition=definition,
                cutoff_at=datetime.fromisoformat(cutoff.replace("Z", "+00:00")),
                risk_free_rate=rates[cutoff],
            )
        }
        for decision in decisions:
            expected = decision["expected"]
            got = evaluated[decision["issuer"]["id"]]
            assert _s(got.capital_adjusted_labor_efficiency) == expected["capital_adjusted_labor_efficiency"]
            assert got.tier == expected["tier"]
            assert _s(got.current_price_to_sales) == expected["current_price_to_sales"]
            assert got.target_price_to_sales is None
            assert _s(got.current_price_to_gross_profit) == expected.get("current_price_to_gross_profit")
            assert _s(got.target_price_to_gross_profit) == expected.get("target_price_to_gross_profit")
            assert _s(got.valuation_gap) == expected["valuation_gap"]
            assert got.outcome.value == expected["outcome"]
            assert (None if got.exclusion_reason is None else got.exclusion_reason.value) == expected[
                "exclusion_reason"
            ]
            assert got.rank == expected["rank"]
            assert _s(got.target_weight) == expected["target_weight"]
            verified += 1
    assert verified == 10


def _v1_issuer(
    issuer_id: str, *, labor_efficiency: str, market_value: str, gross_profit: str | None = None
) -> IssuerInput:
    """With no assets and one employee, labor efficiency equals gross profit exactly; a
    separate gross profit (for a nonpositive case) breaks that on purpose."""
    profit = labor_efficiency if gross_profit is None else gross_profit
    records = {
        "gross_profit": (Decimal(profit), Decimal("0.9")),
        "total_assets": (Decimal("0"), Decimal("0.9")),
        "headcount": (Decimal("1"), Decimal("0.9")),
        "revenue": (Decimal(market_value) * 2, Decimal("0.9")),
        "shares_outstanding": (Decimal(market_value), Decimal("0.9")),
        "last_close": (Decimal("1"), Decimal("0.9")),
    }
    return IssuerInput(
        issuer_id=issuer_id,
        records=records,
        knowable_at=_known_before(records, _V1_CUTOFF),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )


def _evaluate_v1(*issuers: IssuerInput) -> dict[str, EvaluatedDecision]:
    return {
        item.issuer_id: item
        for item in evaluate_cutoff(
            list(issuers),
            definition=load_live_strategy_definition(),
            cutoff_at=_V1_CUTOFF,
            risk_free_rate=Decimal("0"),
        )
    }


@pytest.mark.parametrize(
    ("labor_efficiency", "tier", "target"),
    [
        ("999999.99", "traditional", "8.7500"),
        ("1000000", "tech", "12.8600"),
        ("2999999.99", "tech", "12.8600"),
        ("3000000", "large_model_native", "33.3350"),
    ],
)
def test_v1_tier_cuts_are_lower_inclusive_upper_exclusive(labor_efficiency: str, tier: str, target: str) -> None:
    # Market value = 5 x gross profit -> current P/GP 5, inside every band, so each is a candidate.
    market_value = str(Decimal(labor_efficiency) * 5)
    decision = _evaluate_v1(_v1_issuer("issuer:x", labor_efficiency=labor_efficiency, market_value=market_value))[
        "issuer:x"
    ]
    assert decision.tier == tier
    assert decision.current_price_to_gross_profit == Decimal("5.0000")
    assert decision.target_price_to_gross_profit == Decimal(target)
    assert decision.target_price_to_sales is None
    # P/S is still recorded: revenue = 2 x market value -> 0.5.
    assert decision.current_price_to_sales == Decimal("0.5000")
    assert decision.outcome is GoldenDecisionOutcome.SELECTED


def test_v1_rejects_only_above_the_pgp_upper_bound() -> None:
    decisions = _evaluate_v1(
        # traditional band 7.50-10.00: P/GP 10.00 is at the bound, 10.0001 is above it.
        _v1_issuer("issuer:at", labor_efficiency="1000", market_value="10000"),
        _v1_issuer("issuer:above", labor_efficiency="10000", market_value="100001"),
        # P/GP 2 is below the lower bound: it is the cheapest, so it ranks first.
        _v1_issuer("issuer:below", labor_efficiency="1000", market_value="2000"),
    )
    assert decisions["issuer:above"].outcome is GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND
    assert decisions["issuer:above"].current_price_to_gross_profit == Decimal("10.0001")
    assert decisions["issuer:above"].rank is None
    assert decisions["issuer:at"].outcome is GoldenDecisionOutcome.SELECTED
    assert decisions["issuer:below"].rank == 1
    assert decisions["issuer:below"].valuation_gap == Decimal("3.3750")  # 8.75 / 2 - 1


def test_v1_excludes_nonpositive_gross_profit_with_its_own_reason() -> None:
    decision = _evaluate_v1(_v1_issuer("issuer:loss", labor_efficiency="0", market_value="1000", gross_profit="-5"))[
        "issuer:loss"
    ]
    assert decision.outcome is GoldenDecisionOutcome.EXCLUDED
    assert decision.exclusion_reason is ExclusionReason.NONPOSITIVE_GROSS_PROFIT
    assert decision.current_price_to_gross_profit is None


def _recorded_ps_issuer(**revenue: str) -> IssuerInput:
    """Labor efficiency 1,000 (traditional), P/GP 5 (inside 7.50-10.00 is not needed: below it ranks)."""
    records = {
        "gross_profit": (Decimal("1000"), Decimal("0.9")),
        "total_assets": (Decimal("0"), Decimal("0.9")),
        "headcount": (Decimal("1"), Decimal("0.9")),
        "shares_outstanding": (Decimal("5000"), Decimal("0.9")),
        "last_close": (Decimal("1"), Decimal("0.9")),
    }
    if "value" in revenue:
        records["revenue"] = (Decimal(revenue["value"]), Decimal(revenue.get("confidence", "0.9")))
    return IssuerInput(
        issuer_id="issuer:ps",
        records=records,
        knowable_at=_known_before(records, _V1_CUTOFF),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )


@pytest.mark.parametrize(
    "revenue",
    [{"value": "0"}, {"value": "-1"}, {}, {"value": "100", "confidence": "0.10"}],
    ids=["zero", "negative", "absent", "low-confidence"],
)
def test_v1_records_ps_and_never_gates_on_it(revenue: dict[str, str]) -> None:
    """#1235 audit: under 1.0.0, P/S is recorded only. A revenue that gives no P/S (zero,
    negative, absent) or a low-confidence revenue leaves the issuer eligible on P/GP, with
    P/S recorded as unavailable (None) when it cannot be computed."""
    decision = _evaluate_v1(_recorded_ps_issuer(**revenue))["issuer:ps"]
    assert decision.eligible is True
    assert decision.exclusion_reason is None
    assert decision.current_price_to_gross_profit == Decimal("5.0000")
    expected_ps = Decimal("50.0000") if revenue.get("value") == "100" else None
    assert decision.current_price_to_sales == expected_ps


# -- definition 1.1.0: the decision evaluates the pinned tree (#1518) ---------------------------------


def _jpm_fy2025_bank() -> IssuerInput:
    # JPM FY2025 10-K (accession 0001628280-26-008131, repo sample apps/data-engine/samples/filings):
    # equity 362,438M; preferred stock 20,045M; goodwill 52,731M; intangibles 2,600M;
    # employees 318,512; pre-provision profit 86,807M; total assets 4,424,900M.
    records = {
        **_BANK_RECORDS_WITH_TCE,
        "stockholders_equity": (Decimal("362438000000"), Decimal("0.9")),
        "preferred_stock_value": (Decimal("20045000000"), Decimal("0.9")),
        "goodwill": (Decimal("52731000000"), Decimal("0.9")),
        "intangible_assets_net_excluding_goodwill": (Decimal("2600000000"), Decimal("0.9")),
    }
    return _bank(records)


def test_the_live_decision_of_a_bank_is_the_hand_derived_tce_value_not_the_total_assets_value() -> None:
    # Hand computation, rate 0.05:
    #   TCE          = 362,438M - 20,045M - 52,731M - 2,600M = 287,062M
    #   real profit  = 86,807M - 287,062M x 0.05             = 72,453.9M
    #   value        = 72,453.9M / 318,512                   = 227,476.2018... -> 227,476.20 (2 dp, half-even)
    # The total-assets value of the same bank is (86,807M - 4,424,900M x 0.05) / 318,512 = -422,081.43.
    [decision] = evaluate_cutoff(
        [_jpm_fy2025_bank()], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.capital_adjusted_labor_efficiency == Decimal("227476.20")
    assert decision.labor_efficiency_metric == "gppe_banking_tce_v1"


def test_a_reported_zero_preferred_stock_is_a_value_not_a_gap() -> None:
    # TCE = 340,000M - 0 - (5,000M + 1,000M) = 334,000M. Real profit = 86,807M - 334,000M x 0.05 = 70,107M.
    # Value = 70,107M / 318,512 = 220,107.88 (2 dp, half-even).
    records = {**_BANK_RECORDS_WITH_TCE, "preferred_stock_value": (Decimal("0"), Decimal("0.9"))}
    [decision] = evaluate_cutoff(
        [_bank(records)], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.exclusion_reason is None
    assert decision.capital_adjusted_labor_efficiency == Decimal("220107.88")


def test_a_pin_that_differs_from_the_registered_tree_stops_the_evaluation() -> None:
    # The tick fails before it ranks anyone, and no row is written (#1518, U9).
    payload = json.loads(
        json.dumps(load_strategy_definition_corpus(LIVE_STRATEGY_DEFINITION_RESOURCE)["strategy_definition"])
    )
    payload["labor_efficiency_tree_keys"]["financial"]["tree_sha256"] = "0" * 64
    pinned_elsewhere = LargeModelValueV0Definition.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="registered tree has hash"):
        evaluate_cutoff(
            [_jpm_fy2025_bank()], definition=pinned_elsewhere, cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0.05")
        )
