"""Sample-driven, source-neutral requests for long-lived DataHub service."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import UniverseRef
from truealpha_contracts.usage import DataRequirement

_SHA256 = r"^[0-9a-f]{64}$"
_STABLE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$"
_FIELD_NAME = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"
_MUTABLE_VERSION_TOKENS = frozenset({"current", "default", "head", "latest", "main", "master", "stable", "tip"})


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _identify(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    digest = canonical_sha256(payload)
    expected_id = f"{prefix}:{digest}"
    supplied_id = getattr(model, id_field)
    supplied_hash = getattr(model, "content_sha256")
    if supplied_id not in {"", expected_id} or supplied_hash not in {"", digest}:
        raise ValueError(f"{prefix} identity does not match canonical content")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", digest)


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    return value


def _unique_sorted[T](values: tuple[T, ...], *, key: Any, label: str) -> tuple[T, ...]:
    ordered = tuple(sorted(values, key=key))
    identities = [key(item) for item in ordered]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} must be unique")
    return ordered


class DemandRequesterKind(StrEnum):
    FACTOR = "factor"
    STRATEGY = "strategy"
    RESEARCH = "research"


class SampleAssertionOperator(StrEnum):
    EXACT = "exact"
    PRESENT = "present"
    ABSENT = "absent"
    ABSOLUTE_TOLERANCE = "absolute_tolerance"
    RELATIVE_TOLERANCE = "relative_tolerance"


class FieldValueKind(StrEnum):
    DECIMAL = "decimal"
    INTEGER = "integer"
    STRING = "string"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class UnitBehavior(StrEnum):
    UNITLESS = "unitless"
    FIXED = "fixed"
    FROM_RECORD = "from_record"


class ValidTimeBehavior(StrEnum):
    INSTANT = "instant"
    INTERVAL = "interval"
    REPORTING_PERIOD = "reporting_period"
    NOT_APPLICABLE = "not_applicable"


class ConfidenceTargetBand(StrEnum):
    WEAK = "weak"
    PROVISIONAL = "provisional"
    HIGH = "high"
    VERY_HIGH = "very_high"


class QualityReportDimension(StrEnum):
    DENOMINATOR = "denominator"
    TERMINAL_STATE = "terminal_state"
    COVERAGE = "coverage"
    AVAILABILITY = "availability"
    FRESHNESS = "freshness"
    CONFIDENCE = "confidence"
    SOURCE_COMPOSITION = "source_composition"
    CONFLICTS = "conflicts"
    LINEAGE = "lineage"
    RETRIES = "retries"
    UNAVAILABLE_REASONS = "unavailable_reasons"


class DemandIntakeStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DemandIntakeReasonCode(StrEnum):
    REQUESTER_INVALID = "requester.invalid"
    REQUIREMENT_BINDING_INVALID = "requirement.binding_invalid"
    FIELD_SEMANTICS_INVALID = "field_semantics.invalid"
    SAMPLE_INVALID = "sample.invalid"
    QUALITY_OBJECTIVE_INVALID = "quality_objective.invalid"
    RECOMPUTATION_INVALID = "recomputation.invalid"
    CROSS_CONTRACT_INVALID = "cross_contract.invalid"


class DemandRequester(_FrozenModel):
    kind: DemandRequesterKind
    requester_id: str = Field(pattern=_STABLE_ID)
    requester_version: str = Field(pattern=_STABLE_ID)
    requester_definition_id: str = Field(pattern=_STABLE_ID)
    requester_definition_sha256: str = Field(pattern=_SHA256)

    @field_validator("requester_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        tokens = set(re.split(r"[._:/@+\-]", value.lower()))
        if tokens & _MUTABLE_VERSION_TOKENS:
            raise ValueError("requester_version must name an immutable version")
        return value


class SampleArtifact(_FrozenModel):
    """Transport-neutral reference to exact sample bytes supplied with a demand."""

    sample_artifact_id: str = Field(default="", pattern=r"^(?:|sample-artifact:[0-9a-f]{64})$")
    artifact_sha256: str = Field(pattern=_SHA256)
    relative_path: str = Field(min_length=1)
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    byte_length: int = Field(gt=0)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            value.startswith("/")
            or "\\" in value
            or "://" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("sample artifact path must be a safe relative POSIX path")
        return value

    @model_validator(mode="after")
    def identify(self) -> Self:
        expected_id = f"sample-artifact:{self.artifact_sha256}"
        if self.sample_artifact_id not in {"", expected_id}:
            raise ValueError("sample artifact identity must equal the byte SHA-256")
        object.__setattr__(self, "sample_artifact_id", expected_id)
        return self


class SampleAssertion(_FrozenModel):
    sample_assertion_id: str = Field(default="", pattern=r"^(?:|sample-assertion:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    field_name: str = Field(pattern=_FIELD_NAME)
    operator: SampleAssertionOperator
    expected_value: str | bool | None = None
    tolerance: Decimal | None = Field(default=None, gt=0)

    @field_validator("expected_value", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        value = _reject_float(value)
        if isinstance(value, int) and not isinstance(value, bool):
            raise ValueError("sample assertion values must use a canonical string representation")
        return value

    @field_validator("tolerance", mode="before")
    @classmethod
    def reject_invalid_tolerance(cls, value: Any) -> Any:
        value = _reject_float(value)
        if isinstance(value, bool):
            raise ValueError("tolerance must be a finite base-10 value")
        if value is not None:
            try:
                if not Decimal(value).is_finite():
                    raise ValueError("tolerance must be a finite base-10 value")
            except InvalidOperation as error:
                raise ValueError("tolerance must be a finite base-10 value") from error
        return value

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        tolerance_operator = self.operator in {
            SampleAssertionOperator.ABSOLUTE_TOLERANCE,
            SampleAssertionOperator.RELATIVE_TOLERANCE,
        }
        presence_operator = self.operator in {SampleAssertionOperator.PRESENT, SampleAssertionOperator.ABSENT}
        if tolerance_operator != (self.tolerance is not None):
            raise ValueError("tolerance exists exactly for a tolerance assertion")
        if presence_operator != (self.expected_value is None):
            raise ValueError("presence assertions cannot carry an expected value")
        if not presence_operator and self.expected_value is None:
            raise ValueError("value assertions require an expected value")
        _identify(self, id_field="sample_assertion_id", prefix="sample-assertion")
        return self


class SampleCase(_FrozenModel):
    sample_case_id: str = Field(default="", pattern=r"^(?:|sample-case:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    case_name: str = Field(pattern=_STABLE_ID)
    sample_artifact_id: str = Field(pattern=r"^sample-artifact:[0-9a-f]{64}$")
    assertions: tuple[SampleAssertion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_and_identify(self) -> Self:
        assertions = _unique_sorted(
            self.assertions,
            key=lambda item: item.sample_assertion_id,
            label="sample assertions",
        )
        coordinates = [(item.requirement_id, item.field_name) for item in assertions]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("a sample case cannot assert the same field twice")
        object.__setattr__(self, "assertions", assertions)
        _identify(self, id_field="sample_case_id", prefix="sample-case")
        return self


class RepresentativeSampleManifest(_FrozenModel):
    sample_manifest_id: str = Field(default="", pattern=r"^(?:|sample-manifest:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    artifacts: tuple[SampleArtifact, ...] = Field(min_length=1)
    cases: tuple[SampleCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_and_identify(self) -> Self:
        artifacts = _unique_sorted(
            self.artifacts,
            key=lambda item: item.sample_artifact_id,
            label="sample artifacts",
        )
        cases = _unique_sorted(self.cases, key=lambda item: item.sample_case_id, label="sample cases")
        artifact_ids = {item.sample_artifact_id for item in artifacts}
        referenced_ids = {item.sample_artifact_id for item in cases}
        if not referenced_ids <= artifact_ids:
            raise ValueError("sample cases cannot reference undeclared artifacts")
        if not artifact_ids <= referenced_ids:
            raise ValueError("every sample artifact must be referenced by at least one case")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "cases", cases)
        _identify(self, id_field="sample_manifest_id", prefix="sample-manifest")
        return self


class FieldSemanticExpectation(_FrozenModel):
    field_semantics_id: str = Field(default="", pattern=r"^(?:|field-semantics:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    field_name: str = Field(pattern=_FIELD_NAME)
    definition: str = Field(min_length=1)
    value_kind: FieldValueKind
    required: bool
    nullable: bool
    unit_behavior: UnitBehavior
    unit: str | None = Field(default=None, pattern=_STABLE_ID)
    valid_time_behavior: ValidTimeBehavior
    knowable_time_rule_id: str = Field(pattern=_STABLE_ID)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if (self.unit_behavior is UnitBehavior.FIXED) != (self.unit is not None):
            raise ValueError("unit exists exactly for fixed-unit field semantics")
        if self.definition != self.definition.strip():
            raise ValueError("field definition cannot have surrounding whitespace")
        _identify(self, id_field="field_semantics_id", prefix="field-semantics")
        return self


class ServiceRequirement(_FrozenModel):
    service_requirement_id: str = Field(default="", pattern=r"^(?:|service-requirement:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    data_requirement: DataRequirement
    refresh_cadence: timedelta
    freshness_max_age: timedelta
    fields: tuple[FieldSemanticExpectation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.refresh_cadence != self.data_requirement.cadence:
            raise ValueError("service refresh cadence disagrees with the data requirement")
        if self.freshness_max_age != self.data_requirement.maximum_age:
            raise ValueError("service freshness maximum age disagrees with the data requirement")
        fields = _unique_sorted(self.fields, key=lambda item: item.field_name, label="service fields")
        if any(item.requirement_id != self.data_requirement.requirement_id for item in fields):
            raise ValueError("field semantics reference another data requirement")
        object.__setattr__(self, "fields", fields)
        _identify(self, id_field="service_requirement_id", prefix="service-requirement")
        return self


class DataQualityObjective(_FrozenModel):
    quality_objective_id: str = Field(default="", pattern=r"^(?:|quality-objective:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    minimum_coverage: Decimal = Field(ge=0, le=1)
    minimum_availability: Decimal = Field(ge=0, le=1)
    confidence_policy_id: str = Field(pattern=r"^confidence-policy:[0-9a-f]{64}$")
    confidence_policy_sha256: str = Field(pattern=_SHA256)
    minimum_confidence_score: Decimal = Field(ge=0, le=100)
    confidence_target_band: ConfidenceTargetBand
    minimum_independent_origin_groups: int = Field(ge=1)
    origin_group_rule: Literal["canonical_original_source"] = "canonical_original_source"
    conflict_behavior: Literal["report_and_abstain"] = "report_and_abstain"
    require_complete_lineage: Literal[True] = True
    report_cadence: timedelta
    report_dimensions: frozenset[QualityReportDimension]

    @field_validator("minimum_coverage", "minimum_availability", "minimum_confidence_score", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        value = _reject_float(value)
        if isinstance(value, bool):
            raise ValueError("quality objective decimals cannot be booleans")
        return value

    @field_serializer("report_dimensions", when_used="json")
    def serialize_dimensions(self, values: frozenset[QualityReportDimension]) -> list[str]:
        return sorted(value.value for value in values)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.confidence_policy_id.rsplit(":", 1)[-1] != self.confidence_policy_sha256:
            raise ValueError("confidence policy ID and hash disagree")
        if self.report_cadence <= timedelta(0):
            raise ValueError("quality report cadence must be positive")
        high_bands = {ConfidenceTargetBand.HIGH, ConfidenceTargetBand.VERY_HIGH}
        if self.confidence_target_band in high_bands and self.minimum_independent_origin_groups < 2:
            raise ValueError("high confidence requires at least two independent origin groups")
        required_dimensions = frozenset(QualityReportDimension)
        if self.report_dimensions != required_dimensions:
            raise ValueError("quality reports must retain every required dimension")
        _identify(self, id_field="quality_objective_id", prefix="quality-objective")
        return self


class DownstreamRecomputationHandoff(_FrozenModel):
    handoff_id: str = Field(default="", pattern=r"^(?:|recomputation-handoff:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    materialization_kind: str = Field(pattern=_STABLE_ID)
    definition_id: str = Field(pattern=_STABLE_ID)
    definition_sha256: str = Field(pattern=_SHA256)
    input_requirement_ids: tuple[str, ...] = Field(min_length=1)
    trigger: Literal["on_accepted_exact_snapshot"] = "on_accepted_exact_snapshot"
    overlap_policy: Literal["do_not_overlap"] = "do_not_overlap"
    idempotency_rule: Literal["exact_snapshot_and_definition"] = "exact_snapshot_and_definition"

    @field_validator("input_requirement_ids")
    @classmethod
    def normalize_requirement_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        ordered = tuple(sorted(values))
        if len(ordered) != len(set(ordered)) or any(
            re.fullmatch(r"data-requirement:[0-9a-f]{64}", value) is None for value in ordered
        ):
            raise ValueError("handoff input requirement IDs must be unique content addresses")
        return ordered

    @model_validator(mode="after")
    def identify(self) -> Self:
        if self.definition_id.rsplit(":", 1)[-1] != self.definition_sha256:
            raise ValueError("recomputation definition ID and hash disagree")
        _identify(self, id_field="handoff_id", prefix="recomputation-handoff")
        return self


class DataHubServiceDemand(_FrozenModel):
    """One accepted request for DataHub to operate a long-lived data service."""

    service_demand_id: str = Field(default="", pattern=r"^(?:|datahub-service-demand:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    requester: DemandRequester
    universe: UniverseRef
    requirements: tuple[ServiceRequirement, ...] = Field(min_length=1)
    representative_samples: RepresentativeSampleManifest
    quality_objective: DataQualityObjective
    recomputation_handoffs: tuple[DownstreamRecomputationHandoff, ...] = Field(min_length=1)
    effective_at: datetime

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        requirements = _unique_sorted(
            self.requirements,
            key=lambda item: item.data_requirement.requirement_id,
            label="service requirements",
        )
        handoffs = _unique_sorted(
            self.recomputation_handoffs,
            key=lambda item: item.handoff_id,
            label="recomputation handoffs",
        )
        requirement_ids = {item.data_requirement.requirement_id for item in requirements}
        declared_fields = {
            (field.requirement_id, field.field_name): field
            for requirement in requirements
            for field in requirement.fields
        }
        asserted_fields = {
            (assertion.requirement_id, assertion.field_name)
            for case in self.representative_samples.cases
            for assertion in case.assertions
        }
        if not asserted_fields.issubset(declared_fields):
            raise ValueError("sample assertions reference undeclared field semantics")
        for case in self.representative_samples.cases:
            for assertion in case.assertions:
                _validate_assertion_value(assertion, declared_fields[(assertion.requirement_id, assertion.field_name)])
        required_fields = {coordinate for coordinate, field in declared_fields.items() if field.required}
        if not required_fields.issubset(asserted_fields):
            raise ValueError("representative samples must assert every required field")
        if any(not set(item.input_requirement_ids).issubset(requirement_ids) for item in handoffs):
            raise ValueError("recomputation handoff references an undeclared requirement")
        object.__setattr__(self, "requirements", requirements)
        object.__setattr__(self, "recomputation_handoffs", handoffs)
        _identify(self, id_field="service_demand_id", prefix="datahub-service-demand")
        return self


def _validate_assertion_value(assertion: SampleAssertion, field: FieldSemanticExpectation) -> None:
    if assertion.operator in {SampleAssertionOperator.PRESENT, SampleAssertionOperator.ABSENT}:
        return
    tolerance_operator = assertion.operator in {
        SampleAssertionOperator.ABSOLUTE_TOLERANCE,
        SampleAssertionOperator.RELATIVE_TOLERANCE,
    }
    if tolerance_operator and field.value_kind not in {FieldValueKind.DECIMAL, FieldValueKind.INTEGER}:
        raise ValueError("tolerance assertions require a numeric declared field")

    value = assertion.expected_value
    if field.value_kind is FieldValueKind.DECIMAL:
        if isinstance(value, bool):
            raise ValueError("decimal sample assertions require a finite base-10 value")
        try:
            parsed_decimal = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError("decimal sample assertions require a finite base-10 value") from error
        if not parsed_decimal.is_finite():
            raise ValueError("decimal sample assertions require a finite base-10 value")
    elif field.value_kind is FieldValueKind.INTEGER:
        if isinstance(value, bool) or not (
            isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value) is not None)
        ):
            raise ValueError("integer sample assertions require a base-10 integer")
    elif field.value_kind is FieldValueKind.STRING:
        if not isinstance(value, str):
            raise ValueError("string sample assertions require a string value")
    elif field.value_kind is FieldValueKind.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError("boolean sample assertions require a boolean value")
    elif field.value_kind is FieldValueKind.DATE:
        if not isinstance(value, str):
            raise ValueError("date sample assertions require an ISO date")
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as error:
            raise ValueError("date sample assertions require an ISO date") from error
    elif field.value_kind is FieldValueKind.DATETIME:
        if not isinstance(value, str):
            raise ValueError("datetime sample assertions require an aware ISO datetime")
        try:
            normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
            _require_aware(datetime.fromisoformat(normalized), "sample assertion datetime")
        except ValueError as error:
            raise ValueError("datetime sample assertions require an aware ISO datetime") from error


class DataHubDemandIntakeReport(_FrozenModel):
    intake_report_id: str = Field(default="", pattern=r"^(?:|demand-intake-report:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    payload_sha256: str = Field(pattern=_SHA256)
    status: DemandIntakeStatus
    accepted_demand: DataHubServiceDemand | None = None
    reason_codes: tuple[DemandIntakeReasonCode, ...]

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        accepted = self.status is DemandIntakeStatus.ACCEPTED
        if accepted != (self.accepted_demand is not None) or accepted == bool(self.reason_codes):
            raise ValueError("intake status, accepted demand, and reason codes disagree")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes), key=str)))
        _identify(self, id_field="intake_report_id", prefix="demand-intake-report")
        return self


def evaluate_datahub_service_demand(payload: dict[str, Any]) -> DataHubDemandIntakeReport:
    """Validate an untrusted demand and return stable, non-secret-bearing reason codes."""

    payload_sha256 = canonical_sha256(payload)
    try:
        demand = DataHubServiceDemand.model_validate(payload)
    except ValidationError as error:
        reason_codes: set[DemandIntakeReasonCode] = set()
        for item in error.errors(include_input=False):
            root = str(item["loc"][0]) if item["loc"] else ""
            if root == "requester":
                reason_codes.add(DemandIntakeReasonCode.REQUESTER_INVALID)
            elif root == "requirements":
                location = tuple(str(part) for part in item["loc"])
                reason_codes.add(
                    DemandIntakeReasonCode.FIELD_SEMANTICS_INVALID
                    if "fields" in location
                    else DemandIntakeReasonCode.REQUIREMENT_BINDING_INVALID
                )
            elif root == "representative_samples":
                reason_codes.add(DemandIntakeReasonCode.SAMPLE_INVALID)
            elif root == "quality_objective":
                reason_codes.add(DemandIntakeReasonCode.QUALITY_OBJECTIVE_INVALID)
            elif root == "recomputation_handoffs":
                reason_codes.add(DemandIntakeReasonCode.RECOMPUTATION_INVALID)
            else:
                reason_codes.add(DemandIntakeReasonCode.CROSS_CONTRACT_INVALID)
        return DataHubDemandIntakeReport(
            payload_sha256=payload_sha256,
            status=DemandIntakeStatus.REJECTED,
            reason_codes=tuple(reason_codes),
        )
    return DataHubDemandIntakeReport(
        payload_sha256=payload_sha256,
        status=DemandIntakeStatus.ACCEPTED,
        accepted_demand=demand,
        reason_codes=(),
    )


__all__ = [
    "ConfidenceTargetBand",
    "DataHubDemandIntakeReport",
    "DataHubServiceDemand",
    "DataQualityObjective",
    "DemandIntakeReasonCode",
    "DemandIntakeStatus",
    "DemandRequester",
    "DemandRequesterKind",
    "DownstreamRecomputationHandoff",
    "FieldSemanticExpectation",
    "FieldValueKind",
    "QualityReportDimension",
    "RepresentativeSampleManifest",
    "SampleArtifact",
    "SampleAssertion",
    "SampleAssertionOperator",
    "SampleCase",
    "ServiceRequirement",
    "UnitBehavior",
    "ValidTimeBehavior",
    "evaluate_datahub_service_demand",
]
