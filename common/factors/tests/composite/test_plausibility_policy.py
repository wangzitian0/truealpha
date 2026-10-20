"""#544 acceptance 1: each rule fails against a synthetic run reproducing the real defect it
targets. Four named cases, each the production shape that shipped green.

v2 (#528): the sign rule judges the metric-forest node's declared policy instead of v1's
"a financial branch cannot be negative", which contradicted the definition it judged.

v4 (#1443, #1323): a refusal comes only from an independent reference or the definition. The
two rules that read the previous accepted run are review flags. A refusal names one issuer; the
run is refused only when too large a share of it is refused, or when nothing is eligible."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest
from factors.composite import plausibility_policy as policy
from factors.composite.plausibility_policy import (
    IndependentReference,
    Row,
    Thresholds,
    evaluate,
    evaluate_issuers,
    reviews,
    sign_signals,
)
from factors.forest import IssuerClass, SignPolicy, published_node
from truealpha_contracts.strategy import ExclusionReason

D = Decimal


def row(
    listing: str,
    op: str | None,
    ps: str | None,
    *,
    branch: str = "non_financial",
    close: str | None = None,
    availability: str = "available",
    revenue_period_end: date | None = None,
    revenue: str | None = None,
    gross_profit: str | None = None,
    operating_period_end: date | None = None,
    proxy: bool = False,
    references: tuple[IndependentReference, ...] = (),
) -> Row:
    return Row(
        listing_id=listing,
        operating_branch=branch,
        availability=availability,
        operating_efficiency=D(op) if op is not None else None,
        current_ps=D(ps) if ps is not None else None,
        last_close=D(close) if close is not None else None,
        revenue_period_end=revenue_period_end,
        revenue=D(revenue) if revenue is not None else None,
        gross_profit=D(gross_profit) if gross_profit is not None else None,
        operating_period_end=operating_period_end,
        gross_profit_is_revenue_proxy=proxy,
        references=references,
    )


def reference(figures: dict[date, dict[str, str | None]]) -> IndependentReference:
    return IndependentReference(
        reference_id="normalized-observation:" + "a" * 64,
        figures={
            end: {name: D(value) if value is not None else None for name, value in values.items()}
            for end, values in figures.items()
        },
    )


# The 2026-09-06 production head, abbreviated: NVDA is the universe maximum.
PREVIOUS = [
    row("listing:xnas:nvda", "3272605", "25.71", close="180"),
    row("listing:xnas:meta", "1804263", "7.74", close="750"),
    row("listing:xnys:ma", "751418", "15.55", close="560"),
    row("listing:xnys:jpm", "600000", "5.23", branch="financial", close="300"),
]

FY25 = date(2025, 12, 31)


def test_xom_above_the_universe_maximum_is_a_review_and_its_revenue_proxy_is_refused_by_the_reference() -> None:
    """#533: XOM 4,984,153 above NVIDIA's 4,746,260, revenue used as gross profit. Against our own
    previous run the jump is a review flag. Against the independent reference the gross profit is
    3.4x what the reference reports for the same year, which refuses XOM."""
    xom = row(
        "listing:xnys:xom",
        "4984153",
        "1.5",
        close="110",
        revenue="339247000000",
        revenue_period_end=FY25,
        gross_profit="339247000000",
        operating_period_end=FY25,
        references=(reference({FY25: {"revenue": "339247000000", "gross_profit": "99900000000"}}),),
    )
    current = [*PREVIOUS, xom]
    (review,) = reviews(current, PREVIOUS)
    assert review.rule == policy.REVIEW_UNIVERSE_MAX and review.listing_id == "listing:xnys:xom"
    (violation,) = evaluate(current)
    assert violation.rule == policy.RULE_INDEPENDENT_REFERENCE and violation.listing_id == "listing:xnys:xom"
    assert "gross_profit 339,247,000,000 for the period ending 2025-12-31 is 3.40x" in violation.detail
    # 1.5x the maximum is the ceiling: NVDA growing 40% on its own is not even a review
    grown = [row("listing:xnas:nvda", "4500000", "25.71", close="180")]
    assert reviews(grown, PREVIOUS) == [] and evaluate(grown) == []


def test_ma_price_to_sales_collapsing_on_a_flat_price_is_a_review_not_a_refusal() -> None:
    """#533: MA current_price_to_sales 15.55 → 2.17 with no price move of that size. The share
    count behind it is refused at the source since #529; against our own previous run it is a review."""
    current = [row("listing:xnys:ma", "751418", "2.17", close="558")]
    assert evaluate(current) == []
    (review,) = reviews(current, PREVIOUS)
    assert review.rule == policy.REVIEW_VALUATION_MOVE and review.listing_id == "listing:xnys:ma"
    assert "versus the previous run" in review.detail
    # the same move explained by the price is not even a review
    assert reviews([row("listing:xnys:ma", "751418", "2.17", close="80")], PREVIOUS) == []
    # no price on one side: the detector cannot judge and stays silent
    assert reviews([row("listing:xnys:ma", "751418", "2.17")], PREVIOUS) == []


