"""Continuous, evidence-bound confidence contracts for DataHub."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import (
    ContentHash,
    FrozenModel,
    IdentifiedFrozenModel,
    SafeDecimal,
    Sha256,
    SortedUniqueIds,
    StableId,
    content_id,
    content_ref,
    sorted_unique_coordinates,
)


class ContinuousConfidencePolicy(IdentifiedFrozenModel):
    id_field = "policy_id"
    id_prefix = "confidence-policy"
    policy_id: str = content_id("confidence-policy")
    content_sha256: ContentHash = ""
    reliability_prior_success: SafeDecimal = Field(default=Decimal("8"), ge=0)
    reliability_prior_failure: SafeDecimal = Field(default=Decimal("2"), ge=0)
    unobserved_reliability_ceiling: SafeDecimal = Field(default=Decimal("0.8"), ge=0, le=1)
    agreement_exponent: SafeDecimal = Field(default=Decimal("0.35"), gt=0, le=1)
    semantic_mapping_exponent: SafeDecimal = Field(default=Decimal("0.25"), gt=0, le=1)
    lineage_exponent: SafeDecimal = Field(default=Decimal("0.20"), gt=0, le=1)
    completeness_exponent: SafeDecimal = Field(default=Decimal("0.20"), gt=0, le=1)
    calculation_precision: int = Field(default=50, ge=28, le=100)
    output_decimal_places: int = Field(default=6, ge=2, le=12)

    def _canonicalize(self) -> None:
        if self.reliability_prior_success + self.reliability_prior_failure <= 0:
            raise ValueError("reliability priors must have positive total mass")
        exponents = (
            self.agreement_exponent
            + self.semantic_mapping_exponent
            + self.lineage_exponent
            + self.completeness_exponent
        )
        if exponents != Decimal("1"):
            raise ValueError("quality exponents must sum exactly to one")


class SourceConfidenceEvidence(IdentifiedFrozenModel):
    id_field = "source_evidence_id"
    id_prefix = "source-confidence-evidence"
    source_evidence_id: str = content_id("source-confidence-evidence")
    content_sha256: ContentHash = ""
    provider_id: StableId
    origin_group_id: StableId
    independence_weight: SafeDecimal = Field(ge=0, le=1)
    reliability_prior: SafeDecimal | None = Field(default=None, ge=0, le=1)
    successful_outcome_mass: SafeDecimal = Field(ge=0)
    failed_outcome_mass: SafeDecimal = Field(ge=0)
    freshness: SafeDecimal = Field(ge=0, le=1)
    sample_conformance: SafeDecimal = Field(ge=0, le=1)
    transport_integrity: SafeDecimal = Field(ge=0, le=1)
    evidence_ids: SortedUniqueIds = Field(min_length=1)
    reason_codes: SortedUniqueIds = Field(min_length=1)


class ContinuousConfidenceInput(IdentifiedFrozenModel):
    id_field = "input_id"
    id_prefix = "confidence-input"
    input_id: str = content_id("confidence-input")
    content_sha256: ContentHash = ""
    case_id: StableId
    sources: tuple[SourceConfidenceEvidence, ...] = Field(min_length=1)
    agreement: SafeDecimal = Field(ge=0, le=1)
    semantic_mapping_quality: SafeDecimal = Field(ge=0, le=1)
    lineage_completeness: SafeDecimal = Field(ge=0, le=1)
    required_component_completeness: SafeDecimal = Field(ge=0, le=1)

    def _canonicalize(self) -> None:
        sources = tuple(sorted(self.sources, key=lambda v: v.source_evidence_id))
        if len({s.source_evidence_id for s in sources}) != len(sources):
            raise ValueError("confidence input source evidence must be unique")
        group_weights: dict[str, Decimal] = {}
        for s in sources:
            if group_weights.setdefault(s.origin_group_id, s.independence_weight) != s.independence_weight:
                raise ValueError("one origin group must use one independence weight")
        object.__setattr__(self, "sources", sources)


class SourceConfidenceScore(FrozenModel):
    source_evidence_id: str = content_ref("source-confidence-evidence")
    provider_id: StableId
    origin_group_id: StableId
    reliability: SafeDecimal = Field(ge=0, le=1)
    source_quality: SafeDecimal = Field(ge=0, le=1)


class OriginGroupContribution(FrozenModel):
    origin_group_id: StableId
    independence_weight: SafeDecimal = Field(ge=0, le=1)
    selected_source_evidence_id: str = content_ref("source-confidence-evidence")
    selected_source_quality: SafeDecimal = Field(ge=0, le=1)
    effective_support: SafeDecimal = Field(ge=0, le=1)


class ContinuousConfidenceEvaluation(IdentifiedFrozenModel):
    id_field = "evaluation_id"
    id_prefix = "confidence-evaluation"
    evaluation_id: str = content_id("confidence-evaluation")
    content_sha256: ContentHash = ""
    policy_id: str = content_ref("confidence-policy")
    policy_sha256: Sha256
    input_id: str = content_ref("confidence-input")
    input_sha256: Sha256
    source_scores: tuple[SourceConfidenceScore, ...] = Field(min_length=1)
    origin_groups: tuple[OriginGroupContribution, ...] = Field(min_length=1)
    evidence_mass: SafeDecimal = Field(ge=0)
    source_support: SafeDecimal = Field(ge=0, le=1)
    agreement: SafeDecimal = Field(ge=0, le=1)
    semantic_mapping_quality: SafeDecimal = Field(ge=0, le=1)
    lineage_completeness: SafeDecimal = Field(ge=0, le=1)
    required_component_completeness: SafeDecimal = Field(ge=0, le=1)
    confidence: SafeDecimal = Field(ge=0, le=1)
    reason_codes: SortedUniqueIds = Field(min_length=1)

    def _canonicalize(self) -> None:
        source_scores = tuple(sorted(self.source_scores, key=lambda v: v.source_evidence_id))
        origin_groups = tuple(sorted(self.origin_groups, key=lambda v: v.origin_group_id))
        if len({s.source_evidence_id for s in source_scores}) != len(source_scores):
            raise ValueError("evaluation source scores must be unique")
        if len({g.origin_group_id for g in origin_groups}) != len(origin_groups):
            raise ValueError("evaluation origin groups must be unique")
        scores_by_id = {s.source_evidence_id: s for s in source_scores}
        if any(
            g.selected_source_evidence_id not in scores_by_id
            or scores_by_id[g.selected_source_evidence_id].origin_group_id != g.origin_group_id
            for g in origin_groups
        ):
            raise ValueError("origin groups must select a scored source from the same group")
        if self.policy_id != f"confidence-policy:{self.policy_sha256}":
            raise ValueError("policy identity and hash must agree")
        if self.input_id != f"confidence-input:{self.input_sha256}":
            raise ValueError("input identity and hash must agree")
        object.__setattr__(self, "source_scores", source_scores)
        object.__setattr__(self, "origin_groups", origin_groups)


class ConfidenceCalibrationScenario(FrozenModel):
    scenario_id: StableId
    evidence_class: Literal["sensitivity", "empirical_anchor"]
    expected_effect: str = Field(min_length=1)
    input: ContinuousConfidenceInput
    evaluation: ContinuousConfidenceEvaluation

    @model_validator(mode="after")
    def verify_evaluation(self) -> Self:
        if self.scenario_id != self.input.case_id:
            raise ValueError("scenario ID must match the confidence input case ID")
        if self.evaluation.input_id != self.input.input_id:
            raise ValueError("scenario evaluation must bind the exact confidence input")
        return self


class ConfidenceCalibrationReport(IdentifiedFrozenModel):
    id_field = "report_id"
    id_prefix = "confidence-calibration-report"
    report_id: str = content_id("confidence-calibration-report")
    content_sha256: ContentHash = ""
    schema_version: Literal["truealpha.confidence-calibration@v1"] = "truealpha.confidence-calibration@v1"
    policy: ContinuousConfidencePolicy
    denominator_id: StableId
    denominator_size: int = Field(gt=0)
    empirically_observed_subject_ids: tuple[str, ...]
    scenarios: tuple[ConfidenceCalibrationScenario, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = Field(min_length=1)
    claim_ceiling: Literal["development_sensitivity_only"] = "development_sensitivity_only"

    @field_validator("empirically_observed_subject_ids")
    @classmethod
    def validate_subjects(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return sorted_unique_coordinates(values, "empirically_observed_subject_ids", allow_empty=True)

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("limitations must not contain duplicates")
        return tuple(sorted(values))

    def _canonicalize(self) -> None:
        scenarios = tuple(sorted(self.scenarios, key=lambda v: v.scenario_id))
        if len({s.scenario_id for s in scenarios}) != len(scenarios):
            raise ValueError("calibration scenario IDs must be unique")
        if len(self.empirically_observed_subject_ids) > self.denominator_size:
            raise ValueError("empirical subjects cannot exceed the frozen denominator")
        if any(s.evaluation.policy_id != self.policy.policy_id for s in scenarios):
            raise ValueError("every calibration scenario must use the report policy")
        object.__setattr__(self, "scenarios", scenarios)
