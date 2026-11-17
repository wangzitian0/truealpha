"""One content-addressed `ReconciliationPolicy` per fact family (init.md rule 12)."""

from __future__ import annotations

from decimal import Decimal

from truealpha_runtime.reconciliation import CONSENSUS_RELATIVE_TOLERANCE, ConflictBehavior, ReconciliationPolicy

_MKT_SOURCES = ("yahoo-chart:v1", "twelve-data:v1", "moomoo-kline:v1")


def _pol(
    version: str,
    sources: tuple[str, ...],
    *,
    min_groups: int = 2,
    rel: Decimal = CONSENSUS_RELATIVE_TOLERANCE,
    abs_tol: Decimal = Decimal("0"),
    behavior: ConflictBehavior = ConflictBehavior.MEDIAN_CONSENSUS,
) -> ReconciliationPolicy:
    return ReconciliationPolicy(
        policy_version=version,
        source_priority=sources,
        absolute_tolerance=abs_tol,
        relative_tolerance=rel,
        minimum_independent_origin_groups=min_groups,
        conflict_behavior=behavior,
    )


RECONCILIATION_POLICY = _pol("market-price-fusion:v4", _MKT_SOURCES)
VOLUME_RECONCILIATION_POLICY = _pol("market-volume-fusion:v2", _MKT_SOURCES)
FIELD_RECONCILIATION_POLICIES: dict[str, ReconciliationPolicy] = {
    k: RECONCILIATION_POLICY for k in ("open", "high", "low", "close")
} | {"volume": VOLUME_RECONCILIATION_POLICY}
FINANCIAL_FACT_RECONCILIATION_POLICY = _pol(
    "financial-fact-fusion:v2", ("sec-company-facts:v1", "moomoo-financials:v1")
)
INDEX_MEMBERSHIP_POLICY = _pol(
    "index-membership-fusion:v1",
    ("nasdaq-index:v1", "nport:v1"),
    rel=Decimal("0.10"),
    abs_tol=Decimal("0.05"),
    behavior=ConflictBehavior.REPORT_AND_ABSTAIN,
)
HEADCOUNT_RECONCILIATION_POLICY = _pol(
    "headcount-priority:v1",
    ("10k-extraction", "manual-review"),
    min_groups=1,
    rel=Decimal("0"),
    behavior=ConflictBehavior.REPORT_AND_ABSTAIN,
)
SEGMENT_REVENUE_RECONCILIATION_POLICY = _pol(
    "segment-revenue-priority:v1",
    ("10k-segment-extraction",),
    min_groups=1,
    rel=Decimal("0"),
    behavior=ConflictBehavior.REPORT_AND_ABSTAIN,
)
SUPERSEDED_RECONCILIATION_POLICIES: tuple[ReconciliationPolicy, ...] = (
    _pol("market-price-fusion:v3", _MKT_SOURCES, rel=Decimal("0.003"), behavior=ConflictBehavior.REPORT_AND_ABSTAIN),
    _pol("market-volume-fusion:v1", _MKT_SOURCES, rel=Decimal("0.02"), behavior=ConflictBehavior.REPORT_AND_ABSTAIN),
    _pol(
        "financial-fact-fusion:v1",
        ("sec-company-facts:v1", "moomoo-financials:v1"),
        rel=Decimal("0.01"),
        behavior=ConflictBehavior.REPORT_AND_ABSTAIN,
    ),
)