# #1236: MU's real values. Micron filed its FY2026 10-K on 2026-10-09 (accession
# 0000723125-26-000023): revenue 133,188,000,000 for the year ending 2026-09-03, against
# 37,378,000,000 for the year ending 2025-08-28 (SEC companyconcept).
MU_FY25, MU_FY26 = date(2025, 8, 28), date(2026, 9, 3)
MU_REFERENCE = reference(
    {
        MU_FY25: {"revenue": "37378000000", "gross_profit": "14873000000"},
        MU_FY26: {"revenue": "133188000000", "gross_profit": "107504000000"},
    }
)
MU_BEFORE = row(
    "listing:xnas:mu",
    "154102",
    "31.29837341569479372893145700679544",
    close="1035.84",
    revenue_period_end=MU_FY25,
    revenue="37378000000",
    gross_profit="14873000000",
    operating_period_end=MU_FY25,
    references=(MU_REFERENCE,),
)
MU_AFTER = row(
    "listing:xnas:mu",
    "154102",
    "8.741286641048743130011712766915938",
    close="1029.00",
    revenue_period_end=MU_FY26,
    revenue="133188000000",
    gross_profit="107504000000",
    operating_period_end=MU_FY26,
    references=(MU_REFERENCE,),
)


def test_a_figure_the_reference_supersedes_is_refused_and_the_correction_is_accepted() -> None:
    """#1443: MU's FY2025 revenue served while the reference reports FY2026 at 3.56x is refused;
    the FY2026 figure that agrees with the reference is accepted with no review."""
    refusals = evaluate_issuers([MU_BEFORE])
    assert {(v.rule, v.listing_id) for v in refusals} == {(policy.RULE_INDEPENDENT_REFERENCE, "listing:xnas:mu")}
    revenue = next(v for v in refusals if v.detail.startswith("revenue"))
    assert "later period ending 2026-09-03 (3.56x)" in revenue.detail and "superseded" in revenue.detail
    assert evaluate_issuers([MU_AFTER]) == []
    # v3's fiscal-year explanation stands in the change detector
    assert reviews([MU_AFTER], [MU_BEFORE]) == []


