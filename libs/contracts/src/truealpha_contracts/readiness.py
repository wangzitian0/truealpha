"""Immutable source-readiness, applicability, and service-level contracts.

The declarations in this module are frozen before execution. Runtime reports
derive their result from those declarations and observed evidence; they do not
accept caller-supplied pass/fail fields or applicability overrides.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer, field_validator, model_validator

from truealpha_contracts.catalog import ResearchCatalogManifest
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.models import _require_aware
from truealpha_contracts.registries import RegistrySnapshot, RegistryVersion, SourceId
from truealpha_contracts.universe import SubjectRef, UniverseRef
from truealpha_contracts.usage import (
    DataRequirement,
    DataUsageEvent,
    UsageEmitterKind,
    UsageStage,
    planned_cell_id_for,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
SOURCE_ID_PATTERN = r"^[a-z][a-z0-9._:/-]*$"
SIGNATURE_ID_PATTERN = r"^[a-z][a-z0-9._:/-]*$"
REQUIRED_SOURCE_ENVIRONMENTS = frozenset(
    {
        CaptureEnvironment.LOCAL_DEV,
        CaptureEnvironment.LOCAL_TEST,
        CaptureEnvironment.GITHUB_CI,
        CaptureEnvironment.STAGING,
        CaptureEnvironment.PRODUCTION,
    }
)


def _expected_content_id(model: BaseModel, *, field: str, prefix: str) -> str:
    payload = model.model_dump(mode="json", exclude={field})
    return f"{prefix}:{canonical_sha256(payload)}"


def _content_id(model: BaseModel, *, field: str, prefix: str) -> str:
    expected = _expected_content_id(model, field=field, prefix=prefix)
    supplied = getattr(model, field)
    if supplied and supplied != expected:
        raise ValueError(f"{field} does not match canonical content")
    object.__setattr__(model, field, expected)
    return expected


def _content_sha256(content_id: str) -> str:
    return content_id.rsplit(":", 1)[-1]


def _aware_optional(value: datetime | None, field_name: str) -> datetime | None:
    return None if value is None else _require_aware(value, field_name)


class EvaluationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class SourceUsagePermission(StrEnum):
    RAW_RETENTION = "raw_retention"
    NORMALIZED_CACHING = "normalized_caching"
    DERIVED_METRICS = "derived_metrics"
    PUBLIC_REPORTS = "public_reports"
    PUBLIC_CARDS = "public_cards"
    QUOTATIONS = "quotations"
    SCREENSHOTS = "screenshots"
    ATTRIBUTION = "attribution"


class RightsDecisionBasis(StrEnum):
    AUTHORIZED_HUMAN = "authorized_human"
    LEGAL_COUNSEL = "legal_counsel"
    PROVIDER_LICENSE = "provider_license"


class PermissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    permission: SourceUsagePermission
    permitted: bool
    rationale: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()


class SourceRightsApproval(BaseModel):
    """Authoritative, expiring rights decision for one registry entry version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rights_approval_id: str = ""
    source_id: SourceId
    source_version: RegistryVersion
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=SHA256_PATTERN)
    authorized_owner: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    decision_basis: RightsDecisionBasis
    permission_decisions: tuple[PermissionDecision, ...] = Field(min_length=1)
    terms_evidence_id: str = Field(min_length=1)
    terms_evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    approved_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    @field_validator("approved_at", "expires_at", "revoked_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_optional(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> SourceRightsApproval:
        decisions = tuple(sorted(self.permission_decisions, key=lambda item: item.permission.value))
        permissions = [item.permission for item in decisions]
        if len(permissions) != len(set(permissions)):
            raise ValueError("permission decisions must be unique")
        if set(permissions) != set(SourceUsagePermission):
            missing = sorted(item.value for item in set(SourceUsagePermission) - set(permissions))
            raise ValueError(f"every usage permission requires an explicit decision; missing={missing}")
        if self.expires_at <= self.approved_at:
            raise ValueError("rights expiry must follow approval")
        if self.revoked_at is not None and self.revoked_at < self.approved_at:
            raise ValueError("revocation cannot precede approval")
        object.__setattr__(self, "permission_decisions", decisions)
        _content_id(self, field="rights_approval_id", prefix="source-rights")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.rights_approval_id)

    def permission_map(self) -> dict[SourceUsagePermission, PermissionDecision]:
        return {decision.permission: decision for decision in self.permission_decisions}


class SourceRole(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


class FallbackPolicy(StrEnum):
    REQUIRED = "required"
    DOCUMENTED_HARD_DEPENDENCY = "documented_hard_dependency"


class KnowabilityBasis(StrEnum):
    PUBLICATION_EVENT = "publication_event"
    INDEPENDENT_CORROBORATION = "independent_corroboration"
    PROVIDER_DISCLOSURE = "provider_disclosure"
    VENDOR_RETRIEVAL_OR_BACKFILL = "vendor_retrieval_or_backfill"


class KnowabilityEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    basis: KnowabilityBasis
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "observed_at")


class CoverageGap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)


class CoverageEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1)
    artifact_sha256: tuple[str, ...] = Field(min_length=1)
    observed_at: datetime
    observed_count: int = Field(ge=0)
    earliest_knowable_at: datetime | None = None
    latest_knowable_at: datetime | None = None
    natural_update_ids: tuple[str, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()

    @field_validator("artifact_sha256")
    @classmethod
    def validate_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in values):
            raise ValueError("artifact_sha256 values must be lowercase SHA-256")
        return tuple(sorted(set(values)))

    @field_validator("observed_at", "earliest_knowable_at", "latest_knowable_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_optional(value, info.field_name)

    @model_validator(mode="after")
    def validate_coverage(self) -> CoverageEvidence:
        if (self.earliest_knowable_at is None) != (self.latest_knowable_at is None):
            raise ValueError("coverage bounds must be supplied together")
        if self.observed_count and self.earliest_knowable_at is None:
            raise ValueError("non-empty coverage requires knowability bounds")
        if (
            self.earliest_knowable_at is not None
            and self.latest_knowable_at is not None
            and self.latest_knowable_at < self.earliest_knowable_at
        ):
            raise ValueError("latest knowable time cannot precede earliest")
        object.__setattr__(self, "natural_update_ids", tuple(sorted(set(self.natural_update_ids))))
        object.__setattr__(self, "gaps", tuple(sorted(self.gaps, key=lambda item: item.gap_id)))
        return self


class BudgetDimension(StrEnum):
    VENDOR_FEES = "vendor_fees"
    API_CALLS = "api_calls"
    OBJECT_STORAGE = "object_storage"
    DATABASE_STORAGE = "database_storage"
    MODEL_EXTRACTION = "model_extraction"
    HUMAN_REVIEW = "human_review"


class BudgetLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: BudgetDimension
    unit: str = Field(min_length=1)
    approved_monthly_limit: Decimal = Field(ge=0)
    approved_annual_limit: Decimal = Field(ge=0)
    projected_monthly_use: Decimal = Field(ge=0)
    projected_annual_use: Decimal = Field(ge=0)
    bounded_probe_use: Decimal = Field(ge=0)
    budget_approval_id: str = Field(min_length=1)
    budget_evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    owner: str = Field(min_length=1)


class SourceCoverageRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: CaptureEnvironment
    data_requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    semantic_type_id: str = Field(pattern=r"^semantic\.[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    semantic_type_version: RegistryVersion
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    required_permissions: frozenset[SourceUsagePermission] = Field(min_length=1)
    minimum_observed_count: int = Field(ge=1)
    history_start: datetime | None = None
    history_end: datetime | None = None
    minimum_natural_updates: int = Field(default=0, ge=0)
    requires_historical_knowability: bool = False
    fallback_policy: FallbackPolicy = FallbackPolicy.REQUIRED
    hard_dependency_reason: str | None = None

    @field_serializer("required_permissions", when_used="json")
    def serialize_required_permissions(self, values: frozenset[SourceUsagePermission]) -> list[str]:
        return sorted(value.value for value in values)

    @property
    def key(self) -> tuple[CaptureEnvironment, str, str, str, DataDomain, str]:
        return (
            self.environment,
            self.data_requirement_id,
            self.subject.kind.value,
            self.subject.id,
            self.domain,
            self.partition_key,
        )

    @field_validator("history_start", "history_end")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_optional(value, info.field_name)

    @model_validator(mode="after")
    def validate_requirement(self) -> SourceCoverageRequirement:
        if self.environment is CaptureEnvironment.LOCAL:
            raise ValueError("source coverage requires an explicit logical environment tier")
        if (self.history_start is None) != (self.history_end is None):
            raise ValueError("history bounds must be supplied together")
        if self.history_start is not None and self.history_end is not None and self.history_end < self.history_start:
            raise ValueError("history_end cannot precede history_start")
        if self.fallback_policy is FallbackPolicy.DOCUMENTED_HARD_DEPENDENCY:
            if not self.hard_dependency_reason:
                raise ValueError("a hard dependency requires a reason")
        elif self.hard_dependency_reason is not None:
            raise ValueError("hard_dependency_reason is only valid for a documented hard dependency")
        return self


class SourceCoverageEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_coverage_entry_id: str = ""
    environment: CaptureEnvironment
    data_requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    semantic_type_id: str = Field(pattern=r"^semantic\.[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    semantic_type_version: RegistryVersion
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    role: SourceRole
    priority: int = Field(ge=0)
    source_id: SourceId
    source_version: RegistryVersion
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=SHA256_PATTERN)
    rights_approval_id: str = Field(pattern=r"^source-rights:[0-9a-f]{64}$")
    rights_approval_sha256: str = Field(pattern=SHA256_PATTERN)
    identifier_level: str = Field(min_length=1)
    capture_method: str = Field(min_length=1)
    credential_owner: str = Field(min_length=1)
    cadence: timedelta
    review_expires_at: datetime
    knowability: KnowabilityEvidence
    coverage: CoverageEvidence
    budget_lines: tuple[BudgetLine, ...] = Field(min_length=1)

    @property
    def cell_key(self) -> tuple[CaptureEnvironment, str, str, str, DataDomain, str]:
        return (
            self.environment,
            self.data_requirement_id,
            self.subject.kind.value,
            self.subject.id,
            self.domain,
            self.partition_key,
        )

    @property
    def key(self) -> tuple[CaptureEnvironment, str, str, str, DataDomain, str, SourceRole, int]:
        return (*self.cell_key, self.role, self.priority)

    @field_validator("review_expires_at")
    @classmethod
    def validate_review_expiry(cls, value: datetime) -> datetime:
        return _require_aware(value, "review_expires_at")

    @model_validator(mode="after")
    def validate_entry(self) -> SourceCoverageEntry:
        if self.environment is CaptureEnvironment.LOCAL:
            raise ValueError("source coverage requires an explicit logical environment tier")
        if self.cadence <= timedelta(0):
            raise ValueError("source cadence must be positive")
        if self.role is SourceRole.PRIMARY and self.priority != 0:
            raise ValueError("primary source priority must be zero")
        if self.role is SourceRole.FALLBACK and self.priority == 0:
            raise ValueError("fallback source priority must be positive")
        dimensions = [line.dimension for line in self.budget_lines]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("budget dimensions must be unique per source entry")
        if self.rights_approval_sha256 != _content_sha256(self.rights_approval_id):
            raise ValueError("rights approval ID and hash do not match")
        object.__setattr__(
            self, "budget_lines", tuple(sorted(self.budget_lines, key=lambda item: item.dimension.value))
        )
        _content_id(self, field="source_coverage_entry_id", prefix="source-coverage-entry")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.source_coverage_entry_id)


class SourceCoverageCatalog(BaseModel):
    """Pre-run source matrix at environment, subject, domain, and partition grain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_coverage_catalog_id: str = ""
    catalog_version: str = Field(min_length=1)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    source_registry_id: str = Field(pattern=r"^source-registry:[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=SHA256_PATTERN)
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    requirements: tuple[SourceCoverageRequirement, ...] = Field(min_length=1)
    entries: tuple[SourceCoverageEntry, ...] = Field(min_length=1)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> SourceCoverageCatalog:
        if self.approved_at > self.effective_at:
            raise ValueError("source catalog approval must not postdate its effective time")
        if self.research_catalog_sha256 != _content_sha256(self.research_catalog_id):
            raise ValueError("research catalog ID and hash do not match")
        if self.applicability_catalog_sha256 != _content_sha256(self.applicability_catalog_id):
            raise ValueError("applicability catalog ID and hash do not match")
        requirements = tuple(sorted(self.requirements, key=lambda item: item.key))
        requirement_keys = [item.key for item in requirements]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("source coverage requirement cells must be unique")
        environments = {item.environment for item in requirements}
        missing_environments = REQUIRED_SOURCE_ENVIRONMENTS - environments
        if missing_environments:
            raise ValueError(
                "source coverage matrix omits required environments: "
                f"{sorted(item.value for item in missing_environments)}"
            )
        entries = tuple(sorted(self.entries, key=lambda item: item.key))
        entry_keys = [item.key for item in entries]
        if len(entry_keys) != len(set(entry_keys)):
            raise ValueError("source coverage entry roles and priorities must be unique per cell")
        cell_sources = [(item.cell_key, item.source_id, item.source_version) for item in entries]
        if len(cell_sources) != len(set(cell_sources)):
            raise ValueError("a source version may appear only once per coverage cell")
        object.__setattr__(self, "requirements", requirements)
        object.__setattr__(self, "entries", entries)
        _content_id(self, field="source_coverage_catalog_id", prefix="source-coverage")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.source_coverage_catalog_id)


class SourceReadinessReport(BaseModel):
    """A fail-closed report whose result has no caller-settable override."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: SourceCoverageCatalog
    registry_snapshot: RegistrySnapshot
    rights_approvals: tuple[SourceRightsApproval, ...]
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def normalize_approvals(self) -> SourceReadinessReport:
        approvals = tuple(sorted(self.rights_approvals, key=lambda item: item.rights_approval_id))
        ids = [item.rights_approval_id for item in approvals]
        if len(ids) != len(set(ids)):
            raise ValueError("rights approvals must be unique")
        object.__setattr__(self, "rights_approvals", approvals)
        return self

    def _blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if self.catalog.effective_at > self.evaluated_at or self.catalog.approved_at > self.evaluated_at:
            blockers.append("source coverage catalog was not effective and approved before evaluation")
        if (
            self.catalog.source_registry_id != self.registry_snapshot.source_registry_snapshot_id
            or self.catalog.source_registry_sha256 != self.registry_snapshot.source_registry_sha256
        ):
            blockers.append("source coverage catalog does not bind the supplied source registry snapshot")
        registry_entries = {(entry.source_id, entry.version): entry for entry in self.registry_snapshot.sources}
        registry_types = {
            (entry.semantic_type_id, entry.version): entry for entry in self.registry_snapshot.semantic_types
        }

        requirements = {item.key: item for item in self.catalog.requirements}
        entries_by_cell: dict[
            tuple[CaptureEnvironment, str, str, str, DataDomain, str],
            list[SourceCoverageEntry],
        ] = defaultdict(list)
        for entry in self.catalog.entries:
            entries_by_cell[entry.cell_key].append(entry)
            if entry.cell_key not in requirements:
                blockers.append(f"{entry.cell_key}: source entry is outside the required matrix")

        approvals = {item.rights_approval_id: item for item in self.rights_approvals}
        for key, requirement in requirements.items():
            entries = entries_by_cell.get(key, [])
            primaries = [item for item in entries if item.role is SourceRole.PRIMARY]
            fallbacks = [item for item in entries if item.role is SourceRole.FALLBACK]
            if len(primaries) != 1:
                blockers.append(f"{key}: requires exactly one primary source")
            if requirement.fallback_policy is FallbackPolicy.REQUIRED and not fallbacks:
                blockers.append(f"{key}: required fallback source is missing")
            for entry in entries:
                prefix = f"{key}/{entry.source_id}@{entry.source_version}"
                if (
                    entry.semantic_type_id != requirement.semantic_type_id
                    or entry.semantic_type_version != requirement.semantic_type_version
                ):
                    blockers.append(f"{prefix}: source entry semantic type does not match the requirement")
                registry_entry = registry_entries.get((entry.source_id, entry.source_version))
                if registry_entry is None:
                    blockers.append(f"{prefix}: source registry entry is missing")
                elif (
                    entry.source_registry_entry_id != registry_entry.source_registry_entry_id
                    or entry.source_registry_entry_sha256 != registry_entry.content_sha256
                ):
                    blockers.append(f"{prefix}: source registry entry identity/hash mismatch")
                elif (
                    entry.semantic_type_id not in registry_entry.supported_type_ids
                    or entry.domain not in registry_entry.supported_domains
                ):
                    blockers.append(f"{prefix}: source registry entry does not support the required type/domain")
                semantic_entry = registry_types.get((entry.semantic_type_id, entry.semantic_type_version))
                if semantic_entry is None or semantic_entry.domain is not entry.domain:
                    blockers.append(f"{prefix}: semantic type/domain is absent from the registry snapshot")
                if entry.review_expires_at <= self.evaluated_at:
                    blockers.append(f"{prefix}: source review expired")
                if entry.coverage.gaps:
                    blockers.append(f"{prefix}: longitudinal coverage contains gaps")
                if entry.coverage.observed_count < requirement.minimum_observed_count:
                    blockers.append(f"{prefix}: observed coverage is below the frozen minimum")
                if len(entry.coverage.natural_update_ids) < requirement.minimum_natural_updates:
                    blockers.append(f"{prefix}: natural update evidence is below the frozen minimum")
                if requirement.history_start is not None and (
                    entry.coverage.earliest_knowable_at is None
                    or entry.coverage.earliest_knowable_at > requirement.history_start
                ):
                    blockers.append(f"{prefix}: required history start is not covered")
                if requirement.history_end is not None and (
                    entry.coverage.latest_knowable_at is None
                    or entry.coverage.latest_knowable_at < requirement.history_end
                ):
                    blockers.append(f"{prefix}: required history end is not covered")
                if (
                    requirement.requires_historical_knowability
                    and entry.knowability.basis is KnowabilityBasis.VENDOR_RETRIEVAL_OR_BACKFILL
                ):
                    blockers.append(f"{prefix}: vendor retrieval/backfill does not prove historical knowability")
                for line in entry.budget_lines:
                    if (
                        line.projected_monthly_use > line.approved_monthly_limit
                        or line.projected_annual_use > line.approved_annual_limit
                        or line.bounded_probe_use > line.approved_monthly_limit
                    ):
                        blockers.append(f"{prefix}/{line.dimension.value}: approved budget is insufficient")

                approval = approvals.get(entry.rights_approval_id)
                if approval is None:
                    blockers.append(f"{prefix}: rights approval is missing")
                    continue
                if approval.content_sha256 != entry.rights_approval_sha256:
                    blockers.append(f"{prefix}: rights approval hash mismatch")
                if (
                    approval.source_id != entry.source_id
                    or approval.source_version != entry.source_version
                    or approval.source_registry_entry_id != entry.source_registry_entry_id
                    or approval.source_registry_entry_sha256 != entry.source_registry_entry_sha256
                ):
                    blockers.append(f"{prefix}: rights approval does not bind the exact registry entry version")
                if approval.expires_at <= self.evaluated_at:
                    blockers.append(f"{prefix}: rights approval expired")
                if approval.revoked_at is not None and approval.revoked_at <= self.evaluated_at:
                    blockers.append(f"{prefix}: rights approval was revoked")
                decisions = approval.permission_map()
                denied = [
                    permission.value
                    for permission in requirement.required_permissions
                    if not decisions[permission].permitted
                ]
                if denied:
                    blockers.append(f"{prefix}: required permissions denied: {sorted(denied)}")
        return tuple(sorted(set(blockers)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> EvaluationStatus:
        return EvaluationStatus.FAIL if self._blockers() else EvaluationStatus.PASS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_readiness_report_id(self) -> str:
        payload = {
            "catalog_id": self.catalog.source_coverage_catalog_id,
            "registry_snapshot_id": self.registry_snapshot.registry_snapshot_id,
            "approval_ids": [item.rights_approval_id for item in self.rights_approvals],
            "evaluated_at": self.evaluated_at.isoformat(),
            "blockers": self._blockers(),
        }
        return f"source-readiness:{canonical_sha256(payload)}"


class ApplicabilityClassification(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class ApplicabilityCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    module_id: str = Field(min_length=1)
    catalog_alias: str = Field(min_length=1)
    data_requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    classification: ApplicabilityClassification
    reason: str = Field(min_length=1)
    effective_at: datetime

    @property
    def key(self) -> tuple[str, str, str, str, str, DataDomain, str]:
        return (
            self.module_id,
            self.catalog_alias,
            self.data_requirement_id,
            self.subject.kind.value,
            self.subject.id,
            self.domain,
            self.partition_key,
        )

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")


class ApplicabilityPolicy(BaseModel):
    """Pre-Catalog applicability policy with no Research Catalog back-reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    applicability_policy_id: str = ""
    policy_version: str = Field(min_length=1)
    module_id: str = Field(min_length=1)
    catalog_alias: str = Field(min_length=1)
    universe: UniverseRef
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    cells: tuple[ApplicabilityCell, ...] = Field(min_length=1)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ApplicabilityPolicy:
        if self.approved_at > self.effective_at:
            raise ValueError("applicability policy approval must not postdate its effective time")
        cells = tuple(sorted(self.cells, key=lambda item: item.key))
        keys = [item.key for item in cells]
        if len(keys) != len(set(keys)):
            raise ValueError("applicability policy cells must be unique")
        if any(cell.module_id != self.module_id or cell.catalog_alias != self.catalog_alias for cell in cells):
            raise ValueError("applicability policy cells must match its module and Catalog alias")
        if any(cell.effective_at > self.effective_at for cell in cells):
            raise ValueError("applicability policy cannot predate one of its cells")
        object.__setattr__(self, "cells", cells)
        _content_id(self, field="applicability_policy_id", prefix="applicability-policy")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.applicability_policy_id)


class ApplicabilityCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applicability_catalog_id: str = ""
    catalog_version: str = Field(min_length=1)
    research_catalog_id: str = Field(min_length=1)
    research_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    universe: UniverseRef
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    cells: tuple[ApplicabilityCell, ...] = Field(min_length=1)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ApplicabilityCatalog:
        if self.approved_at > self.effective_at:
            raise ValueError("applicability approval must not postdate its effective time")
        cells = tuple(sorted(self.cells, key=lambda item: item.key))
        keys = [item.key for item in cells]
        if len(keys) != len(set(keys)):
            raise ValueError("applicability cells must be unique")
        object.__setattr__(self, "cells", cells)
        _content_id(self, field="applicability_catalog_id", prefix="applicability")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.applicability_catalog_id)

    def cell_map(self) -> dict[tuple[str, str, str, str, str, DataDomain, str], ApplicabilityCell]:
        return {cell.key: cell for cell in self.cells}


class SourceCoverageClosureReport(BaseModel):
    """Mechanical proof that source coverage closes the accepted catalog demand."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_coverage_closure_report_id: str = ""
    source_coverage_catalog_id: str = Field(pattern=r"^source-coverage:[0-9a-f]{64}$")
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    evaluated_at: datetime
    blocking_reason_codes: tuple[str, ...]

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def normalize_and_identify(self) -> SourceCoverageClosureReport:
        object.__setattr__(self, "blocking_reason_codes", tuple(sorted(set(self.blocking_reason_codes))))
        _content_id(
            self,
            field="source_coverage_closure_report_id",
            prefix="source-coverage-closure",
        )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self.blocking_reason_codes


def evaluate_source_coverage_closure(
    source_catalog: SourceCoverageCatalog,
    research_catalog: ResearchCatalogManifest,
    applicability_catalog: ApplicabilityCatalog,
    data_requirements: tuple[DataRequirement, ...],
    *,
    evaluated_at: datetime,
) -> SourceCoverageClosureReport:
    """Join catalog templates, applicability, and the five-environment source matrix."""

    evaluated_at = _require_aware(evaluated_at, "evaluated_at")
    blockers: set[str] = set()
    if applicability_catalog.applicability_catalog_id != _expected_content_id(
        applicability_catalog,
        field="applicability_catalog_id",
        prefix="applicability",
    ):
        blockers.add("binding.applicability_catalog_content_drift")
    if (
        source_catalog.research_catalog_id != research_catalog.research_catalog_id
        or source_catalog.research_catalog_sha256 != research_catalog.content_sha256
    ):
        blockers.add("binding.research_catalog_mismatch")
    if source_catalog.universe != research_catalog.scope_floor.universe:
        blockers.add("binding.research_universe_mismatch")
    if (
        source_catalog.applicability_catalog_id != applicability_catalog.applicability_catalog_id
        or source_catalog.applicability_catalog_sha256 != applicability_catalog.content_sha256
    ):
        blockers.add("binding.applicability_catalog_mismatch")
    if applicability_catalog.research_catalog_id != research_catalog.research_catalog_id:
        blockers.add("binding.applicability_research_catalog_mismatch")
    if applicability_catalog.universe != research_catalog.scope_floor.universe:
        blockers.add("binding.applicability_universe_mismatch")

    requirements_by_id = {item.requirement_id: item for item in data_requirements}
    if len(requirements_by_id) != len(data_requirements):
        blockers.add("demand.duplicate_data_requirement")
    entries_by_alias = {item.catalog_alias: item for item in research_catalog.entries}
    declared_ids = {
        requirement_id
        for entry in research_catalog.entries
        for requirement_id in entry.invocation_template.factor_template.data_requirement_ids
    }
    unknown_declared = declared_ids - set(requirements_by_id)
    if unknown_declared:
        blockers.add("demand.catalog_references_unknown_data_requirement")
    unused_supplied = set(requirements_by_id) - declared_ids
    if unused_supplied:
        blockers.add("demand.unreferenced_data_requirement_supplied")

    expected: set[tuple[CaptureEnvironment, str, str, str, DataDomain, str]] = set()
    for cell in applicability_catalog.cells:
        entry = entries_by_alias.get(cell.catalog_alias)
        if entry is None:
            blockers.add(f"demand.unknown_catalog_alias:{cell.catalog_alias}")
            continue
        if cell.data_requirement_id not in entry.invocation_template.factor_template.data_requirement_ids:
            blockers.add(f"demand.alias_does_not_declare_requirement:{cell.catalog_alias}")
        if cell.subject not in entry.subject_scope:
            blockers.add(f"demand.subject_outside_catalog_entry:{cell.catalog_alias}/{cell.subject.id}")
        requirement = requirements_by_id.get(cell.data_requirement_id)
        if requirement is None:
            blockers.add(f"demand.missing_requirement_contract:{cell.data_requirement_id}")
            continue
        if requirement.domain is not cell.domain or cell.subject.kind not in requirement.subject_kinds:
            blockers.add(f"demand.requirement_semantics_mismatch:{cell.data_requirement_id}")
        if cell.classification is ApplicabilityClassification.NOT_APPLICABLE:
            continue
        expected.update(
            (
                environment,
                cell.data_requirement_id,
                cell.subject.kind.value,
                cell.subject.id,
                cell.domain,
                cell.partition_key,
            )
            for environment in REQUIRED_SOURCE_ENVIRONMENTS
        )

    actual = {item.key for item in source_catalog.requirements}
    for key in sorted(expected - actual, key=lambda value: tuple(str(item) for item in value)):
        blockers.add(f"matrix.missing:{'/'.join(str(item) for item in key)}")
    for key in sorted(actual - expected, key=lambda value: tuple(str(item) for item in value)):
        blockers.add(f"matrix.extra:{'/'.join(str(item) for item in key)}")
    for source_requirement in source_catalog.requirements:
        requirement = requirements_by_id.get(source_requirement.data_requirement_id)
        if requirement is None:
            continue
        if (
            source_requirement.semantic_type_id != requirement.semantic_type_id
            or source_requirement.domain is not requirement.domain
            or source_requirement.subject.kind not in requirement.subject_kinds
        ):
            blockers.add(f"matrix.requirement_semantics_mismatch:{source_requirement.data_requirement_id}")

    return SourceCoverageClosureReport(
        source_coverage_catalog_id=source_catalog.source_coverage_catalog_id,
        research_catalog_id=research_catalog.research_catalog_id,
        applicability_catalog_id=applicability_catalog.applicability_catalog_id,
        evaluated_at=evaluated_at,
        blocking_reason_codes=tuple(sorted(blockers)),
    )


class ModuleOutcome(StrEnum):
    USABLE = "usable"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    UNRESOLVED = "unresolved"
    UNCLASSIFIED = "unclassified"
    LOW_CONFIDENCE = "low_confidence"


class ModuleSloThreshold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slo_policy_id: str = ""
    module_id: str = Field(min_length=1)
    minimum_subject_count: int = Field(ge=1)
    minimum_usable_coverage: Decimal = Field(ge=0, le=1)
    maximum_unavailable_ratio: Decimal = Field(ge=0, le=1)
    maximum_stale_ratio: Decimal = Field(ge=0, le=1)
    maximum_unresolved_ratio: Decimal = Field(ge=0, le=1)
    maximum_unclassified_ratio: Decimal = Field(ge=0, le=1)
    maximum_low_confidence_ratio: Decimal = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ModuleSloThreshold:
        _content_id(self, field="slo_policy_id", prefix="slo-policy")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.slo_policy_id)


class ModuleSloCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    module_slo_catalog_id: str = ""
    catalog_version: str = Field(min_length=1)
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    thresholds: tuple[ModuleSloThreshold, ...] = Field(min_length=1)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ModuleSloCatalog:
        if self.approved_at > self.effective_at:
            raise ValueError("module SLO approval must not postdate its effective time")
        if self.applicability_catalog_sha256 != _content_sha256(self.applicability_catalog_id):
            raise ValueError("applicability catalog ID and hash do not match")
        thresholds = tuple(sorted(self.thresholds, key=lambda item: item.module_id))
        modules = [item.module_id for item in thresholds]
        if len(modules) != len(set(modules)):
            raise ValueError("module SLO thresholds must be unique")
        object.__setattr__(self, "thresholds", thresholds)
        _content_id(self, field="module_slo_catalog_id", prefix="module-slo")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.module_slo_catalog_id)


class CatalogPolicyClosureReport(BaseModel):
    """Derived proof that Catalog policy refs close over concrete policy catalogs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_policy_closure_report_id: str = ""
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    module_slo_catalog_id: str = Field(pattern=r"^module-slo:[0-9a-f]{64}$")
    applicability_policy_ids: tuple[str, ...]
    slo_policy_ids: tuple[str, ...]
    evaluated_at: datetime
    blocking_reason_codes: tuple[str, ...]

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def normalize_and_identify(self) -> CatalogPolicyClosureReport:
        object.__setattr__(self, "applicability_policy_ids", tuple(sorted(self.applicability_policy_ids)))
        object.__setattr__(self, "slo_policy_ids", tuple(sorted(self.slo_policy_ids)))
        object.__setattr__(self, "blocking_reason_codes", tuple(sorted(set(self.blocking_reason_codes))))
        _content_id(
            self,
            field="catalog_policy_closure_report_id",
            prefix="catalog-policy-closure",
        )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self.blocking_reason_codes


def evaluate_catalog_policy_closure(
    research_catalog: ResearchCatalogManifest,
    applicability_policies: tuple[ApplicabilityPolicy, ...],
    applicability_catalog: ApplicabilityCatalog,
    module_slo_catalog: ModuleSloCatalog,
    *,
    evaluated_at: datetime,
) -> CatalogPolicyClosureReport:
    """Resolve every Catalog policy ref without introducing a reverse hash edge."""

    evaluated_at = _require_aware(evaluated_at, "evaluated_at")
    blockers: set[str] = set()
    if applicability_catalog.applicability_catalog_id != _expected_content_id(
        applicability_catalog,
        field="applicability_catalog_id",
        prefix="applicability",
    ):
        blockers.add("binding.applicability_catalog_content_drift")
    if module_slo_catalog.module_slo_catalog_id != _expected_content_id(
        module_slo_catalog,
        field="module_slo_catalog_id",
        prefix="module-slo",
    ):
        blockers.add("binding.module_slo_catalog_content_drift")
    if (
        applicability_catalog.research_catalog_id != research_catalog.research_catalog_id
        or applicability_catalog.research_catalog_sha256 != research_catalog.content_sha256
    ):
        blockers.add("binding.applicability_research_catalog_mismatch")
    if applicability_catalog.universe != research_catalog.scope_floor.universe:
        blockers.add("binding.applicability_universe_mismatch")
    if (
        module_slo_catalog.applicability_catalog_id != applicability_catalog.applicability_catalog_id
        or module_slo_catalog.applicability_catalog_sha256 != applicability_catalog.content_sha256
    ):
        blockers.add("binding.module_slo_applicability_mismatch")
    if any(
        value > evaluated_at
        for value in (
            research_catalog.effective_at,
            applicability_catalog.effective_at,
            applicability_catalog.approved_at,
            module_slo_catalog.effective_at,
            module_slo_catalog.approved_at,
        )
    ):
        blockers.add("time.catalog_or_projection_postdates_evaluation")

    policy_by_id: dict[str, ApplicabilityPolicy] = {}
    for policy in applicability_policies:
        if policy.applicability_policy_id in policy_by_id:
            blockers.add(f"applicability.duplicate_policy:{policy.applicability_policy_id}")
        else:
            policy_by_id[policy.applicability_policy_id] = policy
        expected_id = _expected_content_id(
            policy,
            field="applicability_policy_id",
            prefix="applicability-policy",
        )
        if policy.applicability_policy_id != expected_id:
            blockers.add(f"applicability.policy_content_drift:{policy.applicability_policy_id}")

    thresholds = module_slo_catalog.thresholds
    threshold_by_id = {threshold.slo_policy_id: threshold for threshold in thresholds}
    if len(threshold_by_id) != len(thresholds):
        blockers.add("slo.duplicate_policy")
    for threshold in thresholds:
        expected_id = _expected_content_id(threshold, field="slo_policy_id", prefix="slo-policy")
        if threshold.slo_policy_id != expected_id:
            blockers.add(f"slo.policy_content_drift:{threshold.slo_policy_id}")

    referenced_applicability_ids: set[str] = set()
    referenced_slo_ids: set[str] = set()
    for entry in research_catalog.entries:
        referenced_applicability_ids.add(entry.applicability_policy_id)
        referenced_slo_ids.add(entry.slo_policy_id)
        resolved_policy = policy_by_id.get(entry.applicability_policy_id)
        if resolved_policy is None:
            blockers.add(f"applicability.missing_policy:{entry.catalog_alias}")
        else:
            expected_id = _expected_content_id(
                resolved_policy,
                field="applicability_policy_id",
                prefix="applicability-policy",
            )
            if entry.applicability_policy_id != expected_id or entry.applicability_policy_sha256 != _content_sha256(
                expected_id
            ):
                blockers.add(f"applicability.policy_ref_drift:{entry.catalog_alias}")
            if resolved_policy.catalog_alias != entry.catalog_alias or resolved_policy.universe != entry.universe:
                blockers.add(f"applicability.policy_scope_mismatch:{entry.catalog_alias}")
            if any(cell.subject not in entry.subject_scope for cell in resolved_policy.cells):
                blockers.add(f"applicability.policy_subject_outside_entry:{entry.catalog_alias}")
            declared_requirements = set(entry.invocation_template.factor_template.data_requirement_ids)
            if any(cell.data_requirement_id not in declared_requirements for cell in resolved_policy.cells):
                blockers.add(f"applicability.policy_requirement_outside_entry:{entry.catalog_alias}")
            if (
                resolved_policy.approved_at > entry.approved_at
                or resolved_policy.effective_at > research_catalog.effective_at
                or resolved_policy.effective_at > evaluated_at
            ):
                blockers.add(f"time.applicability_policy_postdated:{entry.catalog_alias}")

        resolved_threshold = threshold_by_id.get(entry.slo_policy_id)
        if resolved_threshold is None:
            blockers.add(f"slo.missing_policy:{entry.catalog_alias}")
        else:
            expected_id = _expected_content_id(
                resolved_threshold,
                field="slo_policy_id",
                prefix="slo-policy",
            )
            if entry.slo_policy_id != expected_id or entry.slo_policy_sha256 != _content_sha256(expected_id):
                blockers.add(f"slo.policy_ref_drift:{entry.catalog_alias}")
            if resolved_policy is not None and resolved_threshold.module_id != resolved_policy.module_id:
                blockers.add(f"slo.policy_module_mismatch:{entry.catalog_alias}")
            if resolved_threshold.approved_at > entry.approved_at or resolved_threshold.approved_at > evaluated_at:
                blockers.add(f"time.slo_policy_postdated:{entry.catalog_alias}")

    for policy_id in set(policy_by_id) - referenced_applicability_ids:
        blockers.add(f"applicability.extra_policy:{policy_id}")
    for policy_id in referenced_applicability_ids - set(policy_by_id):
        blockers.add(f"applicability.unresolved_policy_ref:{policy_id}")
    for policy_id in set(threshold_by_id) - referenced_slo_ids:
        blockers.add(f"slo.extra_policy:{policy_id}")
    for policy_id in referenced_slo_ids - set(threshold_by_id):
        blockers.add(f"slo.unresolved_policy_ref:{policy_id}")

    projected_cells: dict[tuple[str, str, str, str, str, DataDomain, str], ApplicabilityCell] = {}
    for policy in applicability_policies:
        for cell in policy.cells:
            previous = projected_cells.setdefault(cell.key, cell)
            if previous != cell:
                blockers.add(f"applicability.conflicting_policy_cell:{'/'.join(map(str, cell.key))}")
            elif previous is not cell:
                blockers.add(f"applicability.duplicate_policy_cell:{'/'.join(map(str, cell.key))}")
    concrete_cells = applicability_catalog.cell_map()
    for key in concrete_cells.keys() - projected_cells.keys():
        blockers.add(f"applicability.missing_policy_cell:{'/'.join(map(str, key))}")
    for key in projected_cells.keys() - concrete_cells.keys():
        blockers.add(f"applicability.extra_policy_cell:{'/'.join(map(str, key))}")
    for key in concrete_cells.keys() & projected_cells.keys():
        if concrete_cells[key] != projected_cells[key]:
            blockers.add(f"applicability.policy_cell_drift:{'/'.join(map(str, key))}")

    return CatalogPolicyClosureReport(
        research_catalog_id=research_catalog.research_catalog_id,
        applicability_catalog_id=applicability_catalog.applicability_catalog_id,
        module_slo_catalog_id=module_slo_catalog.module_slo_catalog_id,
        applicability_policy_ids=tuple(policy.applicability_policy_id for policy in applicability_policies),
        slo_policy_ids=tuple(threshold.slo_policy_id for threshold in thresholds),
        evaluated_at=evaluated_at,
        blocking_reason_codes=tuple(sorted(blockers)),
    )


class ModuleSloObservation(BaseModel):
    """Observed module result; applicability is intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module_id: str = Field(min_length=1)
    catalog_alias: str = Field(min_length=1)
    data_requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    outcome: ModuleOutcome
    observed_at: datetime
    output_id: str | None = None
    trace_complete: bool = False

    @property
    def key(self) -> tuple[str, str, str, str, str, DataDomain, str]:
        return (
            self.module_id,
            self.catalog_alias,
            self.data_requirement_id,
            self.subject.kind.value,
            self.subject.id,
            self.domain,
            self.partition_key,
        )

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "observed_at")


class ModuleSloReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applicability: ApplicabilityCatalog
    slo_catalog: ModuleSloCatalog
    run_started_at: datetime
    evaluated_at: datetime
    observations: tuple[ModuleSloObservation, ...]

    @field_validator("run_started_at", "evaluated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    def _blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if self.evaluated_at < self.run_started_at:
            blockers.append("module SLO evaluation precedes the run")
        if (
            self.slo_catalog.applicability_catalog_id != self.applicability.applicability_catalog_id
            or self.slo_catalog.applicability_catalog_sha256 != self.applicability.content_sha256
        ):
            blockers.append("module SLO catalog binds a different applicability catalog")
        if (
            self.applicability.approved_at > self.run_started_at
            or self.applicability.effective_at > self.run_started_at
            or self.slo_catalog.approved_at > self.run_started_at
            or self.slo_catalog.effective_at > self.run_started_at
        ):
            blockers.append("applicability and SLO catalogs must be approved and effective before the run")
        if any(cell.effective_at > self.run_started_at for cell in self.applicability.cells):
            blockers.append("postdated applicability cell cannot classify this run")
        if any(threshold.approved_at > self.run_started_at for threshold in self.slo_catalog.thresholds):
            blockers.append("module threshold approval postdates the run")

        expected = {
            cell.key: cell
            for cell in self.applicability.cells
            if cell.classification is not ApplicabilityClassification.NOT_APPLICABLE
        }
        observed_by_key: dict[
            tuple[str, str, str, str, str, DataDomain, str],
            list[ModuleSloObservation],
        ] = defaultdict(list)
        for observation in self.observations:
            observed_by_key[observation.key].append(observation)
        for key in sorted(set(expected) - set(observed_by_key)):
            blockers.append(f"{key}: required SLO observation is missing")
        for key in sorted(set(observed_by_key) - set(expected)):
            blockers.append(f"{key}: observation is outside applicable scope")
        for key, items in observed_by_key.items():
            if len(items) != 1:
                blockers.append(f"{key}: duplicate SLO observations")
        thresholds = {item.module_id: item for item in self.slo_catalog.thresholds}
        expected_modules = {key[0] for key in expected}
        for module_id in sorted(expected_modules - set(thresholds)):
            blockers.append(f"{module_id}: module threshold is missing")

        for module_id in sorted(expected_modules & set(thresholds)):
            keys = [key for key in expected if key[0] == module_id]
            observations = [observed_by_key[key][0] for key in keys if len(observed_by_key.get(key, [])) == 1]
            threshold = thresholds[module_id]
            subjects = {(key[3], key[4]) for key in keys}
            if len(subjects) < threshold.minimum_subject_count:
                blockers.append(f"{module_id}: applicable subject denominator is below the frozen minimum")
            if len(observations) != len(keys):
                continue
            total = Decimal(len(keys))
            counts = {outcome: sum(item.outcome is outcome for item in observations) for outcome in ModuleOutcome}
            usable_ratio = Decimal(counts[ModuleOutcome.USABLE]) / total
            limits = {
                ModuleOutcome.UNAVAILABLE: threshold.maximum_unavailable_ratio,
                ModuleOutcome.STALE: threshold.maximum_stale_ratio,
                ModuleOutcome.UNRESOLVED: threshold.maximum_unresolved_ratio,
                ModuleOutcome.UNCLASSIFIED: threshold.maximum_unclassified_ratio,
                ModuleOutcome.LOW_CONFIDENCE: threshold.maximum_low_confidence_ratio,
            }
            if usable_ratio < threshold.minimum_usable_coverage:
                blockers.append(f"{module_id}: usable coverage is below the frozen minimum")
            for outcome, maximum in limits.items():
                if Decimal(counts[outcome]) / total > maximum:
                    blockers.append(f"{module_id}: {outcome.value} ratio exceeds the frozen maximum")
            for observation in observations:
                cell = expected[observation.key]
                if observation.observed_at < self.run_started_at or observation.observed_at > self.evaluated_at:
                    blockers.append(f"{observation.key}: observation is outside the run window")
                if observation.outcome is ModuleOutcome.USABLE and (
                    not observation.trace_complete or observation.output_id is None
                ):
                    blockers.append(f"{observation.key}: usable output lacks complete trace evidence")
                if (
                    cell.classification is ApplicabilityClassification.REQUIRED
                    and observation.outcome is not ModuleOutcome.USABLE
                ):
                    blockers.append(f"{observation.key}: required core cell is not usable")
        return tuple(sorted(set(blockers)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> EvaluationStatus:
        return EvaluationStatus.FAIL if self._blockers() else EvaluationStatus.PASS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def module_slo_report_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"blockers", "status", "module_slo_report_id"})
        return f"module-slo-report:{canonical_sha256(payload)}"


class ConsumerSloRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_id: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)
    minimum_availability: Decimal = Field(ge=0, le=1)
    maximum_latency_ms: int = Field(gt=0)
    maximum_row_count: int = Field(gt=0)
    require_authenticated: bool
    require_trace_complete: bool
    maximum_permission_failure_ratio: Decimal = Field(ge=0, le=1)
    error_budget_ratio: Decimal = Field(ge=0, le=1)
    owner: str = Field(min_length=1)
    remediation_runbook: str = Field(min_length=1)

    @property
    def key(self) -> tuple[str, str]:
        return self.consumer_id, self.endpoint_id


class ConsumerSloCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_slo_catalog_id: str = ""
    catalog_version: str = Field(min_length=1)
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    requirements: tuple[ConsumerSloRequirement, ...] = Field(min_length=1)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ConsumerSloCatalog:
        if self.approved_at > self.effective_at:
            raise ValueError("consumer SLO approval must not postdate its effective time")
        if self.applicability_catalog_sha256 != _content_sha256(self.applicability_catalog_id):
            raise ValueError("applicability catalog ID and hash do not match")
        requirements = tuple(sorted(self.requirements, key=lambda item: item.key))
        keys = [item.key for item in requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("consumer SLO requirements must be unique")
        object.__setattr__(self, "requirements", requirements)
        _content_id(self, field="consumer_slo_catalog_id", prefix="consumer-slo")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.consumer_slo_catalog_id)


class ConsumerSloObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_id: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)
    window_started_at: datetime
    window_completed_at: datetime
    request_count: int = Field(gt=0)
    successful_request_count: int = Field(ge=0)
    authenticated_request_count: int = Field(ge=0)
    trace_complete_count: int = Field(ge=0)
    permission_failure_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    latency_p95_ms: int = Field(ge=0)
    largest_row_count: int = Field(ge=0)

    @property
    def key(self) -> tuple[str, str]:
        return self.consumer_id, self.endpoint_id

    @field_validator("window_started_at", "window_completed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_counts(self) -> ConsumerSloObservation:
        if self.window_completed_at < self.window_started_at:
            raise ValueError("consumer observation window is inverted")
        for value in (
            self.successful_request_count,
            self.authenticated_request_count,
            self.trace_complete_count,
            self.permission_failure_count,
            self.error_count,
        ):
            if value > self.request_count:
                raise ValueError("consumer event counts cannot exceed request_count")
        return self


class ConsumerSloReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: ConsumerSloCatalog
    evaluated_at: datetime
    observations: tuple[ConsumerSloObservation, ...]

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    def _blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        requirements = {item.key: item for item in self.catalog.requirements}
        observed: dict[tuple[str, str], list[ConsumerSloObservation]] = defaultdict(list)
        for item in self.observations:
            observed[item.key].append(item)
        for key in sorted(set(requirements) - set(observed)):
            blockers.append(f"{key}: consumer SLO observation is missing")
        for key in sorted(set(observed) - set(requirements)):
            blockers.append(f"{key}: consumer observation is outside the catalog")
        for key, items in observed.items():
            if len(items) != 1:
                blockers.append(f"{key}: duplicate consumer SLO observations")
                continue
            requirement = requirements.get(key)
            if requirement is None:
                continue
            item = items[0]
            total = Decimal(item.request_count)
            if item.window_started_at < self.catalog.effective_at or item.window_completed_at > self.evaluated_at:
                blockers.append(f"{key}: consumer observation is outside the approved evaluation window")
            if Decimal(item.successful_request_count) / total < requirement.minimum_availability:
                blockers.append(f"{key}: authenticated availability is below SLO")
            if requirement.require_authenticated and item.authenticated_request_count != item.request_count:
                blockers.append(f"{key}: unauthenticated requests cannot satisfy the consumer SLO")
            if requirement.require_trace_complete and item.trace_complete_count != item.successful_request_count:
                blockers.append(f"{key}: successful responses lack complete traces")
            if item.latency_p95_ms > requirement.maximum_latency_ms:
                blockers.append(f"{key}: latency exceeds SLO")
            if item.largest_row_count > requirement.maximum_row_count:
                blockers.append(f"{key}: row limit exceeds SLO")
            if Decimal(item.permission_failure_count) / total > requirement.maximum_permission_failure_ratio:
                blockers.append(f"{key}: permission failure ratio exceeds SLO")
            if Decimal(item.error_count) / total > requirement.error_budget_ratio:
                blockers.append(f"{key}: error budget is exhausted")
        return tuple(sorted(set(blockers)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> EvaluationStatus:
        return EvaluationStatus.FAIL if self._blockers() else EvaluationStatus.PASS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def consumer_slo_report_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"blockers", "status", "consumer_slo_report_id"})
        return f"consumer-slo-report:{canonical_sha256(payload)}"


class UsageTelemetryRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    telemetry_requirement_id: str = ""
    planned_cell_id: str = Field(default="", pattern=r"^(?:|planned-demand-cell:[0-9a-f]{64})$")
    data_requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    semantic_type_id: str = Field(pattern=r"^semantic\.[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    emitter_kind: UsageEmitterKind
    emitter_id: str = Field(min_length=1)
    stage: UsageStage
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    expected_window_start: datetime
    expected_window_end: datetime
    expected_minimum_events: int = Field(ge=1)
    expected_maximum_events: int = Field(ge=1)
    maximum_lag: timedelta
    minimum_retention: timedelta
    maximum_reconciliation_difference: int = Field(default=0, ge=0)
    demand_evidence_id: str = Field(min_length=1)
    demand_evidence_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("expected_window_start", "expected_window_end")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_requirement(self) -> UsageTelemetryRequirement:
        if self.expected_window_end <= self.expected_window_start:
            raise ValueError("usage telemetry window must be positive")
        if self.expected_maximum_events < self.expected_minimum_events:
            raise ValueError("expected maximum events cannot be below the minimum")
        if self.maximum_lag <= timedelta(0) or self.minimum_retention <= timedelta(0):
            raise ValueError("usage telemetry lag and retention must be positive")
        manifest_stages = {UsageStage.CAPTURE, UsageStage.NORMALIZATION}
        expected_emitter = (
            UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR
            if self.stage in manifest_stages
            else UsageEmitterKind.INSTRUMENTED_RUNNER
        )
        if self.emitter_kind is not expected_emitter:
            raise ValueError(f"{self.stage.value} telemetry must use {expected_emitter.value}")
        expected_planned_cell_id = planned_cell_id_for(
            requirement_id=self.data_requirement_id,
            capture_requirement_id=self.capture_requirement_id,
            semantic_type_id=self.semantic_type_id,
            domain=self.domain,
            subject=self.subject,
            partition_key=self.partition_key,
        )
        if self.planned_cell_id and self.planned_cell_id != expected_planned_cell_id:
            raise ValueError("planned_cell_id does not match frozen telemetry demand")
        object.__setattr__(self, "planned_cell_id", expected_planned_cell_id)
        _content_id(self, field="telemetry_requirement_id", prefix="usage-telemetry-requirement")
        return self

    @property
    def event_key(self) -> tuple[str, str, str, UsageStage, str, str, DataDomain, str]:
        return (
            self.planned_cell_id,
            self.data_requirement_id,
            self.capture_requirement_id,
            self.stage,
            self.subject.kind.value,
            self.subject.id,
            self.domain,
            self.partition_key,
        )


class UsageTelemetrySloCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    usage_telemetry_slo_catalog_id: str = ""
    catalog_version: str = Field(min_length=1)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)
    completeness_target: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    maximum_catalog_lag: timedelta
    requirements: tuple[UsageTelemetryRequirement, ...] = Field(min_length=1)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> UsageTelemetrySloCatalog:
        if self.approved_at > self.effective_at:
            raise ValueError("usage telemetry SLO approval must not postdate its effective time")
        if self.maximum_catalog_lag <= timedelta(0):
            raise ValueError("maximum_catalog_lag must be positive")
        if self.research_catalog_sha256 != _content_sha256(self.research_catalog_id):
            raise ValueError("research catalog ID and hash do not match")
        if self.applicability_catalog_sha256 != _content_sha256(self.applicability_catalog_id):
            raise ValueError("applicability catalog ID and hash do not match")
        if self.registry_snapshot_sha256 != _content_sha256(self.registry_snapshot_id):
            raise ValueError("registry snapshot ID and hash do not match")
        requirements = tuple(sorted(self.requirements, key=lambda item: item.telemetry_requirement_id))
        ids = [item.telemetry_requirement_id for item in requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("usage telemetry requirements must be unique")
        keys = [item.event_key for item in requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("usage telemetry event coordinates must be unique")
        object.__setattr__(self, "requirements", requirements)
        _content_id(self, field="usage_telemetry_slo_catalog_id", prefix="usage-telemetry-slo")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.usage_telemetry_slo_catalog_id)


class UsageTelemetryReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    telemetry_requirement_id: str = Field(pattern=r"^usage-telemetry-requirement:[0-9a-f]{64}$")
    source_event_count: int = Field(ge=0)
    reconciled_at: datetime
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("reconciled_at")
    @classmethod
    def validate_reconciled_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "reconciled_at")


class UsageTelemetryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: UsageTelemetrySloCatalog
    evaluated_at: datetime
    events: tuple[DataUsageEvent, ...]
    reconciliations: tuple[UsageTelemetryReconciliation, ...]

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    def _blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        requirements = {item.telemetry_requirement_id: item for item in self.catalog.requirements}
        requirements_by_key = {item.event_key: item for item in self.catalog.requirements}
        events_by_requirement: dict[str, list[DataUsageEvent]] = defaultdict(list)
        event_ids: list[str] = []
        for event in self.events:
            event_key = (
                event.planned_cell_id,
                event.requirement_id,
                event.capture_requirement_id,
                event.stage,
                event.subject.kind.value,
                event.subject.id,
                event.domain,
                event.partition_key,
            )
            requirement = requirements_by_key.get(event_key)
            event_ids.append(event.usage_event_id)
            if requirement is None:
                blockers.append(f"{event.usage_event_id}: usage event is outside the catalog")
            else:
                events_by_requirement[requirement.telemetry_requirement_id].append(event)
        if len(event_ids) != len(set(event_ids)):
            blockers.append("usage telemetry event IDs are not idempotent")

        reconciliation_map: dict[str, list[UsageTelemetryReconciliation]] = defaultdict(list)
        for reconciliation in self.reconciliations:
            reconciliation_map[reconciliation.telemetry_requirement_id].append(reconciliation)
        completed_requirements = 0
        for telemetry_requirement_id, requirement in requirements.items():
            events = events_by_requirement.get(telemetry_requirement_id, [])
            if self.evaluated_at < requirement.expected_window_end:
                blockers.append(f"{telemetry_requirement_id}: expected usage window has not completed")
            if len(events) < requirement.expected_minimum_events:
                blockers.append(f"{telemetry_requirement_id}: usage telemetry is absent or incomplete")
            elif len(events) > requirement.expected_maximum_events:
                blockers.append(f"{telemetry_requirement_id}: usage telemetry exceeds frozen expected demand")
            else:
                completed_requirements += 1
            for event in events:
                if (
                    event.emitter_kind is not requirement.emitter_kind
                    or event.emitter_id != requirement.emitter_id
                    or event.semantic_type_id != requirement.semantic_type_id
                    or event.domain is not requirement.domain
                ):
                    blockers.append(f"{telemetry_requirement_id}: usage event attribution does not match frozen demand")
                if not (requirement.expected_window_start <= event.occurred_at <= requirement.expected_window_end):
                    blockers.append(f"{telemetry_requirement_id}: usage event occurred outside the expected window")
                if event.recorded_at - event.occurred_at > requirement.maximum_lag:
                    blockers.append(f"{telemetry_requirement_id}: usage telemetry arrived late")
                if event.retained_until < event.occurred_at + requirement.minimum_retention:
                    blockers.append(f"{telemetry_requirement_id}: usage telemetry retention is too short")
            reconciliations = reconciliation_map.get(telemetry_requirement_id, [])
            if len(reconciliations) != 1:
                blockers.append(f"{telemetry_requirement_id}: exactly one usage reconciliation is required")
            else:
                reconciliation = reconciliations[0]
                if reconciliation.reconciled_at > self.evaluated_at:
                    blockers.append(f"{telemetry_requirement_id}: usage reconciliation is postdated")
                if abs(reconciliation.source_event_count - len(events)) > requirement.maximum_reconciliation_difference:
                    blockers.append(f"{telemetry_requirement_id}: usage telemetry reconciliation failed")
        extra_reconciliations = set(reconciliation_map) - set(requirements)
        for telemetry_requirement_id in sorted(extra_reconciliations):
            blockers.append(f"{telemetry_requirement_id}: usage reconciliation is outside the catalog")
        if Decimal(completed_requirements) / Decimal(len(requirements)) < self.catalog.completeness_target:
            blockers.append("usage telemetry completeness is below SLO")
        return tuple(sorted(set(blockers)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> EvaluationStatus:
        return EvaluationStatus.FAIL if self._blockers() else EvaluationStatus.PASS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usage_telemetry_report_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"blockers", "status", "usage_telemetry_report_id"})
        return f"usage-telemetry-report:{canonical_sha256(payload)}"


class RefreshEvidenceKind(StrEnum):
    NATURAL_PUBLICATION = "natural_publication"
    IMMEDIATE_RETRY = "immediate_retry"
    SYNTHETIC_MUTATION = "synthetic_mutation"
    REPARSE = "reparse"
    FIXTURE_REPLAY = "fixture_replay"


class NaturalRefreshRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    natural_refresh_requirement_id: str = ""
    source_class: str = Field(pattern=SOURCE_ID_PATTERN)
    source_ids: tuple[str, ...] = Field(min_length=1)
    environment: CaptureEnvironment
    subject: SubjectRef
    domain: DataDomain
    partition_pattern: str = Field(min_length=1)
    cadence: timedelta
    maximum_age: timedelta
    required_naturally_changed_partitions: int = Field(ge=1)
    required_publication_transitions: int = Field(ge=1)
    maximum_observation_window: timedelta
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    alert_id: str = Field(min_length=1)
    remediation_runbook: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(re.fullmatch(SOURCE_ID_PATTERN, value) is None for value in values):
            raise ValueError("source_ids must be validated open source identifiers")
        return tuple(sorted(set(values)))

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> NaturalRefreshRequirement:
        if self.environment is CaptureEnvironment.LOCAL:
            raise ValueError("natural refresh requires an explicit logical environment tier")
        if self.approved_at > self.effective_at:
            raise ValueError("natural refresh approval must not postdate its effective time")
        if min(self.cadence, self.maximum_age, self.maximum_observation_window) <= timedelta(0):
            raise ValueError("natural refresh durations must be positive")
        _content_id(self, field="natural_refresh_requirement_id", prefix="natural-refresh")
        return self

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.natural_refresh_requirement_id)


class RefreshTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(pattern=r"^natural-refresh:[0-9a-f]{64}$")
    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    source_version: str = Field(min_length=1)
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    evidence_kind: RefreshEvidenceKind
    previous_publication_id: str = Field(min_length=1)
    current_publication_id: str = Field(min_length=1)
    previous_content_sha256: str = Field(pattern=SHA256_PATTERN)
    current_content_sha256: str = Field(pattern=SHA256_PATTERN)
    previous_published_at: datetime
    current_published_at: datetime
    observed_at: datetime

    @field_validator("previous_published_at", "current_published_at", "observed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)


class NaturalRefreshReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement: NaturalRefreshRequirement
    observation_started_at: datetime
    evaluated_at: datetime
    transitions: tuple[RefreshTransition, ...]

    @field_validator("observation_started_at", "evaluated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    def _blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if self.evaluated_at <= self.observation_started_at:
            blockers.append("natural refresh observation window is not positive")
        if self.evaluated_at - self.observation_started_at > self.requirement.maximum_observation_window:
            blockers.append("natural refresh observation exceeded the approved maximum window")
        if (
            self.requirement.approved_at > self.observation_started_at
            or self.requirement.effective_at > self.observation_started_at
        ):
            blockers.append("natural refresh requirement was not approved and effective before observation")

        valid: list[RefreshTransition] = []
        for transition in self.transitions:
            prefix = (
                f"{transition.subject.kind.value}/{transition.subject.id}/"
                f"{transition.domain.value}/{transition.partition_key}"
            )
            if transition.requirement_id != self.requirement.natural_refresh_requirement_id:
                blockers.append(f"{prefix}: refresh evidence binds a different requirement")
            if (
                transition.source_id not in self.requirement.source_ids
                or transition.subject != self.requirement.subject
                or transition.domain is not self.requirement.domain
                or not fnmatchcase(transition.partition_key, self.requirement.partition_pattern)
            ):
                blockers.append(f"{prefix}: refresh evidence is outside the approved scope")
            if transition.evidence_kind is not RefreshEvidenceKind.NATURAL_PUBLICATION:
                blockers.append(f"{prefix}: {transition.evidence_kind.value} cannot satisfy natural refresh")
                continue
            if (
                transition.previous_publication_id == transition.current_publication_id
                or transition.previous_content_sha256 == transition.current_content_sha256
            ):
                blockers.append(f"{prefix}: unchanged publication cannot satisfy natural refresh")
                continue
            if transition.current_published_at <= transition.previous_published_at:
                blockers.append(f"{prefix}: publication transition is not chronological")
                continue
            if transition.observed_at < transition.current_published_at:
                blockers.append(f"{prefix}: refresh cannot be observed before publication")
                continue
            if not (self.observation_started_at <= transition.observed_at <= self.evaluated_at):
                blockers.append(f"{prefix}: refresh observation is outside the approved window")
                continue
            valid.append(transition)
        partitions = {item.partition_key for item in valid}
        if len(partitions) < self.requirement.required_naturally_changed_partitions:
            blockers.append("naturally changed partition count is below the frozen requirement")
        if len(valid) < self.requirement.required_publication_transitions:
            blockers.append("natural publication transition count is below the frozen requirement")
        if not valid:
            blockers.append("no valid natural refresh transition was observed")
        else:
            latest = max(item.current_published_at for item in valid)
            if self.evaluated_at - latest > self.requirement.maximum_age:
                blockers.append("latest natural publication is stale")
            ordered = sorted(item.current_published_at for item in valid)
            if any(current - previous > self.requirement.cadence for previous, current in zip(ordered, ordered[1:])):
                blockers.append("natural publication cadence exceeded the frozen requirement")
        return tuple(sorted(set(blockers)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> EvaluationStatus:
        return EvaluationStatus.FAIL if self._blockers() else EvaluationStatus.PASS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def natural_refresh_report_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"blockers", "status", "natural_refresh_report_id"})
        return f"natural-refresh-report:{canonical_sha256(payload)}"


__all__ = [
    "ApplicabilityCatalog",
    "ApplicabilityCell",
    "ApplicabilityClassification",
    "ApplicabilityPolicy",
    "BudgetDimension",
    "BudgetLine",
    "CatalogPolicyClosureReport",
    "ConsumerSloCatalog",
    "ConsumerSloObservation",
    "ConsumerSloReport",
    "ConsumerSloRequirement",
    "CoverageEvidence",
    "CoverageGap",
    "EvaluationStatus",
    "FallbackPolicy",
    "KnowabilityBasis",
    "KnowabilityEvidence",
    "ModuleOutcome",
    "ModuleSloCatalog",
    "ModuleSloObservation",
    "ModuleSloReport",
    "ModuleSloThreshold",
    "NaturalRefreshReport",
    "NaturalRefreshRequirement",
    "PermissionDecision",
    "REQUIRED_SOURCE_ENVIRONMENTS",
    "RefreshEvidenceKind",
    "RefreshTransition",
    "RightsDecisionBasis",
    "SourceCoverageCatalog",
    "SourceCoverageClosureReport",
    "SourceCoverageEntry",
    "SourceCoverageRequirement",
    "SourceReadinessReport",
    "SourceRightsApproval",
    "SourceRole",
    "SourceUsagePermission",
    "UsageStage",
    "UsageTelemetryReconciliation",
    "UsageTelemetryReport",
    "UsageTelemetryRequirement",
    "UsageTelemetrySloCatalog",
    "evaluate_catalog_policy_closure",
    "evaluate_source_coverage_closure",
]
