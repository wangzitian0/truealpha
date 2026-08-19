"""The single-source decision evaluator reproduces the #21 golden exactly (#393).

This replaces the hand-written recompute that used to live in
`truealpha_contracts` `test_strategy.py`: the golden is now the frozen output of
the one evaluator both the replay and this test consume, so there is no second
implementation to drift from.
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from factors.composite.strategy_evaluator import (
    EvaluatedDecision,
    IssuerInput,
    evaluate_cutoff,
    rank_and_select,
)
from truealpha_contracts.strategy import GoldenDecisionOutcome, LargeModelValueV0Definition

_CORPUS_PATH = Path(__file__).parents[2] / "contracts" / "tests" / "fixtures" / "large_model_value_v0_strategy.v1.json"
_CORPUS_SHA256 = "8cdb081d887ff7754ac52a1eb02679b94a1c1c71b1eb32c606c06f5d6fe96083"


def _s(value: object) -> str | None:
    return None if value is None else str(value)


def test_evaluator_reproduces_every_golden_decision_exactly() -> None:
    raw = _CORPUS_PATH.read_bytes()
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
            )
            for decision in decisions
        ]
        as_of = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        evaluated = {
            item.issuer_id: item
            for item in evaluate_cutoff(issuers, definition=definition, cutoff_at=as_of, risk_free_rate=rates[cutoff])
        }
        for decision in decisions:
            expected = decision["expected"]
            got = evaluated[decision["issuer"]["id"]]
            assert _s(got.capital_adjusted_labor_efficiency) == _s(expected["capital_adjusted_labor_efficiency"])
            assert got.tier == expected["tier"]
            assert _s(got.current_price_to_sales) == _s(expected["current_price_to_sales"])
            assert _s(got.target_price_to_sales) == _s(expected["target_price_to_sales"])
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
    corpus = json.loads(_CORPUS_PATH.read_bytes())
    return LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))


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
    # Invariant guarding the #393 regression: an issuer whose P/S sits between its
    # tier-band midpoint and upper bound must NOT be rejected — rejection is only
    # above the upper bound. (The frozen golden never exercises this window, which
    # is exactly why the midpoint-vs-upper divergence went unnoticed.)
    issuer = IssuerInput(
        issuer_id="issuer:inband",
        records={
            "gross_profit": (Decimal("200000"), Decimal("0.9")),  # -> tech-tier labor efficiency 200000
            "total_assets": (Decimal("0"), Decimal("0.9")),
            "headcount": (Decimal("1"), Decimal("0.9")),
            "revenue": (Decimal("1"), Decimal("0.9")),
            "shares_outstanding": (Decimal("5"), Decimal("0.9")),  # -> current P/S = 5.0
            "last_close": (Decimal("1"), Decimal("0.9")),
        },
    )
    cutoff = datetime.fromisoformat("2026-06-30T23:59:59+00:00")
    [decision] = evaluate_cutoff([issuer], definition=_definition(), cutoff_at=cutoff, risk_free_rate=Decimal("0"))

    # tech band [2.50, 6.00], midpoint 4.25: current P/S 5.0 is above the midpoint
    # (negative gap) but below the upper bound -> eligible and ranked, not rejected.
    assert decision.tier == "tech"
    assert decision.current_price_to_sales == Decimal("5.0000")
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
        periodic_records={"net_income": _PEG_SERIES if series is None else series},
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
        [_peg_inputs()], definition=_definition(), cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0")
    )[0]
    assert decision.peg is not None
    assert decision.peg.quantize(Decimal("0.01")) == Decimal("0.50")


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
            periodic_records={"net_income": _SLOW_SERIES},
        ),
    ]
    decisions = rank_and_select(
        evaluate_cutoff(issuers, definition=definition, cutoff_at=_PEG_CUTOFF, risk_free_rate=Decimal("0")),
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
