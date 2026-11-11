"""Continuous, evidence-bound confidence contracts for DataHub."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import (
    SHA256_PATTERN,
    STABLE_ID_PATTERN,
    FrozenModel,
    finite_decimal_input,
    identify,
    sorted_unique_coordinates,
)


class ContinuousConfidencePolicy(FrozenModel):
    policy_id: str = Field(default="", pattern=r"^(?:|confidence-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    reliability_prior_success: Decimal = Field(default=Decimal("8"), ge=0)
    reliability_prior_failure: Decimal = Field(default=Decimal("2"), ge=0)
    unobserved_reliability_ceiling: Decimal = Field(default=Decimal("0.8"), ge=0, le=1)
    agreement_exponent: Decimal = Field(default=Decimal("0.35"), gt=0, le=1)
    semantic_mapping_exponent: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    lineage_exponent: Decimal = Field(default=Decimal("0.20"), gt=0, le=1)
    completeness_exponent: Decimal = Field(default=Decimal("0.20"), gt=0, le=1)
    calculation_precision: int = Field(default=50, ge=28, le=100)
    output_decimal_places: int = Field(default=6, ge=2, le=12)

    @field_validator(
        "reliability_prior_success",
        "reliability_prior_failure",
        "unobserved_reliability_ceiling",
        "agreement_exponent",
        "semantic_mapping_exponent",
        "lineage_exponent",
        "completeness_exponent",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return finite_decimal_input(value)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
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
        identify(self, id_field="policy_id", prefix="confidence-policy")
        return self


class SourceConfidenceEvidence(FrozenModel):
    source_evidence_id: str = Field(default="", pattern=r"^(?:|source-confidence-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    provider_id: str = Field(pattern=STABLE_ID_PATTERN)
    origin_group_id: str = Field(pattern=STABLE_ID_PATTERN)
    independence_weight: Decimal = Field(ge=0, le=1)
    reliability_prior: Decimal | None = Field(default=None, ge=0, le=1)
    successful_outcome_mass: Decimal = Field(ge=0)
    failed_outcome_mass: Decimal = Field(ge=0)
    freshness: Decimal = Field(ge=0, le=1)
    sample_conformance: Decimal = Field(ge=0, le=1)
    transport_integrity: Decimal = Field(ge=0, le=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "independence_weight",
        "reliability_prior",
        "successful_outcome_mass",
        "failed_outcome_mass",
        "freshness",
        "sample_conformance",
        "transport_integrity",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return finite_decimal_input(value)

    @field_validator("evidence_ids", "reason_codes")
    @classmethod
    def validate_coordinates(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return sorted_unique_coordinates(values, info.field_name, allow_empty=False)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        identify(self, id_field="source_evidence_id", prefix="source-confidence-evidence")
        return self


class ContinuousConfidenceInput(FrozenModel):
    input_id: str = Field(default="", pattern=r"^(?:|confidence-input:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    case_id: str = Field(pattern=STABLE_ID_PATTERN)
    sources: tuple[SourceConfidenceEvidence, ...] = Field(min_length=1)
    agreement: Decimal = Field(ge=0, le=1)
    semantic_mapping_quality: Decimal = Field(ge=0, le=1)
    lineage_completeness: Decimal = Field(ge=0, le=1)
    required_component_completeness: Decimal = Field(ge=0, le=1)

    @field_validator(
        "agreement",
        "semantic_mapping_quality",
        "lineage_completeness",
        "required_component_completeness",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return finite_decimal_input(value)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        sources = tuple(sorted(self.sources, key=lambda v: v.source_evidence_id))
        if len({s.source_evidence_id for s in sources}) != len(sources):
            raise ValueError("confidence input source evidence must be unique")
        group_weights: dict[str, Decimal] = {}
        for s in sources:
            if group_weights.setdefault(s.origin_group_id, s.independence_weight) != s.independence_weight:
                raise ValueError("one origin group must use one independence weight")
        object.__setattr__(self, "sources", sources)
        identify(self, id_field="input_id", prefix="confidence-input")
        return self


class SourceConfidenceScore(FrozenModel):
    source_evidence_id: str = Field(pattern=r"^source-confidence-evidence:[0-9a-f]{64}$")
    provider_id: str = Field(pattern=STABLE_ID_PATTERN)
    origin_group_id: str = Field(pattern=STABLE_ID_PATTERN)
    reliability: Decimal = Field(ge=0, le=1)
    source_quality: Decimal = Field(ge=0, le=1)

    @field_validator("reliability", "source_quality", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return finite_decimal_input(value)


class OriginGroupContribution(FrozenModel):
    origin_group_id: str = Field(pattern=STABLE_ID_PATTERN)
    independence_weight: Decimal = Field(ge=0, le=1)
    selected_source_evidence_id: str = Field(pattern=r"^source-confidence-evidence:[0-9a-f]{64}$")
    selected_source_quality: Decimal = Field(ge=0, le=1)
    effective_support: Decimal = Field(ge=0, le=1)

    @field_validator("independence_weight", "selected_source_quality", "effective_support", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return finite_decimal_input(value)


class ContinuousConfidenceEvaluation(FrozenModel):
    evaluation_id: str = Field(default="", pattern=r"^(?:|confidence-evaluation:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    policy_id: str = Field(pattern=r"^confidence-policy:[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=SHA256_PATTERN)
    input_id: str = Field(pattern=r"^confidence-input:[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    source_scores: tuple[SourceConfidenceScore, ...] = Field(min_length=1)
    origin_groups: tuple[OriginGroupContribution, ...] = Field(min_length=1)
    evidence_mass: Decimal = Field(ge=0)
    source_support: Decimal = Field(ge=0, le=1)
    agreement: Decimal = Field(ge=0, le=1)
    semantic_mapping_quality: Decimal = Field(ge=0, le=1)
    lineage_completeness: Decimal = Field(ge=0, le=1)
    required_component_completeness: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "evidence_mass",
        "source_support",
        "agreement",
        "semantic_mapping_quality",
        "lineage_completeness",
        "required_component_completeness",
        "confidence",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return finite_decimal_input(value)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return sorted_unique_coordinates(values, "reason_codes", allow_empty=False)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
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
        identify(self, id_field="evaluation_id", prefix="confidence-evaluation")
        return self


class ConfidenceCalibrationScenario(FrozenModel):
    scenario_id: str = Field(pattern=STABLE_ID_PATTERN)
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


class ConfidenceCalibrationReport(FrozenModel):
    report_id: str = Field(default="", pattern=r"^(?:|confidence-calibration-report:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schema_version: Literal["truealpha.confidence-calibration@v1"] = "truealpha.confidence-calibration@v1"
    policy: ContinuousConfidencePolicy
    denominator_id: str = Field(pattern=STABLE_ID_PATTERN)
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

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        scenarios = tuple(sorted(self.scenarios, key=lambda v: v.scenario_id))
        if len({s.scenario_id for s in scenarios}) != len(scenarios):
            raise ValueError("calibration scenario IDs must be unique")
        if len(self.empirically_observed_subject_ids) > self.denominator_size:
            raise ValueError("empirical subjects cannot exceed the frozen denominator")
        if any(s.evaluation.policy_id != self.policy.policy_id for s in scenarios):
            raise ValueError("every calibration scenario must use the report policy")
        object.__setattr__(self, "scenarios", scenarios)
        identify(self, id_field="report_id", prefix="confidence-calibration-report")
        return self