def test_the_reference_judges_within_the_ratio_and_only_like_for_like() -> None:
    within = row(
        "listing:x",
        "1",
        "5",
        revenue="200",
        revenue_period_end=FY25,
        references=(reference({FY25: {"revenue": "100", "gross_profit": None}}),),
    )
    # exactly 2x is the limit, not over it
    assert evaluate([within]) == []
    over = row(
        "listing:x",
        "1",
        "5",
        revenue="201",
        revenue_period_end=FY25,
        references=(reference({FY25: {"revenue": "100", "gross_profit": None}}),),
    )
    assert [v.rule for v in evaluate_issuers([over])] == [policy.RULE_INDEPENDENT_REFERENCE]
    # agreement on the served period does not make it current: a later period 9x off supersedes it,
    # and a later period within the ratio (a normal year of growth before the primary files) does not
    later = date(2026, 12, 31)
    superseded = row(
        "listing:x",
        "1",
        "5",
        revenue="100",
        revenue_period_end=FY25,
        references=(reference({FY25: {"revenue": "100"}, later: {"revenue": "900"}}),),
    )
    (violation,) = evaluate_issuers([superseded])
    assert "later period ending 2026-12-31 (9.00x)" in violation.detail
    grown = row(
        "listing:x",
        "1",
        "5",
        revenue="100",
        revenue_period_end=FY25,
        references=(reference({FY25: {"revenue": "100"}, later: {"revenue": "130"}}),),
    )
    assert evaluate([grown]) == []
    # the #533 revenue proxy is another quantity than the reference's gross profit (PYPL: GP = revenue)
    proxy = row(
        "listing:xnas:pypl",
        "1",
        "5",
        revenue="33172000000",
        revenue_period_end=FY25,
        gross_profit="33172000000",
        operating_period_end=FY25,
        proxy=True,
        references=(reference({FY25: {"revenue": "33172000000", "gross_profit": "15465000000"}}),),
    )
    assert evaluate([proxy]) == []
    # a bank's operating numerator is not gross profit either
    bank = row(
        "listing:xnys:jpm",
        "1",
        "5",
        branch="financial",
        gross_profit="100",
        operating_period_end=FY25,
        references=(reference({FY25: {"gross_profit": "10"}}),),
    )
    assert evaluate([bank]) == []
    # no reference, or a figure the reference does not report: nothing to judge
    assert evaluate([row("listing:x", "1", "5", revenue="201", revenue_period_end=FY25)]) == []
    silent = reference({FY25: {"revenue": None}})
    assert evaluate([row("listing:x", "1", "5", revenue="201", revenue_period_end=FY25, references=(silent,))]) == []
    # an unavailable row is judged too: its served figures still reach the strategy decision
    unavailable = row("listing:x", None, None, availability="unavailable", revenue="201", revenue_period_end=FY25,
                      references=(reference({FY25: {"revenue": "100"}}),))  # fmt: skip
    assert [v.rule for v in evaluate_issuers([unavailable])] == [policy.RULE_INDEPENDENT_REFERENCE]


def test_a_negative_bank_metric_is_a_declared_signal_not_a_violation() -> None:
    """#528: JPM at -514,726 is what GPPE v0.2.0 (#59) defines as a valid low signal. v1
    refused it under an expiring exemption; v2 accepts it and names it."""
    jpm = row("listing:xnys:jpm", "-514726", "5.23", branch="financial", close="300")
    assert evaluate([jpm]) == []
    (signal,) = sign_signals([jpm])
    assert signal.rule == policy.RULE_NODE_SIGN_POLICY and signal.listing_id == "listing:xnys:jpm"
    assert "sign-is-signal" in signal.detail and "-514,726.00" in signal.detail
    # the same holds for every class the node declares, and an unavailable row is not judged
    assert sign_signals([row("listing:xnas:tsla", "-1", "14", close="300")])
    assert sign_signals([row("listing:xnys:jpm", "-1", "5", branch="financial", availability="unavailable")]) == []
    # a non-negative value says nothing
    assert sign_signals(PREVIOUS) == []


@pytest.fixture
def strict_financial(monkeypatch: pytest.MonkeyPatch) -> None:
    node = published_node(policy.JUDGED_TABLE, policy.JUDGED_COLUMN)
    strict = node.model_copy(
        update={"sign_policy": {**node.sign_policy, IssuerClass.FINANCIAL: SignPolicy.MUST_BE_NON_NEGATIVE}}
    )
    monkeypatch.setattr(policy, "published_node", lambda table, column: strict)


