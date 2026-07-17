"""Continuous, evidence-bound confidence contracts for DataHub."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256

_SHA256 = r"^[0-9a-f]{64}$"
_STABLE_COORDINATE = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$"


def _reject_binary_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    if value is not None:
        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
        if not decimal_value.is_finite():
            raise ValueError("non-finite Decimal values are forbidden")
    return value


def _sorted_unique(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    if any(re.fullmatch(_STABLE_COORDINATE, value) is None for value in values):
        raise ValueError(f"{field_name} must contain stable coordinates")
    return tuple(sorted(values))


def _freeze_content_addressed(model: BaseModel, *, id_field: str, prefix: str) -> None:
    content = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    content_sha256 = canonical_sha256(content)
    expected_id = f"{prefix}:{content_sha256}"
    supplied_id = getattr(model, id_field)
    supplied_sha256 = getattr(model, "content_sha256")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match the canonical content")
    if supplied_sha256 and supplied_sha256 != content_sha256:
        raise ValueError("content_sha256 does not match the canonical content")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", content_sha256)


class ContinuousConfidencePolicy(BaseModel):
    """Content-addressed parameters for the continuous confidence formula."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        return _reject_binary_float(value)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.reliability_prior_success + self.reliability_prior_failure <= 0:
            raise ValueError("reliability priors must have positive total mass")
        exponent_total = (
            self.agreement_exponent
            + self.semantic_mapping_exponent
            + self.lineage_exponent
            + self.completeness_exponent
        )
        if exponent_total != Decimal("1"):
            raise ValueError("quality exponents must sum exactly to one")
        _freeze_content_addressed(self, id_field="policy_id", prefix="confidence-policy")
        return self


