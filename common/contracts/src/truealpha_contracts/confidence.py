"""Continuous, evidence-bound confidence contracts for DataHub."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from truealpha_contracts import common as _c

_U01 = Annotated[_c.SafeDecimal, Field(ge=0, le=1)]


class ContinuousConfidencePolicy(_c.IdentifiedFrozenModel):
    id_field, id_prefix = "policy_id", "confidence-policy"
    policy_id: str = _c.content_id("confidence-policy")
    content_sha256: _c.ContentHash = ""
    reliability_prior_success: _c.SafeDecimal = Field(default=Decimal("8"), ge=0)
    reliability_prior_failure: _c.SafeDecimal = Field(default=Decimal("2"), ge=0)
    unobserved_reliability_ceiling: _c.SafeDecimal = Field(default=Decimal("0.8"), ge=0, le=1)
    agreement_exponent: _c.SafeDecimal = Field(default=Decimal("0.35"), gt=0, le=1)
    semantic_mapping_exponent: _c.SafeDecimal = Field(default=Decimal("0.25"), gt=0, le=1)
    lineage_exponent: _c.SafeDecimal = Field(default=Decimal("0.20"), gt=0, le=1)
    completeness_exponent: _c.SafeDecimal = Field(default=Decimal("0.20"), gt=0, le=1)
    calculation_precision: int = Field(default=50, ge=28, le=100)
    output_decimal_places: int = Field(default=6, ge=2, le=12)

    def _canonicalize(self) -> None:
        if self.reliability_prior_success + self.reliability_prior_failure <= 0:
            raise ValueError("positive total mass required")
        exps = (
            self.agreement_exponent,
            self.semantic_mapping_exponent,
            self.lineage_exponent,
            self.completeness_exponent,
        )
        if sum(exps) != Decimal("1"):
            raise ValueError("exponents must sum exactly to one")


class SourceConfidenceEvidence(_c.IdentifiedFrozenModel):
    id_field, id_prefix = "source_evidence_id", "source-confidence-evidence"
    source_evidence_id: str = _c.content_id("source-confidence-evidence")
    content_sha256: _c.ContentHash = ""
    provider_id: _c.StableId
    origin_group_id: _c.StableId
    independence_weight: _U01
    reliability_prior: _U01 | None = None
    successful_outcome_mass: _c.SafeDecimal = Field(ge=0)
    failed_outcome_mass: _c.SafeDecimal = Field(ge=0)
    freshness: _U01
    sample_conformance: _U01
    transport_integrity: _U01
    evidence_ids: _c.SortedUniqueIds = Field(min_length=1)
    reason_codes: _c.SortedUniqueIds = Field(min_length=1)


class ContinuousConfidenceInput(_c.IdentifiedFrozenModel):
    id_field, id_prefix = "input_id", "confidence-input"
    input_id: str = _c.content_id("confidence-input")
    content_sha256: _c.ContentHash = ""
    case_id: _c.StableId
    sources: tuple[SourceConfidenceEvidence, ...] = Field(min_length=1)
    agreement: _U01
    semantic_mapping_quality: _U01
    lineage_completeness: _U01
    required_component_completeness: _U01

    def _canonicalize(self) -> None:
        sources = tuple(sorted(self.sources, key=lambda v: v.source_evidence_id))
        if len({s.source_evidence_id for s in sources}) != len(sources):
            raise ValueError("source evidence must be unique")
        w: dict[str, Decimal] = {}
        for s in sources:
            if w.setdefault(s.origin_group_id, s.independence_weight) != s.independence_weight:
                raise ValueError("one origin group must use one independence weight")
        object.__setattr__(self, "sources", sources)


class SourceConfidenceScore(_c.FrozenModel):
    source_evidence_id: str = _c.content_ref("source-confidence-evidence")
    provider_id: _c.StableId
    origin_group_id: _c.StableId
    reliability: _U01
    source_quality: _U01


class OriginGroupContribution(_c.FrozenModel):
    origin_group_id: _c.StableId
    independence_weight: _U01
    selected_source_evidence_id: str = _c.content_ref("source-confidence-evidence")
    selected_source_quality: _U01
    effective_support: _U01


class ContinuousConfidenceEvaluation(_c.IdentifiedFrozenModel):
    id_field, id_prefix = "evaluation_id", "confidence-evaluation"
    evaluation_id: str = _c.content_id("confidence-evaluation")
    content_sha256: _c.ContentHash = ""
    policy_id: str = _c.content_ref("confidence-policy")
    policy_sha256: _c.Sha256
    input_id: str = _c.content_ref("confidence-input")
    input_sha256: _c.Sha256
    source_scores: tuple[SourceConfidenceScore, ...] = Field(min_length=1)
    origin_groups: tuple[OriginGroupContribution, ...] = Field(min_length=1)
    evidence_mass: _c.SafeDecimal = Field(ge=0)
    source_support: _U01
    agreement: _U01
    semantic_mapping_quality: _U01
    lineage_completeness: _U01
    required_component_completeness: _U01
    confidence: _U01
    reason_codes: _c.SortedUniqueIds = Field(min_length=1)

    def _canonicalize(self) -> None:
        sc = tuple(sorted(self.source_scores, key=lambda v: v.source_evidence_id))
        gr = tuple(sorted(self.origin_groups, key=lambda v: v.origin_group_id))
        by_id = {s.source_evidence_id: s for s in sc}
        if len(by_id) != len(sc) or len({g.origin_group_id for g in gr}) != len(gr):
            raise ValueError("scores and groups must be unique")
        if any(
            g.selected_source_evidence_id not in by_id
            or by_id[g.selected_source_evidence_id].origin_group_id != g.origin_group_id
            for g in gr
        ):
            raise ValueError("origin group must match scored source group")
        if (
            self.policy_id != f"confidence-policy:{self.policy_sha256}"
            or self.input_id != f"confidence-input:{self.input_sha256}"
        ):
            raise ValueError("policy/input identity and hash mismatch")
        object.__setattr__(self, "source_scores", sc)
        object.__setattr__(self, "origin_groups", gr)
