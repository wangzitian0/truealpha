"""Metric standards loaded from registries.v1.json."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field, model_validator
from truealpha_contracts.common import CanonicalHash, FrozenModel, load_data_json
from truealpha_contracts.metrics import METRICS
from truealpha_runtime.reconciliation import ReconciliationPolicy
from truealpha_runtime.reconciliation_policies import (
    HEADCOUNT_RECONCILIATION_POLICY,
    SEGMENT_REVENUE_RECONCILIATION_POLICY,
)


class StandardKind(StrEnum):
    HARD = "hard"
    EVALUATIVE = "evaluative"


class EvidenceRequirement(StrEnum):
    XBRL_FACT = "xbrl_fact"
    FILING_SPAN = "filing_span"
    DOCUMENT_CITATION = "document_citation"
    RUBRIC_JUDGEMENT = "rubric_judgement"


class FactPlane(FrozenModel):
    table: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$")
    issuer_column: str = Field(pattern=r"^[a-z_]+$")
    reconciliation_policy: ReconciliationPolicy
    withdrawn_extractors: tuple[str, ...] = ()


class MetricStandard(CanonicalHash, FrozenModel):
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


_REG = load_data_json("registries.v1.json")
_POLICIES = {"headcount": HEADCOUNT_RECONCILIATION_POLICY, "segment_revenue": SEGMENT_REVENUE_RECONCILIATION_POLICY}
CONFIDENCE_POLICIES: MappingProxyType[str, MappingProxyType[str, Decimal]] = MappingProxyType(
    {k: MappingProxyType({ek: Decimal(ev) for ek, ev in v.items()}) for k, v in _REG["confidence_policies"].items()}
)
STANDARDS: MappingProxyType[str, MetricStandard] = MappingProxyType(
    {
        k: MetricStandard(
            **{k2: v2 for k2, v2 in row.items() if k2 != "plane"},
            plane=FactPlane(
                table=row["plane"]["table"],
                issuer_column=row["plane"]["issuer_column"],
                reconciliation_policy=_POLICIES[row["plane"]["policy"]],
                withdrawn_extractors=tuple(row["plane"]["withdrawn_extractors"]),
            ),
        )
        for k, row in _REG["standards"].items()
    }
)


def confidence_for(policy_id: str, extractor: str) -> Decimal:
    key = "model-selection" if extractor.startswith("model:") else extractor
    try:
        return CONFIDENCE_POLICIES[policy_id][key]
    except KeyError as error:
        raise KeyError(f"confidence policy {policy_id!r} has no entry for extractor {extractor!r}") from error
