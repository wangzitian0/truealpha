"""Environment-neutral, row-complete capture contracts.

The scope freezes semantic demand and immutable catalog/registry bindings.  A
manifest records what one environment actually captured.  Readiness is derived
by joining the manifest to the applicability denominator frozen before the run;
neither a green orchestrator run nor a producer-supplied status can waive a row.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.models import _require_aware
from truealpha_contracts.readiness import ApplicabilityCatalog, ApplicabilityClassification, SourceCoverageCatalog
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.usage import DataRequirement

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_SHA256_PATTERN = r"^(?:|[0-9a-f]{64})$"
_STABLE_ID_PATTERN = r"^[a-z0-9][a-z0-9._:/@+\-]*$"
_SEMANTIC_TYPE_PATTERN = r"^semantic\.[a-z0-9]+(?:[._-][a-z0-9]+)*$"
_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(_SHA256_PATTERN)

CaptureApplicability = Literal["required", "optional", "not_applicable"]
CaptureStatus = Literal["complete", "optional", "not_applicable", "missing", "stale", "unresolved", "error"]
CaptureCellKey = tuple[SubjectKind, str, DataDomain, str, str]
ApplicabilityBinding = tuple[CaptureApplicability, datetime]
ApplicabilityMapping = Mapping[CaptureCellKey, ApplicabilityBinding]
SourceCoverageCellKey = tuple[CaptureEnvironment, SubjectKind, str, DataDomain, str, str]
SourceCoverageMapping = Mapping[SourceCoverageCellKey, tuple[str, ...]]


def _content_address(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, "content_sha256")
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError("content_sha256 does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, "content_sha256", expected_hash)
    object.__setattr__(model, id_field, expected_id)


def _validate_reference_pair(reference_id: str, content_sha256: str, field_name: str) -> None:
    suffix = reference_id.rsplit(":", 1)[-1]
    if _SHA256.fullmatch(suffix) and suffix != content_sha256:
        raise ValueError(f"{field_name} ID and hash do not match")


def _stable_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    if any(not re.fullmatch(_STABLE_ID_PATTERN, value) for value in values):
        raise ValueError(f"{field_name} must contain stable identifiers")
    return tuple(sorted(values))


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CaptureRequirement(BaseModel):
    """Source-neutral semantic demand shared by every environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_requirement_id: str = Field(
        default="",
        pattern=r"^(?:|capture-requirement:[0-9a-f]{64})$",
    )
    semantic_type_id: str = Field(pattern=_SEMANTIC_TYPE_PATTERN)
    semantic_type_version: str = Field(pattern=_STABLE_ID_PATTERN)
    domain: DataDomain
    required_fields: tuple[str, ...] = Field(min_length=1)
    subject_kinds: tuple[SubjectKind, ...] = Field(min_length=1)
    cadence: timedelta
    partition_rule_id: str = Field(pattern=_STABLE_ID_PATTERN)
    freshness_policy_id: str = Field(pattern=_STABLE_ID_PATTERN)
    maximum_age: timedelta
    quality_policy_ids: tuple[str, ...] = Field(min_length=1)
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)

    @field_validator("required_fields")
    @classmethod
    def validate_required_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("required_fields must not contain duplicates")
        if any(not _FIELD_PATTERN.fullmatch(value) for value in values):
            raise ValueError("required_fields must contain canonical field names")
        return tuple(sorted(values))

    @field_validator("subject_kinds")
    @classmethod
    def validate_subject_kinds(cls, values: tuple[SubjectKind, ...]) -> tuple[SubjectKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("subject_kinds must not contain duplicates")
        return tuple(sorted(values, key=lambda value: value.value))

    @field_validator("quality_policy_ids")
    @classmethod
    def validate_stable_ids(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _stable_tuple(values, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> CaptureRequirement:
        if self.cadence <= timedelta(0):
            raise ValueError("cadence must be positive")
        if self.maximum_age <= timedelta(0):
            raise ValueError("maximum_age must be positive")
        _content_address(self, id_field="capture_requirement_id", prefix="capture-requirement")
        return self


class CaptureScope(BaseModel):
    """Immutable environment-neutral denominator promoted unchanged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_scope_id: str = Field(default="", pattern=r"^(?:|capture-scope:[0-9a-f]{64})$")
    research_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    research_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    applicability_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    applicability_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_coverage_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    source_coverage_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_coverage_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    slo_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    slo_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_id: str = Field(pattern=_STABLE_ID_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_type_registry_id: str = Field(pattern=_STABLE_ID_PATTERN)
    semantic_type_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    requirements: tuple[CaptureRequirement, ...] = Field(min_length=1)
    effective_at: datetime
    owner: str = Field(min_length=1)
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("owner cannot have surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_and_identify(self) -> CaptureScope:
        requirements = tuple(sorted(self.requirements, key=lambda value: value.capture_requirement_id))
        requirement_ids = [value.capture_requirement_id for value in requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirements must not contain duplicate content")
        object.__setattr__(self, "requirements", requirements)
        for id_field, hash_field in (
            ("research_catalog_id", "research_catalog_sha256"),
            ("applicability_catalog_id", "applicability_catalog_sha256"),
            ("source_coverage_catalog_id", "source_coverage_catalog_sha256"),
            ("slo_catalog_id", "slo_catalog_sha256"),
            ("source_registry_id", "source_registry_sha256"),
            ("semantic_type_registry_id", "semantic_type_registry_sha256"),
        ):
            _validate_reference_pair(getattr(self, id_field), getattr(self, hash_field), id_field)
        _content_address(self, id_field="capture_scope_id", prefix="capture-scope")
        return self

    def requirement_map(self) -> dict[str, CaptureRequirement]:
        return {requirement.capture_requirement_id: requirement for requirement in self.requirements}


class CaptureRecordEvidence(BaseModel):
    """One exact raw-checksum to normalized-record lineage edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|capture-evidence:[0-9a-f]{64})$")
    source_coverage_entry_id: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    raw_id: str | None = Field(default=None, pattern=r"^raw(?:\.[a-z][a-z0-9_]*)+:[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    raw_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    normalized_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$",
    )
    semantic_type_id: str | None = Field(default=None, pattern=_SEMANTIC_TYPE_PATTERN)
    semantic_type_version: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    populated_fields: tuple[str, ...] = ()
    knowable_at: datetime | None = None
    recorded_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    mapping_version: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    policy_versions: dict[str, str] = Field(default_factory=dict)
    quality_check_ids: tuple[str, ...] = ()
    quality_status: QualityStatus | None = None
    lineage_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)

    @field_validator("knowable_at", "recorded_at", "valid_from", "valid_to")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @field_validator("policy_versions")
    @classmethod
    def validate_policy_versions(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            not re.fullmatch(_STABLE_ID_PATTERN, key) or not re.fullmatch(_STABLE_ID_PATTERN, value)
            for key, value in values.items()
        ):
            raise ValueError("policy_versions must bind stable policy IDs to stable versions")
        return dict(sorted(values.items()))

    @field_validator("quality_check_ids")
    @classmethod
    def validate_quality_checks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_tuple(values, "quality_check_ids", allow_empty=True)

    @field_validator("populated_fields")
    @classmethod
    def validate_populated_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("populated_fields must not contain duplicates")
        if any(not _FIELD_PATTERN.fullmatch(value) for value in values):
            raise ValueError("populated_fields must contain canonical field names")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def validate_and_identify(self) -> CaptureRecordEvidence:
        if self.knowable_at is not None and self.recorded_at is not None and self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        _content_address(self, id_field="evidence_id", prefix="capture-evidence")
        return self

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return self.raw_id or "", self.raw_sha256 or "", self.normalized_id or "", self.evidence_id


class CaptureCell(BaseModel):
    """One manifest row at subject, semantic domain, and vintage grain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_cell_id: str = Field(default="", pattern=r"^(?:|capture-cell:[0-9a-f]{64})$")
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(pattern=_STABLE_ID_PATTERN)
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    applicability: CaptureApplicability
    status: CaptureStatus
    evidence: tuple[CaptureRecordEvidence, ...] = ()
    reason_codes: tuple[str, ...] = ()
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_tuple(values, "reason_codes", allow_empty=True)

    @model_validator(mode="after")
    def normalize_and_identify(self) -> CaptureCell:
        object.__setattr__(self, "evidence", tuple(sorted(self.evidence, key=lambda value: value.sort_key)))
        _content_address(self, id_field="capture_cell_id", prefix="capture-cell")
        return self

    @property
    def key(self) -> CaptureCellKey:
        return self.subject.kind, self.subject.id, self.domain, self.partition_key, self.capture_requirement_id


class CaptureManifest(BaseModel):
    """Observed capture rows plus every immutable binding needed to evaluate them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_manifest_id: str = Field(default="", pattern=r"^(?:|capture-manifest:[0-9a-f]{64})$")
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment: CaptureEnvironment
    research_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    research_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    applicability_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    applicability_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_coverage_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    source_coverage_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    slo_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    slo_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_id: str = Field(pattern=_STABLE_ID_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_type_registry_id: str = Field(pattern=_STABLE_ID_PATTERN)
    semantic_type_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    partition_key: str = Field(pattern=_STABLE_ID_PATTERN)
    as_of: datetime
    started_at: datetime
    cells: tuple[CaptureCell, ...] = ()
    created_at: datetime
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)

    @field_validator("as_of", "started_at", "created_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def normalize_and_identify(self) -> CaptureManifest:
        if self.environment is CaptureEnvironment.LOCAL:
            raise ValueError("exact capture manifests require an explicit logical environment tier")
        if self.created_at < self.started_at:
            raise ValueError("created_at must not precede started_at")
        if self.as_of > self.created_at:
            raise ValueError("as_of must not follow manifest creation")
        cells = tuple(sorted(self.cells, key=lambda value: (*_sortable_key(value.key), value.content_sha256)))
        object.__setattr__(self, "cells", cells)
        _validate_reference_pair(self.capture_scope_id, self.capture_scope_sha256, "capture_scope_id")
        for id_field, hash_field in (
            ("research_catalog_id", "research_catalog_sha256"),
            ("applicability_catalog_id", "applicability_catalog_sha256"),
            ("source_coverage_catalog_id", "source_coverage_catalog_sha256"),
            ("slo_catalog_id", "slo_catalog_sha256"),
            ("source_registry_id", "source_registry_sha256"),
            ("semantic_type_registry_id", "semantic_type_registry_sha256"),
        ):
            _validate_reference_pair(getattr(self, id_field), getattr(self, hash_field), id_field)
        _content_address(self, id_field="capture_manifest_id", prefix="capture-manifest")
        return self


class CaptureEvaluationReport(BaseModel):
    """Deterministic fail-closed result for one exact manifest/applicability join."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_evaluation_report_id: str = Field(
        default="",
        pattern=r"^(?:|capture-evaluation:[0-9a-f]{64})$",
    )
    evaluator_id: Literal["truealpha.capture-evaluator"] = "truealpha.capture-evaluator"
    evaluator_version: Literal["v1"] = "v1"
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    capture_manifest_id: str = Field(pattern=r"^capture-manifest:[0-9a-f]{64}$")
    capture_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    applicability_catalog_id: str = Field(pattern=_STABLE_ID_PATTERN)
    applicability_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    applicability_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_coverage_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment: CaptureEnvironment
    evaluated_at: datetime
    blocking_reason_codes: tuple[str, ...] = ()
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @field_validator("blocking_reason_codes")
    @classmethod
    def validate_blockers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("blocking_reason_codes must not contain duplicates")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def identify(self) -> CaptureEvaluationReport:
        _validate_reference_pair(self.capture_scope_id, self.capture_scope_sha256, "capture_scope_id")
        _validate_reference_pair(self.capture_manifest_id, self.capture_manifest_sha256, "capture_manifest_id")
        _validate_reference_pair(
            self.applicability_catalog_id,
            self.applicability_catalog_sha256,
            "applicability_catalog_id",
        )
        _content_address(self, id_field="capture_evaluation_report_id", prefix="capture-evaluation")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self.blocking_reason_codes


def _sortable_key(key: CaptureCellKey) -> tuple[str, str, str, str, str]:
    kind, subject_id, domain, partition_key, requirement_id = key
    return kind.value, subject_id, domain.value, partition_key, requirement_id


def _key_label(key: CaptureCellKey) -> str:
    return "/".join(_sortable_key(key))


def _normalize_applicability(
    applicability: ApplicabilityMapping,
) -> tuple[dict[CaptureCellKey, ApplicabilityBinding], str]:
    normalized: dict[CaptureCellKey, ApplicabilityBinding] = {}
    serialized: list[dict[str, object]] = []
    for raw_key, raw_binding in applicability.items():
        if len(raw_key) != 5:
            raise ValueError("applicability keys must have five components")
        kind_value, subject_id, domain_value, partition_key, requirement_id = raw_key
        kind = SubjectKind(kind_value)
        domain = DataDomain(domain_value)
        if not re.fullmatch(_STABLE_ID_PATTERN, subject_id):
            raise ValueError("applicability subject IDs must be stable")
        if not re.fullmatch(_STABLE_ID_PATTERN, partition_key):
            raise ValueError("applicability partition keys must be stable")
        if not re.fullmatch(r"^capture-requirement:[0-9a-f]{64}$", requirement_id):
            raise ValueError("applicability requirement IDs must be content-addressed")
        if len(raw_binding) != 2:
            raise ValueError("applicability values must contain classification and effective_at")
        classification, effective_at = raw_binding
        if classification not in {"required", "optional", "not_applicable"}:
            raise ValueError("applicability classification is invalid")
        effective_at = _require_aware(effective_at, "applicability effective_at")
        key = kind, subject_id, domain, partition_key, requirement_id
        if key in normalized:
            raise ValueError("applicability mapping contains duplicate normalized keys")
        normalized[key] = classification, effective_at
        serialized.append(
            {
                "key": _sortable_key(key),
                "classification": classification,
                "effective_at": _canonical_datetime(effective_at),
            }
        )
    return normalized, canonical_sha256(sorted(serialized, key=lambda value: tuple(value["key"])))


def canonical_applicability_projection_sha256(applicability: ApplicabilityMapping) -> str:
    """Hash the exact classified capture-cell denominator in canonical order."""

    return _normalize_applicability(applicability)[1]


def _source_coverage_sortable_key(key: SourceCoverageCellKey) -> tuple[str, str, str, str, str, str]:
    environment, kind, subject_id, domain, partition_key, requirement_id = key
    return environment.value, kind.value, subject_id, domain.value, partition_key, requirement_id


def _normalize_source_coverage(
    source_coverage: SourceCoverageMapping,
) -> tuple[dict[SourceCoverageCellKey, tuple[str, ...]], str]:
    normalized: dict[SourceCoverageCellKey, tuple[str, ...]] = {}
    serialized: list[dict[str, object]] = []
    for raw_key, raw_entry_ids in source_coverage.items():
        if len(raw_key) != 6:
            raise ValueError("source coverage projection keys must have six components")
        environment_value, kind_value, subject_id, domain_value, partition_key, requirement_id = raw_key
        environment = CaptureEnvironment(environment_value)
        if environment is CaptureEnvironment.LOCAL:
            raise ValueError("source coverage projection requires explicit logical environment tiers")
        kind = SubjectKind(kind_value)
        domain = DataDomain(domain_value)
        if not re.fullmatch(_STABLE_ID_PATTERN, subject_id):
            raise ValueError("source coverage projection subject IDs must be stable")
        if not re.fullmatch(_STABLE_ID_PATTERN, partition_key):
            raise ValueError("source coverage projection partition keys must be stable")
        if not re.fullmatch(r"^capture-requirement:[0-9a-f]{64}$", requirement_id):
            raise ValueError("source coverage projection requirement IDs must be content-addressed")
        entry_ids = tuple(sorted(raw_entry_ids))
        if not entry_ids or len(entry_ids) != len(set(entry_ids)):
            raise ValueError("source coverage projection entry IDs must be non-empty and unique")
        if any(re.fullmatch(r"^source-coverage-entry:[0-9a-f]{64}$", value) is None for value in entry_ids):
            raise ValueError("source coverage projection entry IDs must be content-addressed")
        key = environment, kind, subject_id, domain, partition_key, requirement_id
        if key in normalized:
            raise ValueError("source coverage projection contains duplicate normalized keys")
        normalized[key] = entry_ids
        serialized.append({"key": _source_coverage_sortable_key(key), "entry_ids": entry_ids})
    return normalized, canonical_sha256(sorted(serialized, key=lambda value: tuple(value["key"])))


def canonical_source_coverage_projection_sha256(source_coverage: SourceCoverageMapping) -> str:
    """Hash exact environment/cell to source-coverage-entry bindings."""

    return _normalize_source_coverage(source_coverage)[1]


def compile_capture_requirement_bindings(
    data_requirements: tuple[DataRequirement, ...],
    capture_requirements: tuple[CaptureRequirement, ...],
) -> dict[str, CaptureRequirement]:
    """Compile explicit DataRequirement-to-CaptureRequirement links or fail closed."""

    capture_by_id = {item.capture_requirement_id: item for item in capture_requirements}
    if len(capture_by_id) != len(capture_requirements):
        raise ValueError("capture requirements must be unique")
    data_by_id = {item.requirement_id: item for item in data_requirements}
    if len(data_by_id) != len(data_requirements):
        raise ValueError("data requirements must be unique")

    compiled: dict[str, CaptureRequirement] = {}
    referenced_capture_ids: set[str] = set()
    for demand in data_requirements:
        capture = capture_by_id.get(demand.capture_requirement_id)
        if capture is None:
            raise ValueError(
                f"DataRequirement references an unknown CaptureRequirement: {demand.capture_requirement_id}"
            )
        if (
            demand.semantic_type_id != capture.semantic_type_id
            or demand.domain is not capture.domain
            or set(demand.subject_kinds) != set(capture.subject_kinds)
            or demand.valid_period_rule_id != capture.partition_rule_id
            or demand.maximum_age != capture.maximum_age
            or demand.cadence != capture.cadence
        ):
            raise ValueError("DataRequirement semantics drift from its explicit CaptureRequirement")
        if demand.metric is not None and demand.metric not in capture.required_fields:
            raise ValueError("DataRequirement metric is absent from its CaptureRequirement fields")
        compiled[demand.requirement_id] = capture
        referenced_capture_ids.add(capture.capture_requirement_id)

    unreferenced = set(capture_by_id) - referenced_capture_ids
    if unreferenced:
        raise ValueError(f"CaptureRequirements are not referenced by DataRequirements: {sorted(unreferenced)}")
    return compiled


def project_capture_source_coverage(
    source_coverage_catalog: SourceCoverageCatalog,
    requirements: tuple[CaptureRequirement, ...],
    data_requirements: tuple[DataRequirement, ...],
) -> dict[SourceCoverageCellKey, tuple[str, ...]]:
    """Project the accepted source matrix onto CaptureRequirement identities."""

    requirement_by_data_id = compile_capture_requirement_bindings(data_requirements, requirements)
    projected: dict[SourceCoverageCellKey, list[str]] = defaultdict(list)
    source_requirement_keys = {item.key for item in source_coverage_catalog.requirements}
    entry_cell_keys: set[tuple[CaptureEnvironment, str, str, str, DataDomain, str]] = set()
    for entry in source_coverage_catalog.entries:
        capture_requirement = requirement_by_data_id.get(entry.data_requirement_id)
        if capture_requirement is None:
            raise ValueError(f"source coverage references an unmapped DataRequirement: {entry.data_requirement_id}")
        if (
            entry.semantic_type_id != capture_requirement.semantic_type_id
            or entry.semantic_type_version != capture_requirement.semantic_type_version
            or entry.domain is not capture_requirement.domain
            or entry.subject.kind not in capture_requirement.subject_kinds
        ):
            raise ValueError("source coverage entry semantics do not match its CaptureRequirement")
        key = (
            entry.environment,
            entry.subject.kind,
            entry.subject.id,
            entry.domain,
            entry.partition_key,
            capture_requirement.capture_requirement_id,
        )
        projected[key].append(entry.source_coverage_entry_id)
        entry_cell_keys.add(entry.cell_key)
    missing_entry_cells = source_requirement_keys - entry_cell_keys
    if missing_entry_cells:
        raise ValueError("source coverage requirements are missing source entries")
    referenced = {key[-1] for key in projected}
    missing_requirements = {item.capture_requirement_id for item in requirements} - referenced
    if missing_requirements:
        raise ValueError(f"source coverage omits CaptureRequirements: {sorted(missing_requirements)}")
    return {key: tuple(sorted(set(entry_ids))) for key, entry_ids in projected.items()}


def source_coverage_mapping_from_catalog(
    scope: CaptureScope,
    source_coverage_catalog: SourceCoverageCatalog,
    data_requirements: tuple[DataRequirement, ...],
) -> dict[SourceCoverageCellKey, tuple[str, ...]]:
    """Resolve and verify the exact source projection bound into a CaptureScope."""

    if (
        scope.research_catalog_id != source_coverage_catalog.research_catalog_id
        or scope.research_catalog_sha256 != source_coverage_catalog.research_catalog_sha256
    ):
        raise ValueError("CaptureScope and SourceCoverageCatalog bind different Research Catalogs")
    if scope.universe != source_coverage_catalog.universe:
        raise ValueError("CaptureScope and SourceCoverageCatalog bind different UniverseRefs")
    if (
        scope.source_coverage_catalog_id != source_coverage_catalog.source_coverage_catalog_id
        or scope.source_coverage_catalog_sha256 != source_coverage_catalog.content_sha256
    ):
        raise ValueError("CaptureScope does not bind the supplied SourceCoverageCatalog exactly")
    projected = project_capture_source_coverage(
        source_coverage_catalog,
        scope.requirements,
        data_requirements,
    )
    if canonical_source_coverage_projection_sha256(projected) != scope.source_coverage_projection_sha256:
        raise ValueError("CaptureScope source coverage projection is shrunken or drifted")
    return projected


def project_capture_applicability(
    applicability_catalog: ApplicabilityCatalog,
    requirements: tuple[CaptureRequirement, ...],
    data_requirements: tuple[DataRequirement, ...],
) -> dict[CaptureCellKey, ApplicabilityBinding]:
    """Project module applicability onto the row-complete capture denominator."""

    requirement_by_data_id = compile_capture_requirement_bindings(data_requirements, requirements)
    strength = {
        ApplicabilityClassification.NOT_APPLICABLE: 0,
        ApplicabilityClassification.OPTIONAL: 1,
        ApplicabilityClassification.REQUIRED: 2,
    }
    projected: dict[CaptureCellKey, ApplicabilityBinding] = {}
    referenced_requirements: set[str] = set()
    for cell in applicability_catalog.cells:
        requirement = requirement_by_data_id.get(cell.data_requirement_id)
        if requirement is None:
            raise ValueError(f"applicability references an unmapped DataRequirement: {cell.data_requirement_id}")
        if cell.domain is not requirement.domain:
            raise ValueError("applicability domain does not match its CaptureRequirement")
        if cell.subject.kind not in requirement.subject_kinds:
            raise ValueError("applicability subject kind does not match its CaptureRequirement")
        key = (
            cell.subject.kind,
            cell.subject.id,
            cell.domain,
            cell.partition_key,
            requirement.capture_requirement_id,
        )
        candidate: ApplicabilityBinding = cell.classification.value, cell.effective_at
        current = projected.get(key)
        if current is None:
            projected[key] = candidate
        else:
            current_classification = ApplicabilityClassification(current[0])
            if strength[cell.classification] > strength[current_classification]:
                projected[key] = cell.classification.value, max(current[1], cell.effective_at)
            else:
                projected[key] = current[0], max(current[1], cell.effective_at)
        referenced_requirements.add(requirement.capture_requirement_id)
    missing = {item.capture_requirement_id for item in requirements} - referenced_requirements
    if missing:
        raise ValueError(f"applicability omits CaptureRequirements: {sorted(missing)}")
    return projected


def applicability_mapping_from_catalog(
    scope: CaptureScope,
    applicability_catalog: ApplicabilityCatalog,
    data_requirements: tuple[DataRequirement, ...],
) -> dict[CaptureCellKey, ApplicabilityBinding]:
    """Resolve and verify the exact catalog projection bound into a CaptureScope."""

    if (
        scope.research_catalog_id != applicability_catalog.research_catalog_id
        or scope.research_catalog_sha256 != applicability_catalog.research_catalog_sha256
    ):
        raise ValueError("CaptureScope and ApplicabilityCatalog bind different Research Catalogs")
    if scope.universe != applicability_catalog.universe:
        raise ValueError("CaptureScope and ApplicabilityCatalog bind different UniverseRefs")
    if (
        scope.applicability_catalog_id != applicability_catalog.applicability_catalog_id
        or scope.applicability_catalog_sha256 != applicability_catalog.content_sha256
    ):
        raise ValueError("CaptureScope does not bind the supplied ApplicabilityCatalog exactly")
    projected = project_capture_applicability(
        applicability_catalog,
        scope.requirements,
        data_requirements,
    )
    if canonical_applicability_projection_sha256(projected) != scope.applicability_projection_sha256:
        raise ValueError("CaptureScope applicability projection is shrunken or drifted")
    return projected


def _evaluate_evidence(
    *,
    cell: CaptureCell,
    requirement: CaptureRequirement,
    manifest: CaptureManifest,
    approved_coverage_entries: set[str],
) -> set[str]:
    label = _key_label(cell.key)
    blockers: set[str] = set()
    if not cell.evidence:
        blockers.add(f"evidence.empty:{label}")
        return blockers

    edge_counts: Counter[tuple[str | None, str | None, str | None]] = Counter()
    raw_checksums: dict[str, set[str]] = defaultdict(set)
    required_policy_ids = {requirement.partition_rule_id, requirement.freshness_policy_id}
    required_quality_ids = set(requirement.quality_policy_ids)
    if not approved_coverage_entries:
        blockers.add(f"evidence.source_coverage_environment_unbound:{label}")
    for evidence in cell.evidence:
        edge_counts[(evidence.raw_id, evidence.raw_sha256, evidence.normalized_id)] += 1
        if evidence.raw_id is not None and evidence.raw_sha256 is not None:
            raw_checksums[evidence.raw_id].add(evidence.raw_sha256)
        missing_components = [
            name
            for name, value in (
                ("source_coverage_entry_id", evidence.source_coverage_entry_id),
                ("raw_id", evidence.raw_id),
                ("raw_sha256", evidence.raw_sha256),
                ("normalized_id", evidence.normalized_id),
                ("semantic_type_id", evidence.semantic_type_id),
                ("semantic_type_version", evidence.semantic_type_version),
                ("knowable_at", evidence.knowable_at),
                ("recorded_at", evidence.recorded_at),
                ("valid_from", evidence.valid_from),
                ("confidence", evidence.confidence),
                ("mapping_version", evidence.mapping_version),
                ("quality_status", evidence.quality_status),
                ("lineage_sha256", evidence.lineage_sha256),
            )
            if value is None
        ]
        for component in missing_components:
            blockers.add(f"evidence.missing_{component}:{label}")
        if evidence.source_coverage_entry_id not in approved_coverage_entries:
            blockers.add(f"evidence.unapproved_source_coverage_entry:{label}")
        if (
            evidence.semantic_type_id != requirement.semantic_type_id
            or evidence.semantic_type_version != requirement.semantic_type_version
        ):
            blockers.add(f"evidence.semantic_type_mismatch:{label}")
        if not set(requirement.required_fields).issubset(evidence.populated_fields):
            blockers.add(f"evidence.required_fields_missing:{label}")
        if evidence.quality_status is not QualityStatus.PASS:
            blockers.add(f"evidence.quality_failed:{label}")
        missing_policies = required_policy_ids - set(evidence.policy_versions)
        if missing_policies:
            blockers.add(f"evidence.missing_policy:{label}")
        missing_quality = required_quality_ids - set(evidence.quality_check_ids)
        if missing_quality:
            blockers.add(f"evidence.missing_quality:{label}")
        if evidence.knowable_at is not None:
            if evidence.knowable_at > manifest.as_of:
                blockers.add(f"evidence.future_knowledge:{label}")
            elif manifest.as_of - evidence.knowable_at > requirement.maximum_age:
                blockers.add(f"evidence.stale:{label}")
        if evidence.recorded_at is not None and evidence.recorded_at > manifest.created_at:
            blockers.add(f"evidence.future_recording:{label}")

    if any(count > 1 for count in edge_counts.values()):
        blockers.add(f"evidence.duplicate_lineage_edge:{label}")
    if any(len(checksums) > 1 for checksums in raw_checksums.values()):
        blockers.add(f"evidence.raw_checksum_conflict:{label}")
    return blockers


def evaluate_capture_manifest(
    scope: CaptureScope,
    manifest: CaptureManifest,
    *,
    applicability_catalog_id: str,
    applicability_catalog_sha256: str,
    applicability: ApplicabilityMapping,
    source_coverage: SourceCoverageMapping,
    evaluated_at: datetime,
) -> CaptureEvaluationReport:
    """Evaluate one manifest against the exact pre-run applicability denominator."""

    evaluated_at = _require_aware(evaluated_at, "evaluated_at")
    if not re.fullmatch(_STABLE_ID_PATTERN, applicability_catalog_id):
        raise ValueError("applicability_catalog_id must be a stable identifier")
    if not _SHA256.fullmatch(applicability_catalog_sha256):
        raise ValueError("applicability_catalog_sha256 must be a lowercase SHA-256")
    _validate_reference_pair(applicability_catalog_id, applicability_catalog_sha256, "applicability_catalog_id")
    frozen_applicability, applicability_projection_sha256 = _normalize_applicability(applicability)
    frozen_source_coverage, source_coverage_projection_sha256 = _normalize_source_coverage(source_coverage)
    blockers: set[str] = set()

    if scope.capture_scope_id != manifest.capture_scope_id:
        blockers.add("binding.capture_scope_id_mismatch")
    if scope.content_sha256 != manifest.capture_scope_sha256:
        blockers.add("binding.capture_scope_sha256_mismatch")
    for id_field, hash_field in (
        ("research_catalog_id", "research_catalog_sha256"),
        ("applicability_catalog_id", "applicability_catalog_sha256"),
        ("source_coverage_catalog_id", "source_coverage_catalog_sha256"),
        ("slo_catalog_id", "slo_catalog_sha256"),
        ("source_registry_id", "source_registry_sha256"),
        ("semantic_type_registry_id", "semantic_type_registry_sha256"),
    ):
        if getattr(scope, id_field) != getattr(manifest, id_field):
            blockers.add(f"binding.{id_field}_mismatch")
        if getattr(scope, hash_field) != getattr(manifest, hash_field):
            blockers.add(f"binding.{hash_field}_mismatch")
    if scope.applicability_catalog_id != applicability_catalog_id:
        blockers.add("binding.applicability_input_id_mismatch")
    if scope.applicability_catalog_sha256 != applicability_catalog_sha256:
        blockers.add("binding.applicability_input_sha256_mismatch")
    if scope.applicability_projection_sha256 != applicability_projection_sha256:
        blockers.add("binding.applicability_projection_sha256_mismatch")
    if scope.source_coverage_projection_sha256 != source_coverage_projection_sha256:
        blockers.add("binding.source_coverage_projection_sha256_mismatch")
    if scope.effective_at > manifest.started_at:
        blockers.add("binding.capture_scope_postdates_run")
    if evaluated_at < manifest.created_at:
        blockers.add("evaluation.predates_manifest")

    requirements = scope.requirement_map()
    expected_keys = set(frozen_applicability)
    if not expected_keys:
        blockers.add("applicability.empty")
    mapped_requirement_ids = {key[-1] for key in expected_keys}
    for requirement_id in set(requirements) - mapped_requirement_ids:
        blockers.add(f"applicability.requirement_missing:{requirement_id}")
    for key, (_, effective_at) in frozen_applicability.items():
        requirement = requirements.get(key[-1])
        label = _key_label(key)
        if requirement is None:
            blockers.add(f"applicability.unknown_requirement:{label}")
            continue
        if requirement.domain is not key[2]:
            blockers.add(f"applicability.domain_mismatch:{label}")
        if key[0] not in requirement.subject_kinds:
            blockers.add(f"applicability.subject_kind_mismatch:{label}")
        if key[3] != manifest.partition_key:
            blockers.add(f"applicability.partition_mismatch:{label}")
        if effective_at > manifest.started_at:
            blockers.add(f"applicability.postdated:{label}")

    expected_source_keys = {
        (manifest.environment, *key)
        for key, (classification, _) in frozen_applicability.items()
        if classification != "not_applicable"
    }
    actual_source_keys = {key for key in frozen_source_coverage if key[0] is manifest.environment}
    for coverage_key in expected_source_keys - actual_source_keys:
        blockers.add(f"source_coverage.missing:{'/'.join(_source_coverage_sortable_key(coverage_key))}")
    for coverage_key in actual_source_keys - expected_source_keys:
        blockers.add(f"source_coverage.extra:{'/'.join(_source_coverage_sortable_key(coverage_key))}")

    cells_by_key: dict[CaptureCellKey, list[CaptureCell]] = defaultdict(list)
    for cell in manifest.cells:
        cells_by_key[cell.key].append(cell)
    actual_keys = set(cells_by_key)
    for key in expected_keys - actual_keys:
        blockers.add(f"cell.missing:{_key_label(key)}")
    for key in actual_keys - expected_keys:
        blockers.add(f"cell.extra:{_key_label(key)}")
    for key, cells in cells_by_key.items():
        if len(cells) > 1:
            blockers.add(f"cell.duplicate:{_key_label(key)}")

    for key in expected_keys & actual_keys:
        classification, _ = frozen_applicability[key]
        requirement = requirements.get(key[-1])
        if requirement is None:
            continue
        for cell in cells_by_key[key]:
            label = _key_label(key)
            if cell.applicability != classification:
                blockers.add(f"cell.applicability_mismatch:{label}")
            if classification == "not_applicable":
                if cell.status != "not_applicable":
                    blockers.add(f"cell.not_applicable_status_mismatch:{label}")
                if cell.evidence:
                    blockers.add(f"cell.not_applicable_has_evidence:{label}")
                if not cell.reason_codes:
                    blockers.add(f"cell.missing_reason_code:{label}")
                continue
            if classification == "required" and cell.status != "complete":
                blockers.add(f"cell.required_not_complete:{label}")
            if classification == "optional" and cell.status not in {"complete", "optional"}:
                blockers.add(f"cell.optional_status_invalid:{label}")
            if cell.status == "complete":
                if cell.reason_codes:
                    blockers.add(f"cell.complete_has_reason_code:{label}")
                source_key = (
                    manifest.environment,
                    cell.subject.kind,
                    cell.subject.id,
                    cell.domain,
                    cell.partition_key,
                    cell.capture_requirement_id,
                )
                blockers.update(
                    _evaluate_evidence(
                        cell=cell,
                        requirement=requirement,
                        manifest=manifest,
                        approved_coverage_entries=set(frozen_source_coverage.get(source_key, ())),
                    )
                )
            else:
                if cell.evidence:
                    blockers.add(f"cell.non_complete_has_evidence:{label}")
                if not cell.reason_codes:
                    blockers.add(f"cell.missing_reason_code:{label}")

    return CaptureEvaluationReport(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        capture_manifest_id=manifest.capture_manifest_id,
        capture_manifest_sha256=manifest.content_sha256,
        applicability_catalog_id=applicability_catalog_id,
        applicability_catalog_sha256=applicability_catalog_sha256,
        applicability_projection_sha256=applicability_projection_sha256,
        source_coverage_projection_sha256=source_coverage_projection_sha256,
        environment=manifest.environment,
        evaluated_at=evaluated_at,
        blocking_reason_codes=tuple(sorted(blockers)),
    )