def test_a_sign_the_node_forbids_is_caught(strict_financial: None) -> None:
    """The rule is only as strict as the definition: where a node forbids a negative value,
    the v1 shape is caught exactly as before."""
    current = [row("listing:xnys:jpm", "-514726", "5.23", branch="financial", close="300")]
    (violation,) = evaluate_issuers(current)
    assert violation.rule == policy.RULE_NODE_SIGN_POLICY and violation.listing_id == "listing:xnys:jpm"
    assert "must-be-non-negative" in violation.detail
    assert sign_signals(current) == []
    # the non-financial class still declares a signal, and an unavailable row is not judged
    assert evaluate([row("listing:xnas:tsla", "-1", "14", close="300")]) == []
    assert evaluate([row("listing:xnys:jpm", "-1", "5", branch="financial", availability="unavailable")]) == []


def test_a_value_for_a_class_the_node_does_not_declare_is_caught() -> None:
    """No policy vouches for a branch the forest does not know, whatever the sign."""
    current = [row("listing:xnys:new", "10", "5", branch="sovereign_fund", close="300")]
    (violation,) = evaluate_issuers(current)
    assert violation.rule == policy.RULE_NODE_SIGN_POLICY and "no policy for this issuer class" in violation.detail


def _universe(refused: int, size: int = 20) -> list[Row]:
    stale = reference({FY25: {"revenue": "100"}})
    return [
        row(f"listing:{index:02d}", "1", "5", revenue="900" if index < refused else "100", revenue_period_end=FY25,
            references=(stale,))
        for index in range(size)
    ]  # fmt: skip


def test_one_refused_issuer_never_refuses_the_run_and_a_large_share_does() -> None:
    """#1323: up to 25% of the run's rows refused are filings or splits, issuer by issuer. More is a
    data fault: 6 of 20 refuses the run, 5 of 20 does not."""
    five = evaluate(_universe(5))
    assert len(five) == 5 and {v.rule for v in five} == {policy.RULE_INDEPENDENT_REFERENCE}
    six = evaluate(_universe(6))
    run_refusals = [v for v in six if v.rule in policy.RUN_RULES]
    assert [(v.rule, v.listing_id) for v in run_refusals] == [(policy.RULE_REFUSED_SHARE, None)]
    assert "6 of 20 rows refused (30%)" in run_refusals[0].detail
    # one issuer refused twice (revenue and gross profit) counts once: 1 of 4 listings, not 2
    twice = row("listing:twice", "1", "5", revenue="900", revenue_period_end=FY25, gross_profit="900",
                operating_period_end=FY25, references=(reference({FY25: {"revenue": "100", "gross_profit": "100"}}),))  # fmt: skip
    found = evaluate([twice, *_universe(0, 3)])
    assert [v.listing_id for v in found] == ["listing:twice", "listing:twice"]
    assert {v.rule for v in found} == {policy.RULE_INDEPENDENT_REFERENCE}
    # a listing read twice is one listing in the share: 2 of 6 rows (33%), but 1 of 5 listings (20%)
    assert policy.refused_share_of_run([twice, twice, *_universe(0, 4)], {"listing:twice"}) == []


def test_the_judged_listings_show_a_run_no_reference_reached() -> None:
    """A second origin that fails leaves every row without a reference. Nothing is refused, and the
    judged count says 0 instead of letting the run read as checked."""
    judged = row("listing:a", "1", "5", revenue="100", revenue_period_end=FY25,
                 references=(reference({FY25: {"revenue": "100"}}),))  # fmt: skip
    silent = row("listing:b", "1", "5", revenue="100", revenue_period_end=FY25,
                 references=(reference({FY25: {"revenue": None, "gross_profit": None}}),))  # fmt: skip
    bare = row("listing:c", "1", "5", revenue="100", revenue_period_end=FY25)
    assert policy.judged_listings([judged, silent, bare]) == {"listing:a"}
    assert policy.judged_listings([silent, bare]) == set()
    # a bank's gross profit is not compared, so a reference with only gross profit judges nothing
    bank = row("listing:jpm", "1", "5", branch="financial", gross_profit="100", operating_period_end=FY25,
               references=(reference({FY25: {"gross_profit": "100"}}),))  # fmt: skip
    assert policy.judged_listings([bank]) == set()


