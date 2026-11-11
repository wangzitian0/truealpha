"""One content-addressed `ReconciliationPolicy` per fact family (init.md rule 12)."""

from __future__ import annotations

from decimal import Decimal

from truealpha_contracts.reconciliation import (
    CONSENSUS_RELATIVE_TOLERANCE,
    ConflictBehavior,
    ReconciliationPolicy,
)


def _consensus_policy(policy_version: str, source_priority: tuple[str, ...]) -> ReconciliationPolicy:
    return ReconciliationPolicy(
        policy_version=policy_version,
        source_priority=source_priority,
        absolute_tolerance=Decimal("0"),
        relative_tolerance=CONSENSUS_RELATIVE_TOLERANCE,
        minimum_independent_origin_groups=2,
        conflict_behavior=ConflictBehavior.MEDIAN_CONSENSUS,
    )


def _priority_only_policy(policy_version: str, source_priority: tuple[str, ...]) -> ReconciliationPolicy:
    return ReconciliationPolicy(
        policy_version=policy_version,
        source_priority=source_priority,
        absolute_tolerance=Decimal("0"),
        relative_tolerance=Decimal("0"),
        minimum_independent_origin_groups=1,
    )


RECONCILIATION_POLICY = _consensus_policy(
    "market-price-fusion:v4", ("yahoo-chart:v1", "twelve-data:v1", "moomoo-kline:v1")
)
VOLUME_RECONCILIATION_POLICY = _consensus_policy("market-volume-fusion:v2", RECONCILIATION_POLICY.source_priority)
FIELD_RECONCILIATION_POLICIES: dict[str, ReconciliationPolicy] = {
    "open": RECONCILIATION_POLICY,
    "high": RECONCILIATION_POLICY,
    "low": RECONCILIATION_POLICY,
    "close": RECONCILIATION_POLICY,
    "volume": VOLUME_RECONCILIATION_POLICY,
}

FINANCIAL_FACT_RECONCILIATION_POLICY = _consensus_policy(
    "financial-fact-fusion:v2", ("sec-company-facts:v1", "moomoo-financials:v1")
)
INDEX_MEMBERSHIP_POLICY = ReconciliationPolicy(
    policy_version="index-membership-fusion:v1",
    source_priority=("nasdaq-index:v1", "nport:v1"),
    absolute_tolerance=Decimal("0.05"),
    relative_tolerance=Decimal("0.10"),
    minimum_independent_origin_groups=2,
)
HEADCOUNT_RECONCILIATION_POLICY = _priority_only_policy("headcount-priority:v1", ("10k-extraction", "manual-review"))
SEGMENT_REVENUE_RECONCILIATION_POLICY = _priority_only_policy(
    "segment-revenue-priority:v1", ("10k-segment-extraction",)
)

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
