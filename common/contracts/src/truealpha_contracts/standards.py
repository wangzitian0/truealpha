"""Metric standards: what a field means, what counts as its value, and where its facts live (#735, #733)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.metrics import METRICS
from truealpha_contracts.reconciliation import ReconciliationPolicy
from truealpha_contracts.reconciliation_policies import (
    HEADCOUNT_RECONCILIATION_POLICY,
    SEGMENT_REVENUE_RECONCILIATION_POLICY,
)


class StandardKind(StrEnum):
    HARD = "hard"
    EVALUATIVE = "evaluative"


class FactPlane(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$")
    issuer_column: str = Field(pattern=r"^[a-z_]+$")
    reconciliation_policy: ReconciliationPolicy
    withdrawn_extractors: tuple[str, ...] = ()


class EvidenceRequirement(StrEnum):
    XBRL_FACT = "xbrl_fact"
    FILING_SPAN = "filing_span"
    DOCUMENT_CITATION = "document_citation"
    RUBRIC_JUDGEMENT = "rubric_judgement"


class MetricStandard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    acceptance_rule: str = Field(min_length=1)
    kind: StandardKind
    evidence: EvidenceRequirement
    confidence_policy_id: str = Field(pattern=r"^[a-z0-9-]+:v[0-9]+$")
    max_age_days: int = Field(gt=0)
    evidence_bearing_sources: tuple[str, ...] = Field(min_length=1)
    plane: FactPlane
    adapter: str = Field(pattern=r"^[a-z0-9_.]+:[a-z0-9_]+$")

    @model_validator(mode="after")
    def _registered_metric(self) -> MetricStandard:
        if self.metric not in METRICS:
            raise ValueError(f"standard {self.metric!r} names a metric the registry does not declare")
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


CONFIDENCE_POLICIES: MappingProxyType[str, MappingProxyType[str, Decimal]] = MappingProxyType(
    {
        "segment-revenue-confidence:v1": MappingProxyType(
            {
                "rule:exhaustive-partition:v1": Decimal("0.90"),
                "rule:single-segment:v4": Decimal("0.75"),
                "model-selection": Decimal("0.85"),
            }
        ),
        "headcount-confidence:v1": MappingProxyType(
            {
                "rule:single-candidate:v1": Decimal("0.85"),
                "model-selection": Decimal("0.90"),
                "manual-review": Decimal("0.70"),
            }
        ),
    }
)

STANDARDS: MappingProxyType[str, MetricStandard] = MappingProxyType(
    {
        "employees_total": MetricStandard(
            metric="employees_total",
            definition=(
                "Company-wide total employees as stated in the issuer's latest annual filing "
                "(10-K or 20-F), as of the date the filing states."
            ),
            acceptance_rule=(
                "One company-wide total. Departmental, segment, geographic, part-time, "
                "temporary or contractor counts are not the value; a filing that states "
                "several distinct company-wide totals needs selection, not a guess."
            ),
            kind=StandardKind.HARD,
            evidence=EvidenceRequirement.FILING_SPAN,
            confidence_policy_id="headcount-confidence:v1",
            max_age_days=400,
            evidence_bearing_sources=("10k-extraction",),
            plane=FactPlane(
                table="staging.issuer_headcount_facts",
                issuer_column="cik",
                reconciliation_policy=HEADCOUNT_RECONCILIATION_POLICY,
            ),
            adapter="data_engine.datahub.standards.filing_extraction:extract_headcount",
        ),
        "segment_revenue": MetricStandard(
            metric="segment_revenue",
            definition=(
                "Revenue attributed to one reportable segment, as the issuer's latest annual "
                "filing states it in its own segment table."
            ),
            acceptance_rule=(
                "The parts, not a part. A segment set is the value only when it accounts for "
                "the issuer's consolidated revenue: a missed segment silently raises every "
                "remaining segment's share, so a set that does not balance is refused rather "
                "than ranked. Intersegment revenue is excluded, not summed."
            ),
            kind=StandardKind.HARD,
            evidence=EvidenceRequirement.FILING_SPAN,
            confidence_policy_id="segment-revenue-confidence:v1",
            max_age_days=400,
            evidence_bearing_sources=("10k-segment-extraction",),
            plane=FactPlane(
                table="staging.issuer_segment_revenue_facts",
                issuer_column="cik",
                reconciliation_policy=SEGMENT_REVENUE_RECONCILIATION_POLICY,
                withdrawn_extractors=("rule:single-segment:v1", "rule:single-segment:v2", "rule:single-segment:v3"),
            ),
            adapter="data_engine.datahub.standards.segment_extraction:extract_segment_revenue",
        ),
    }
)


def confidence_for(policy_id: str, extractor: str) -> Decimal:
    key = "model-selection" if extractor.startswith("model:") else extractor
    try:
        return CONFIDENCE_POLICIES[policy_id][key]
    except KeyError as error:
        raise KeyError(f"confidence policy {policy_id!r} has no entry for extractor {extractor!r}") from error