def test_an_empty_eligible_set_is_caught() -> None:
    """#527: 100 % excluded decisions, or an L2 complete count of zero, recorded SUCCESS."""
    all_excluded = evaluate(PREVIOUS, outcomes={"excluded": 20}, l2_complete=0)
    assert [v.rule for v in all_excluded] == [policy.RULE_EMPTY_ELIGIBLE, policy.RULE_EMPTY_ELIGIBLE]
    assert evaluate(PREVIOUS, outcomes={"selected": 2, "excluded": 18}, l2_complete=17) == []
    # a tick without a strategy passes nothing here and is not judged
    assert evaluate(PREVIOUS) == []
    # #544: outcomes={} (a strategy run that produced 0 decisions) must not be exempted
    assert [v.rule for v in evaluate(PREVIOUS, outcomes={}, l2_complete=5)] == [policy.RULE_EMPTY_ELIGIBLE]
    assert [v.rule for v in policy.empty_eligible_set({}, 5)] == [policy.RULE_EMPTY_ELIGIBLE]


def test_the_first_accepted_run_has_no_previous_to_review_against() -> None:
    current = [row("listing:xnas:nvda", "3272605", "25.71", close="180")]
    assert evaluate(current) == [] and reviews(current, []) == []


def test_no_refusal_reads_the_previous_run() -> None:
    """#1443 acceptance 2: no refusal names "moved Nx versus the previous run" as its only reason.
    The refusal entry points take no previous run at all, and every large move against a previous
    run, explained or not, comes back as a review flag and never as a refusal."""
    for entry in (policy.evaluate, policy.evaluate_issuers, policy.refused_share_of_run):
        assert "previous" not in inspect.signature(entry).parameters, entry.__name__
    moves = [
        row(f"listing:{index}", str(op), str(ps), close=str(close), revenue_period_end=FY25)
        for index, (op, ps, close) in enumerate([(9_000_000, "0.5", 100), (1, "80", 100), (5_000_000, "9", 1)])
    ]
    before = [row(f"listing:{index}", "1000", "5", close="100", revenue_period_end=FY25) for index in range(3)]
    flagged = reviews(moves, before)
    assert {v.rule for v in flagged} == set(policy.REVIEWS) and len(flagged) >= 3
    refusals = evaluate(moves)
    assert all(v.rule in policy.RULES and "versus the previous run" not in v.detail for v in refusals)
    assert refusals == []


def test_the_policy_is_v4_with_named_rules_reasons_and_a_pinned_identity() -> None:
    assert policy.POLICY_VERSION == "v4"
    assert Thresholds() == Thresholds(D("1.5"), D("2"), D("1.4"), D("2"), D("0.25"))
    assert set(policy.RULES) == {
        "independent-reference-disagreement",
        "node-sign-policy",
        "refused-share-of-run",
        "empty-eligible-set",
    }
    assert set(policy.REVIEWS) == {"universe-max-regression", "unexplained-valuation-move"}
    # every issuer rule names the exclusion reason its refused decision carries
    assert set(policy.EXCLUSION_REASON) == set(policy.ISSUER_RULES)
    assert policy.EXCLUSION_REASON[policy.RULE_INDEPENDENT_REFERENCE] is (
        ExclusionReason.PLAUSIBILITY_REFUSED_INDEPENDENT_REFERENCE
    )
    # #1323 criterion 4: a threshold, rule or version edit changes this identity
    assert policy.policy_sha256() == "c7299e5b7854b9bae4afe8516002a4a378f7ce774e8274e081192b2107420d80"
    assert policy.policy_sha256(Thresholds(refused_share_limit=D("0.30"))) != policy.policy_sha256()
    # the gate judges the published column the forest ties to GPPE v0.2.0 (#1176: the uniform node)
    assert published_node(policy.JUDGED_TABLE, policy.JUDGED_COLUMN).key == "gppe_uniform_charge_v0"
