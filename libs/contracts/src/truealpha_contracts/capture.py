"""Versioned capture scope and fail-closed completeness manifests.

The scope freezes what a run promises before the run starts. The manifest then
accounts for every promised subject/domain/partition cell. A scheduler status is
not evidence of completeness; only a passing manifest is.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.models import DataSource, _require_aware


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class CaptureEnvironment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class CaptureSubjectKind(StrEnum):
    FUND = "fund"
    ISSUER = "issuer"
    INSTRUMENT = "instrument"


class CaptureRequirementLevel(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class CaptureCellStatus(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    UNRESOLVED = "unresolved"
    STALE = "stale"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class CaptureManifestStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class CaptureSubject(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    kind: CaptureSubjectKind
    parent_subject_id: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or not value for key, value in values.items()):
            raise ValueError("identifier names and values must be non-empty")
        return dict(sorted(values.items()))


class CaptureCellRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1)
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    level: CaptureRequirementLevel
    required_fields: tuple[str, ...] = ()
    primary_source: DataSource | None = None
    fallback_sources: tuple[DataSource, ...] = ()
    maximum_age: timedelta | None = None
    minimum_confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)

    @property
    def key(self) -> tuple[str, DataDomain, str]:
        return self.subject_id, self.domain, self.partition_key

    @model_validator(mode="after")
    def validate_requirement(self) -> CaptureCellRequirement:
        fields = tuple(sorted(set(self.required_fields)))
        object.__setattr__(self, "required_fields", fields)
        if self.level is CaptureRequirementLevel.NOT_APPLICABLE:
            if self.primary_source is not None or self.fallback_sources or fields:
                raise ValueError("not-applicable requirements cannot declare fields or sources")
            return self
        if not fields:
            raise ValueError("required and optional requirements must declare required_fields")
        if self.primary_source is None:
            raise ValueError("required and optional requirements must declare a primary_source")
        if self.primary_source in self.fallback_sources:
            raise ValueError("primary_source cannot also be a fallback source")
        if len(self.fallback_sources) != len(set(self.fallback_sources)):
            raise ValueError("fallback sources must be unique")
        if self.maximum_age is not None and self.maximum_age <= timedelta(0):
            raise ValueError("maximum_age must be positive")
        return self


class CaptureScope(BaseModel):
    """Immutable, pre-run declaration of every cell a capture promises."""

    model_config = ConfigDict(frozen=True)

    capture_scope_id: str = ""
    scope_version: str = Field(min_length=1)
    environment: CaptureEnvironment
    research_catalog_version: str = Field(min_length=1)
    source_matrix_version: str = Field(min_length=1)
    slo_version: str = Field(min_length=1)
    universe_id: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    universe_membership_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Stable scope-effective baseline. Each scheduled manifest carries its own
    # evaluated cutoff so multiple cycles retain one capture_scope_id.
    as_of: datetime
    approved_by: str = Field(min_length=1)
    subjects: tuple[CaptureSubject, ...] = Field(min_length=1)
    requirements: tuple[CaptureCellRequirement, ...] = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CaptureScope:
        subjects = tuple(sorted(self.subjects, key=lambda item: item.subject_id))
        subject_ids = [subject.subject_id for subject in subjects]
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError("capture scope subjects must be unique")
        known_subjects = set(subject_ids)
        for subject in subjects:
            if subject.parent_subject_id is not None and subject.parent_subject_id not in known_subjects:
                raise ValueError(f"unknown parent subject {subject.parent_subject_id}")

        requirements = tuple(sorted(self.requirements, key=lambda item: item.key))
        requirement_keys = [requirement.key for requirement in requirements]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("capture scope requirement cells must be unique")
        unknown = {requirement.subject_id for requirement in requirements} - known_subjects
        if unknown:
            raise ValueError(f"capture requirements reference unknown subjects: {sorted(unknown)}")

        object.__setattr__(self, "subjects", subjects)
        object.__setattr__(self, "requirements", requirements)
        payload = self.model_dump(mode="json", exclude={"capture_scope_id"})
        expected = f"capture-scope:{canonical_sha256(payload)}"
        if self.capture_scope_id and self.capture_scope_id != expected:
            raise ValueError("capture_scope_id does not match canonical scope content")
        object.__setattr__(self, "capture_scope_id", expected)
        return self

    def requirement_map(self) -> dict[tuple[str, DataDomain, str], CaptureCellRequirement]:
        return {requirement.key: requirement for requirement in self.requirements}


class CaptureManifestCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1)
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    status: CaptureCellStatus
    source: DataSource | None = None
    raw_refs: tuple[str, ...] = ()
    normalized_record_ids: tuple[str, ...] = ()
    record_count: int = Field(default=0, ge=0)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    min_knowable_at: datetime | None = None
    max_knowable_at: datetime | None = None
    recorded_at: datetime | None = None
    observed_at: datetime | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    mapping_version: str | None = None
    detail: str | None = None

    @property
    def key(self) -> tuple[str, DataDomain, str]:
        return self.subject_id, self.domain, self.partition_key

    @field_validator("min_knowable_at", "max_knowable_at", "recorded_at", "observed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @field_validator("raw_refs")
    @classmethod
    def validate_raw_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.startswith("raw.fetches:") for value in values):
            raise ValueError("raw_refs must use raw.fetches:<id> identifiers")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> CaptureManifestCell:
        object.__setattr__(self, "normalized_record_ids", tuple(sorted(set(self.normalized_record_ids))))
        if self.status is CaptureCellStatus.COMPLETE:
            max_knowable_at = self.max_knowable_at
            recorded_at = self.recorded_at
            required = (
                self.source,
                self.raw_refs,
                self.normalized_record_ids,
                self.content_sha256,
                max_knowable_at,
                recorded_at,
                self.confidence,
                self.mapping_version,
            )
            if any(value is None or value == () for value in required):
                raise ValueError(
                    "complete cells require source, raw/normalized lineage, hashes, times, confidence, and mapping"
                )
            assert max_knowable_at is not None and recorded_at is not None
            if self.record_count <= 0 or self.record_count != len(self.normalized_record_ids):
                raise ValueError("complete cell record_count must equal normalized_record_ids length")
            if self.min_knowable_at is not None and self.min_knowable_at > max_knowable_at:
                raise ValueError("min_knowable_at cannot follow max_knowable_at")
            if recorded_at < max_knowable_at:
                raise ValueError("recorded_at cannot precede max_knowable_at")
        elif self.status is CaptureCellStatus.NOT_APPLICABLE:
            if self.record_count or self.raw_refs or self.normalized_record_ids:
                raise ValueError("not-applicable cells cannot carry captured records")
        elif not self.detail:
            raise ValueError("non-complete cells require a failure/unavailability detail")
        return self


class CaptureManifest(BaseModel):
    """Self-contained evidence that every cell in a CaptureScope was accounted for."""

    model_config = ConfigDict(frozen=True)

    capture_manifest_id: str = ""
    scope: CaptureScope
    run_id: str = Field(min_length=1)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    as_of: datetime
    started_at: datetime
    completed_at: datetime
    cells: tuple[CaptureManifestCell, ...] = Field(min_length=1)
    status: CaptureManifestStatus = CaptureManifestStatus.FAIL
    blockers: tuple[str, ...] = ()

    @field_validator("as_of", "started_at", "completed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def evaluate(self) -> CaptureManifest:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        cells = tuple(sorted(self.cells, key=lambda item: item.key))
        cell_keys = [cell.key for cell in cells]
        if len(cell_keys) != len(set(cell_keys)):
            raise ValueError("capture manifest cells must be unique")

        requirements = self.scope.requirement_map()
        if set(cell_keys) != set(requirements):
            missing = sorted(set(requirements) - set(cell_keys))
            extra = sorted(set(cell_keys) - set(requirements))
            raise ValueError(f"manifest cells must exactly match scope requirements; missing={missing}, extra={extra}")

        blockers: list[str] = []
        for cell in cells:
            requirement = requirements[cell.key]
            prefix = f"{cell.subject_id}/{cell.domain.value}/{cell.partition_key}"
            if requirement.level is CaptureRequirementLevel.NOT_APPLICABLE:
                if cell.status is not CaptureCellStatus.NOT_APPLICABLE:
                    blockers.append(f"{prefix}: scope declares not_applicable")
                continue
            if cell.status is CaptureCellStatus.NOT_APPLICABLE:
                blockers.append(f"{prefix}: applicability changed after scope freeze")
                continue
            if requirement.level is CaptureRequirementLevel.REQUIRED and cell.status is not CaptureCellStatus.COMPLETE:
                blockers.append(f"{prefix}: required cell is {cell.status.value}")
                continue
            if cell.status is not CaptureCellStatus.COMPLETE:
                continue
            allowed_sources = (requirement.primary_source, *requirement.fallback_sources)
            if cell.source not in allowed_sources:
                blockers.append(f"{prefix}: source {cell.source} is not approved")
            if cell.confidence is None or cell.confidence < requirement.minimum_confidence:
                blockers.append(f"{prefix}: confidence is below the frozen minimum")
            if cell.max_knowable_at is None or cell.max_knowable_at > self.as_of:
                blockers.append(f"{prefix}: contains future knowledge")
            if requirement.maximum_age is not None:
                if cell.max_knowable_at is None:
                    blockers.append(f"{prefix}: freshness cannot be evaluated")
                elif self.as_of - cell.max_knowable_at > requirement.maximum_age:
                    blockers.append(f"{prefix}: data is stale")

        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "blockers", tuple(blockers))
        object.__setattr__(self, "status", CaptureManifestStatus.FAIL if blockers else CaptureManifestStatus.PASS)
        payload = self.model_dump(
            mode="json",
            exclude={"capture_manifest_id", "status", "blockers"},
        )
        expected = f"capture-manifest:{canonical_sha256(payload)}"
        if self.capture_manifest_id and self.capture_manifest_id != expected:
            raise ValueError("capture_manifest_id does not match canonical manifest content")
        object.__setattr__(self, "capture_manifest_id", expected)
        return self

    @property
    def complete(self) -> bool:
        return self.status is CaptureManifestStatus.PASS
