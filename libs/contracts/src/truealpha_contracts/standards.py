"""Metric standards: the unit the standard→wide-row loop maps onto every issuer (#735, #733).

A `MetricStandard` says what a field MEANS and what counts as its value, on top of the
metric registry's fusion entry (`metrics.METRICS`: unit family, `source_priority`, period
kind). The registry answers "which source wins"; the standard answers "what is the value
at all" — the acceptance rule a candidate must satisfy, the evidence a landed value must
carry, whether the column is a hard (deterministic, corroborated) or an evaluative
(model-judged) factor, and how old a value may be before the planner reopens the cell.

Two kinds, one row (owner decision 2026-09-04):
- `HARD`: a deterministic acceptance rule; may gate strategy selection.
- `EVALUATIVE`: a model judges a rubric; carries `factor_validation_status` and never
  gates selection until a sealed holdout has calibrated it (init.md §8, #734).

The first standard is `employees_total` — the GPPE denominator, prose-only in every
filing (SEC company-facts publishes no employee count), which is why the loop exists.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.metrics import METRICS


class StandardKind(StrEnum):
    HARD = "hard"
    EVALUATIVE = "evaluative"


class EvidenceRequirement(StrEnum):
    """What a landed value must point at before it is a value rather than a guess."""

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
    # A value older than this, measured from the cutoff, reopens the cell. Annual
    # disclosures refresh every ~365 days; 400 tolerates a late filer without treating a
    # two-year-old figure as current.
    max_age_days: int = Field(gt=0)
    # Source labels (as written in the fact table's `source`) that satisfy `evidence`. A
    # cell whose best fact comes from any other source is open: the loop may supersede a
    # reviewed seed with a cited extraction, never the reverse.
    evidence_bearing_sources: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _registered_metric(self) -> MetricStandard:
        if self.metric not in METRICS:
            raise ValueError(f"standard {self.metric!r} names a metric the registry does not declare")
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


#: Confidence a landed value carries, by policy id and extractor identity. The
#: extractor identity is the rule or model revision that CHOSE the value; the number is
#: the policy's statement about that chooser, not the chooser's opinion of itself
#: (init.md §9: self-reported confidence is a signal, not ground truth).
CONFIDENCE_POLICIES: MappingProxyType[str, MappingProxyType[str, Decimal]] = MappingProxyType(
    {
        "headcount-confidence:v1": MappingProxyType(
            {
                # Exactly one company-wide statement in the filing: nothing to choose.
                "rule:single-candidate:v1": Decimal("0.85"),
                # Reserved for the model-selection path (#70 scope item 2).
                "model-selection": Decimal("0.90"),
                # The reviewed seed, as it was recorded (#521).
                "manual-review": Decimal("0.70"),
            }
        )
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
        )
    }
)


def confidence_for(policy_id: str, extractor: str) -> Decimal:
    try:
        return CONFIDENCE_POLICIES[policy_id][extractor]
    except KeyError as error:
        raise KeyError(f"confidence policy {policy_id!r} has no entry for extractor {extractor!r}") from error
