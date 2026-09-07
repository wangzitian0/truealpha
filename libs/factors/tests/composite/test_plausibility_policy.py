"""#544 acceptance 1: each invariant fails against a synthetic run reproducing the real
defect it targets. Four named cases, each the production shape that shipped green."""

from __future__ import annotations

from decimal import Decimal

from factors.composite import plausibility_policy as policy
from factors.composite.plausibility_policy import Row, Thresholds, evaluate

D = Decimal


def row(
    listing: str,
    op: str | None,
    ps: str | None,
    *,
    branch: str = "non_financial",
    close: str | None = None,
    availability: str = "available",
) -> Row:
    return Row(
        listing_id=listing,
        operating_branch=branch,
        availability=availability,
        operating_efficiency=D(op) if op is not None else None,
        current_ps=D(ps) if ps is not None else None,
        last_close=D(close) if close is not None else None,
    )


# The 2026-09-06 production head, abbreviated: NVDA is the universe maximum.
PREVIOUS = [
    row("listing:xnas:nvda", "3272605", "25.71", close="180"),
    row("listing:xnas:meta", "1804263", "7.74", close="750"),
    row("listing:xnys:ma", "751418", "15.55", close="560"),
    row("listing:xnys:jpm", "600000", "5.23", branch="financial", close="300"),
]


def test_xom_above_the_universe_maximum_is_caught() -> None:
    """#533: XOM 4,984,153 above NVIDIA's 4,746,260, revenue used as gross profit."""
    current = [*PREVIOUS, row("listing:xnys:xom", "4984153", "1.5", close="110")]
    (violation,) = evaluate(current, PREVIOUS)
    assert violation.rule == policy.RULE_UNIVERSE_MAX and violation.listing_id == "listing:xnys:xom"
    # 1.5x the maximum is the ceiling: NVDA growing 40% on its own is not a defect
    grown = [row("listing:xnas:nvda", "4500000", "25.71", close="180")]
    assert evaluate(grown, PREVIOUS) == []


def test_ma_price_to_sales_collapsing_on_a_flat_price_is_caught() -> None:
    """#533: MA current_price_to_sales 15.55 → 2.17 with no price move of that size."""
    current = [row("listing:xnys:ma", "751418", "2.17", close="558")]
    (violation,) = evaluate(current, PREVIOUS)
    assert violation.rule == policy.RULE_VALUATION_MOVE and violation.listing_id == "listing:xnys:ma"
    # the same move explained by the price is not a defect
    explained = [row("listing:xnys:ma", "751418", "2.17", close="80")]
    assert evaluate(explained, PREVIOUS) == []
    # no price on one side: the rule cannot judge and stays silent
    silent = [row("listing:xnys:ma", "751418", "2.17")]
    assert evaluate(silent, PREVIOUS) == []


def test_a_negative_bank_metric_is_caught() -> None:
    """#528: JPM at -510,498 with +$86.8B of pre-provision profit."""
    current = [row("listing:xnys:jpm", "-514726", "5.23", branch="financial", close="300")]
    (violation,) = evaluate(current, PREVIOUS)
    assert violation.rule == policy.RULE_SIGN_PER_BRANCH and violation.listing_id == "listing:xnys:jpm"
    # a negative NON-financial metric is a different question (a loss-making issuer) and
    # not this rule's; an unavailable row is not judged at all
    assert evaluate([row("listing:xnas:tsla", "-1", "14", close="300")], PREVIOUS) == []
    assert (
        evaluate([row("listing:xnys:jpm", "-1", "5", branch="financial", availability="unavailable")], PREVIOUS) == []
    )


def test_an_empty_eligible_set_is_caught() -> None:
    """#527: 100 % excluded decisions, or an L2 complete count of zero, recorded SUCCESS."""
    all_excluded = evaluate(PREVIOUS, PREVIOUS, outcomes={"excluded": 20}, l2_complete=0)
    assert [v.rule for v in all_excluded] == [policy.RULE_EMPTY_ELIGIBLE, policy.RULE_EMPTY_ELIGIBLE]
    assert evaluate(PREVIOUS, PREVIOUS, outcomes={"selected": 2, "excluded": 18}, l2_complete=17) == []
    # a tick without a strategy passes nothing here and is not judged
    assert evaluate(PREVIOUS, PREVIOUS) == []


def test_the_first_accepted_run_has_no_previous_to_regress_against() -> None:
    current = [row("listing:xnas:nvda", "3272605", "25.71", close="180")]
    assert evaluate(current, []) == []


def test_the_thresholds_are_v1_and_named() -> None:
    assert policy.POLICY_VERSION == "v1"
    assert Thresholds() == Thresholds(D("1.5"), D("2"), D("1.4"))
    assert set(policy.RULES) == {
        "universe-max-regression",
        "unexplained-valuation-move",
        "sign-per-branch",
        "empty-eligible-set",
    }