class SourceConfidenceEvidence(BaseModel):
    """One provider's evidence without claiming provider independence by name alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_evidence_id: str = Field(
        default="",
        pattern=r"^(?:|source-confidence-evidence:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    provider_id: str = Field(pattern=_STABLE_COORDINATE)
    origin_group_id: str = Field(pattern=_STABLE_COORDINATE)
    independence_weight: Decimal = Field(ge=0, le=1)
    successful_outcome_mass: Decimal = Field(ge=0)
    failed_outcome_mass: Decimal = Field(ge=0)
    freshness: Decimal = Field(ge=0, le=1)
    sample_conformance: Decimal = Field(ge=0, le=1)
    transport_integrity: Decimal = Field(ge=0, le=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "independence_weight",
        "successful_outcome_mass",
        "failed_outcome_mass",
        "freshness",
        "sample_conformance",
        "transport_integrity",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("evidence_ids", "reason_codes")
    @classmethod
    def validate_coordinates(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _sorted_unique(values, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_content_addressed(
            self,
            id_field="source_evidence_id",
            prefix="source-confidence-evidence",
        )
        return self


class ContinuousConfidenceInput(BaseModel):
    """All evidence dimensions required for one deterministic evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str = Field(default="", pattern=r"^(?:|confidence-input:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    case_id: str = Field(pattern=_STABLE_COORDINATE)
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
        return _reject_binary_float(value)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        sources = tuple(sorted(self.sources, key=lambda value: value.source_evidence_id))
        source_ids = tuple(source.source_evidence_id for source in sources)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("confidence input source evidence must be unique")
        group_weights: dict[str, Decimal] = {}
        for source in sources:
            existing = group_weights.setdefault(source.origin_group_id, source.independence_weight)
            if existing != source.independence_weight:
                raise ValueError("one origin group must use one independence weight")
        object.__setattr__(self, "sources", sources)
        _freeze_content_addressed(self, id_field="input_id", prefix="confidence-input")
        return self


class SourceConfidenceScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_evidence_id: str = Field(pattern=r"^source-confidence-evidence:[0-9a-f]{64}$")
    provider_id: str = Field(pattern=_STABLE_COORDINATE)
    origin_group_id: str = Field(pattern=_STABLE_COORDINATE)
    reliability: Decimal = Field(ge=0, le=1)
    source_quality: Decimal = Field(ge=0, le=1)

    @field_validator("reliability", "source_quality", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return _reject_binary_float(value)


class OriginGroupContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    origin_group_id: str = Field(pattern=_STABLE_COORDINATE)
    independence_weight: Decimal = Field(ge=0, le=1)
    selected_source_evidence_id: str = Field(pattern=r"^source-confidence-evidence:[0-9a-f]{64}$")
    selected_source_quality: Decimal = Field(ge=0, le=1)
    effective_support: Decimal = Field(ge=0, le=1)

    @field_validator(
        "independence_weight",
        "selected_source_quality",
        "effective_support",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return _reject_binary_float(value)


class ContinuousConfidenceEvaluation(BaseModel):
    """Auditable decomposition of a continuous confidence score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str = Field(default="", pattern=r"^(?:|confidence-evaluation:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    policy_id: str = Field(pattern=r"^confidence-policy:[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=_SHA256)
    input_id: str = Field(pattern=r"^confidence-input:[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=_SHA256)
    source_scores: tuple[SourceConfidenceScore, ...] = Field(min_length=1)
    origin_groups: tuple[OriginGroupContribution, ...] = Field(min_length=1)
    evidence_mass: Decimal = Field(ge=0)
    source_support: Decimal = Field(ge=0, le=1)
    agreement: Decimal = Field(ge=0, le=1)
    semantic_mapping_quality: Decimal = Field(ge=0, le=1)
    lineage_completeness: Decimal = Field(ge=0, le=1)
    required_component_completeness: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    score_100: Decimal = Field(ge=0, le=100)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "evidence_mass",
        "source_support",
        "agreement",
        "semantic_mapping_quality",
        "lineage_completeness",
        "required_component_completeness",
        "confidence",
        "score_100",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(values, "reason_codes")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        source_scores = tuple(sorted(self.source_scores, key=lambda value: value.source_evidence_id))
        origin_groups = tuple(sorted(self.origin_groups, key=lambda value: value.origin_group_id))
        source_ids = tuple(source.source_evidence_id for source in source_scores)
        group_ids = tuple(group.origin_group_id for group in origin_groups)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("evaluation source scores must be unique")
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("evaluation origin groups must be unique")
        scores_by_id = {source.source_evidence_id: source for source in source_scores}
        if any(
            group.selected_source_evidence_id not in scores_by_id
            or scores_by_id[group.selected_source_evidence_id].origin_group_id != group.origin_group_id
            for group in origin_groups
        ):
            raise ValueError("origin groups must select a scored source from the same group")
        if self.policy_id != f"confidence-policy:{self.policy_sha256}":
            raise ValueError("policy identity and hash must agree")
        if self.input_id != f"confidence-input:{self.input_sha256}":
            raise ValueError("input identity and hash must agree")
        with localcontext(_evaluation_context(100)):
            if self.score_100 != self.confidence * Decimal(100):
                raise ValueError("score_100 must be the exact normalized confidence projection")
        object.__setattr__(self, "source_scores", source_scores)
        object.__setattr__(self, "origin_groups", origin_groups)
        _freeze_content_addressed(
            self,
            id_field="evaluation_id",
            prefix="confidence-evaluation",
        )
        return self


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimal_places))


def _power(value: Decimal, exponent: Decimal) -> Decimal:
    if value == 0:
        return Decimal(0)
    return (value.ln() * exponent).exp()


def _evaluation_context(precision: int) -> Context:
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def evaluate_continuous_confidence(
    policy: ContinuousConfidencePolicy,
    evidence: ContinuousConfidenceInput,
) -> ContinuousConfidenceEvaluation:
    """Evaluate the versioned formula without binary floating point or provider branching."""

    with localcontext(_evaluation_context(policy.calculation_precision)):
        prior_total = policy.reliability_prior_success + policy.reliability_prior_failure
        source_scores: list[SourceConfidenceScore] = []
        grouped: dict[str, list[tuple[SourceConfidenceEvidence, Decimal]]] = defaultdict(list)
        for source in evidence.sources:
            observed_mass = source.successful_outcome_mass + source.failed_outcome_mass
            reliability = (source.successful_outcome_mass + policy.reliability_prior_success) / (
                observed_mass + prior_total
            )
            if observed_mass == 0:
                reliability = min(reliability, policy.unobserved_reliability_ceiling)
            source_quality = reliability * source.freshness * source.sample_conformance * source.transport_integrity
            source_scores.append(
                SourceConfidenceScore(
                    source_evidence_id=source.source_evidence_id,
                    provider_id=source.provider_id,
                    origin_group_id=source.origin_group_id,
                    reliability=_quantize(reliability, policy.output_decimal_places),
                    source_quality=_quantize(source_quality, policy.output_decimal_places),
                )
            )
            grouped[source.origin_group_id].append((source, source_quality))

        origin_groups: list[OriginGroupContribution] = []
        evidence_mass = Decimal(0)
        for origin_group_id, candidates in grouped.items():
            selected, selected_quality = sorted(
                candidates,
                key=lambda item: (-item[1], item[0].source_evidence_id),
            )[0]
            effective_support = selected.independence_weight * selected_quality
            evidence_mass += effective_support
            origin_groups.append(
                OriginGroupContribution(
                    origin_group_id=origin_group_id,
                    independence_weight=selected.independence_weight,
                    selected_source_evidence_id=selected.source_evidence_id,
                    selected_source_quality=_quantize(selected_quality, policy.output_decimal_places),
                    effective_support=_quantize(effective_support, policy.output_decimal_places),
                )
            )

        source_support = Decimal(1) - (-evidence_mass).exp()
        confidence = (
            source_support
            * _power(evidence.agreement, policy.agreement_exponent)
            * _power(evidence.semantic_mapping_quality, policy.semantic_mapping_exponent)
            * _power(evidence.lineage_completeness, policy.lineage_exponent)
            * _power(evidence.required_component_completeness, policy.completeness_exponent)
        )
        confidence = _quantize(confidence, policy.output_decimal_places)
        score_100 = _quantize(confidence * Decimal(100), policy.output_decimal_places)
        quantized_evidence_mass = _quantize(evidence_mass, policy.output_decimal_places)
        quantized_source_support = _quantize(source_support, policy.output_decimal_places)

    reason_codes = {"confidence.evaluated"}
    if len(grouped) == 1:
        reason_codes.add("support.single-origin-ceiling")
    if len(evidence.sources) > len(grouped):
        reason_codes.add("support.same-origin-deduplicated")
    if any(source.successful_outcome_mass + source.failed_outcome_mass == 0 for source in evidence.sources):
        reason_codes.add("reliability.provisional-unobserved-ceiling")
    if evidence.agreement < 1:
        reason_codes.add("quality.source-disagreement")
    if evidence.semantic_mapping_quality < 1:
        reason_codes.add("quality.semantic-mapping-penalty")
    if evidence.lineage_completeness < 1:
        reason_codes.add("quality.lineage-penalty")
    if evidence.required_component_completeness < 1:
        reason_codes.add("quality.required-component-penalty")
    if any(source.transport_integrity == 0 for source in evidence.sources):
        reason_codes.add("evidence.raw-transport-missing")

    return ContinuousConfidenceEvaluation(
        policy_id=policy.policy_id,
        policy_sha256=policy.content_sha256,
        input_id=evidence.input_id,
        input_sha256=evidence.content_sha256,
        source_scores=tuple(source_scores),
        origin_groups=tuple(origin_groups),
        evidence_mass=quantized_evidence_mass,
        source_support=quantized_source_support,
        agreement=evidence.agreement,
        semantic_mapping_quality=evidence.semantic_mapping_quality,
        lineage_completeness=evidence.lineage_completeness,
        required_component_completeness=evidence.required_component_completeness,
        confidence=confidence,
        score_100=score_100,
        reason_codes=tuple(reason_codes),
    )


class ConfidenceCalibrationScenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(pattern=_STABLE_COORDINATE)
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


class ConfidenceCalibrationReport(BaseModel):
    """Versioned report that cannot hide an incomplete calibration denominator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str = Field(default="", pattern=r"^(?:|confidence-calibration-report:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schema_version: Literal["truealpha.confidence-calibration@v1"] = "truealpha.confidence-calibration@v1"
    policy: ContinuousConfidencePolicy
    denominator_id: str = Field(pattern=_STABLE_COORDINATE)
    denominator_size: int = Field(gt=0)
    empirically_observed_subject_ids: tuple[str, ...]
    scenarios: tuple[ConfidenceCalibrationScenario, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = Field(min_length=1)
    claim_ceiling: Literal["development_sensitivity_only"] = "development_sensitivity_only"

    @field_validator("empirically_observed_subject_ids")
    @classmethod
    def validate_subjects(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(values, "empirically_observed_subject_ids", allow_empty=True)

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("limitations must not contain duplicates")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        scenarios = tuple(sorted(self.scenarios, key=lambda value: value.scenario_id))
        scenario_ids = tuple(scenario.scenario_id for scenario in scenarios)
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("calibration scenario IDs must be unique")
        if len(self.empirically_observed_subject_ids) > self.denominator_size:
            raise ValueError("empirical subjects cannot exceed the frozen denominator")
        for scenario in scenarios:
            if scenario.evaluation.policy_id != self.policy.policy_id:
                raise ValueError("every calibration scenario must use the report policy")
            expected = evaluate_continuous_confidence(self.policy, scenario.input)
            if scenario.evaluation != expected:
                raise ValueError("every calibration evaluation must exactly reproduce from policy and input")
        object.__setattr__(self, "scenarios", scenarios)
        _freeze_content_addressed(
            self,
            id_field="report_id",
            prefix="confidence-calibration-report",
        )
        return self
