"""The source order of each fact family: one `ReconciliationPolicy` per family.

A policy holds the source order, the tolerance and the quorum of its family. Its `policy_id`
is a content hash. A reader takes the order from the policy and keeps no copy (init.md rule 12).

Readers: the market-price failover (`market_price_adapter.failover_order`), the quality and
confidence reports, the headcount lookup (`headcount.lookup_headcount`), the missing-cell
planner (through `FactPlane.reconciliation_policy`) and `truealpha_tools.output_invariants`.
Each reader takes its policy from this module. An edit here reaches all of them.
"""

from __future__ import annotations

from decimal import Decimal

from truealpha_contracts.reconciliation import (
    CONSENSUS_RELATIVE_TOLERANCE,
    ConflictBehavior,
    ReconciliationPolicy,
)


def _consensus_policy(policy_version: str, source_priority: tuple[str, ...]) -> ReconciliationPolicy:
    """A family policy under the owner's rule: 0.1% relative tolerance and no absolute slack."""
    return ReconciliationPolicy(
        policy_version=policy_version,
        source_priority=source_priority,
        absolute_tolerance=Decimal("0"),
        relative_tolerance=CONSENSUS_RELATIVE_TOLERANCE,
        minimum_independent_origin_groups=2,
        conflict_behavior=ConflictBehavior.MEDIAN_CONSENSUS,
    )


def _priority_only_policy(policy_version: str, source_priority: tuple[str, ...]) -> ReconciliationPolicy:
    """A family policy that only orders sources. Both tolerances are 0. One origin group is enough.

    No value comparison runs for these families. The first source in the order wins.
    """
    return ReconciliationPolicy(
        policy_version=policy_version,
        source_priority=source_priority,
        absolute_tolerance=Decimal("0"),
        relative_tolerance=Decimal("0"),
        minimum_independent_origin_groups=1,
    )


# Market price. The first origin is the primary. Each later origin serves a cell that the
# earlier origins cannot serve (#862).
# v4 (owner decision 2026-09-17): `grade_consensus` at 0.1% relative tolerance. Three agreeing
# origins serve their median. Two serve their mean. Any other case serves the primary's
# value. The order breaks ties and serves the cells where no two origins agree.
RECONCILIATION_POLICY = _consensus_policy(
    "market-price-fusion:v4", ("yahoo-chart:v1", "twelve-data:v1", "moomoo-kline:v1")
)
# Volume has its own policy and the order of the price policy. Since 2026-09-17 volume grades
# at 0.1% like every other family. A volume conflict never changes the grade of the close.
VOLUME_RECONCILIATION_POLICY = _consensus_policy("market-volume-fusion:v2", RECONCILIATION_POLICY.source_priority)
# Each field of a price bar is its own reconciliation cell. Open, high and low share the policy
# of the close.
FIELD_RECONCILIATION_POLICIES: dict[str, ReconciliationPolicy] = {
    "open": RECONCILIATION_POLICY,
    "high": RECONCILIATION_POLICY,
    "low": RECONCILIATION_POLICY,
    "close": RECONCILIATION_POLICY,
    "volume": VOLUME_RECONCILIATION_POLICY,
}

# Financial facts (v2, 0.1%). SEC company-facts is the primary. The moomoo statements are the
# independent second origin. The report compares each field at the fiscal period end of the
# primary. An agreeing pair serves its mean. A median needs a third independent origin.
FINANCIAL_FACT_RECONCILIATION_POLICY = _consensus_policy(
    "financial-fact-fusion:v2", ("sec-company-facts:v1", "moomoo-financials:v1")
)

# Index membership. The index operator list is the primary: the governed universe is published
# from it. The fund N-PORT holdings are the independent second route. Both routes list a
# listing or not, so membership compares exactly. The fund weight is the `etf_weight` family
# under this policy. The tolerance catches a wrong join, which is ten times larger than the
# price drift between a filed weight and an operator weight. The operator route carries no
# weight yet, so `etf_weight` has one origin until it does.
INDEX_MEMBERSHIP_POLICY = ReconciliationPolicy(
    policy_version="index-membership-fusion:v1",
    source_priority=("nasdaq-index:v1", "nport:v1"),
    absolute_tolerance=Decimal("0.05"),
    relative_tolerance=Decimal("0.10"),
    minimum_independent_origin_groups=2,
)

# Headcount (`staging.issuer_headcount_facts`). A cited 10-K extraction outranks the reviewed
# manual seed, whatever the knowable time of each. A source absent from the order ranks last,
# so an unknown producer never outranks a named source by being newer.
HEADCOUNT_RECONCILIATION_POLICY = _priority_only_policy("headcount-priority:v1", ("10k-extraction", "manual-review"))

# Segment revenue (`staging.issuer_segment_revenue_facts`). One source today.
SEGMENT_REVENUE_RECONCILIATION_POLICY = _priority_only_policy(
    "segment-revenue-priority:v1", ("10k-segment-extraction",)
)

#: The policies that the owner's rule replaced. Each keeps the order that it declared, so the
#: policy id in a historical report still resolves. The live policies never change these
#: orders. Nothing grades against these policies again.
SUPERSEDED_RECONCILIATION_POLICIES: tuple[ReconciliationPolicy, ...] = (
    ReconciliationPolicy(
        policy_version="market-price-fusion:v3",
        source_priority=("yahoo-chart:v1", "twelve-data:v1", "moomoo-kline:v1"),
        absolute_tolerance=Decimal("0"),
        relative_tolerance=Decimal("0.003"),
        minimum_independent_origin_groups=2,
    ),
    ReconciliationPolicy(
        policy_version="market-volume-fusion:v1",
        source_priority=("yahoo-chart:v1", "twelve-data:v1", "moomoo-kline:v1"),
        absolute_tolerance=Decimal("0"),
        relative_tolerance=Decimal("0.02"),
        minimum_independent_origin_groups=2,
    ),
    ReconciliationPolicy(
        policy_version="financial-fact-fusion:v1",
        source_priority=("sec-company-facts:v1", "moomoo-financials:v1"),
        absolute_tolerance=Decimal("0"),
        relative_tolerance=Decimal("0.01"),
        minimum_independent_origin_groups=2,
    ),
)
