"""Executable point-in-time snapshot, factor, lineage, and replay contracts.

This module keeps evidence-bearing orchestration objects outside factor code.
Factors receive only ``ProvenanceNeutralInput`` values and return output drafts;
the runner owns input-read events and derives durable output lineage.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.models import _require_aware
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeId
from truealpha_contracts.universe import SubjectRef, UniverseManifest, UniverseMembership, UniverseRef
from truealpha_contracts.usage import RequirementLevel, planned_cell_id_for

_SHA256 = r"^[0-9a-f]{64}$"
_CONTENT_ID = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_STABLE_KEY = r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$"
_MUTABLE_VERSION_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})


def _identify(model: BaseModel, *, id_field: str, hash_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, hash_field})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, hash_field)
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError(f"{hash_field} does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, expected_hash)
    object.__setattr__(model, id_field, expected_id)


def _validate_content_reference(reference_id: str, content_sha256: str, prefix: str) -> None:
    if reference_id != f"{prefix}:{content_sha256}":
        raise ValueError(f"{prefix} ID and hash do not match")


def _reject_mutable_version(value: str, field_name: str) -> str:
    tokens = {token for token in re.split(r"[._:/@+-]", value.lower()) if token}
    if tokens & _MUTABLE_VERSION_TOKENS:
        raise ValueError(f"{field_name} must be immutable")
    return value


class ModelRevisionRef(BaseModel):
    """One exact provider model revision and its immutable execution settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_revision_id: str = Field(default="", pattern=r"^(?:|model-revision:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    provider: str = Field(pattern=_STABLE_KEY)
    model_id: str = Field(pattern=_STABLE_KEY)
    immutable_revision: str = Field(pattern=_STABLE_KEY)
    endpoint_or_artifact_sha256: str = Field(pattern=_SHA256)
    decoding_parameters_sha256: str = Field(pattern=_SHA256)

    @field_validator("immutable_revision")
    @classmethod
    def reject_mutable_model_aliases(cls, value: str) -> str:
        return _reject_mutable_version(value, "model revisions")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ModelRevisionRef:
        _identify(self, id_field="model_revision_id", hash_field="content_sha256", prefix="model-revision")
        return self


class ExtractionTemplate(BaseModel):
    """Frozen instructions, output schema, model revision, and extractor code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extraction_template_id: str = Field(default="", pattern=r"^(?:|extraction-template:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    template_name: str = Field(pattern=_STABLE_KEY)
    template_version: str = Field(pattern=_STABLE_KEY)
    semantic_type_id: SemanticTypeId
    semantic_type_version: str = Field(pattern=_STABLE_KEY)
    payload_model_key: str = Field(pattern=_STABLE_KEY)
    output_schema_sha256: str = Field(pattern=_SHA256)
    instructions_sha256: str = Field(pattern=_SHA256)
    extractor_implementation_sha256: str = Field(pattern=_SHA256)
    model_revision_id: str = Field(pattern=r"^model-revision:[0-9a-f]{64}$")
    model_revision_sha256: str = Field(pattern=_SHA256)

    @field_validator("template_version")
    @classmethod
    def reject_mutable_template_aliases(cls, value: str) -> str:
        return _reject_mutable_version(value, "extraction template versions")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ExtractionTemplate:
        _validate_content_reference(self.model_revision_id, self.model_revision_sha256, "model-revision")
        _identify(
            self,
            id_field="extraction_template_id",
            hash_field="content_sha256",
            prefix="extraction-template",
        )
        return self


class ExtractionInvocation(BaseModel):
    """One completed live model attempt; replay reuses this exact object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extraction_invocation_id: str = Field(default="", pattern=r"^(?:|extraction-invocation:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    model_revision_id: str = Field(pattern=r"^model-revision:[0-9a-f]{64}$")
    model_revision_sha256: str = Field(pattern=_SHA256)
    extraction_template_id: str = Field(pattern=r"^extraction-template:[0-9a-f]{64}$")
    extraction_template_sha256: str = Field(pattern=_SHA256)
    input_sha256: str = Field(pattern=_SHA256)
    response_sha256: str = Field(pattern=_SHA256)
    semantic_payload_sha256: str = Field(pattern=_SHA256)
    attempt_number: int = Field(ge=1)
    previous_invocation_id: str | None = Field(
        default=None,
        pattern=r"^extraction-invocation:[0-9a-f]{64}$",
    )
    previous_invocation_sha256: str | None = Field(default=None, pattern=_SHA256)
    started_at: datetime
    completed_at: datetime
    invoker_id: str = Field(pattern=_STABLE_KEY)
    invoker_version: str = Field(pattern=_STABLE_KEY)
    invoker_implementation_sha256: str = Field(pattern=_SHA256)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @field_validator("invoker_version")
    @classmethod
    def reject_mutable_invoker_version(cls, value: str) -> str:
        return _reject_mutable_version(value, "extraction invoker version")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ExtractionInvocation:
        _validate_content_reference(self.model_revision_id, self.model_revision_sha256, "model-revision")
        _validate_content_reference(
            self.extraction_template_id,
            self.extraction_template_sha256,
            "extraction-template",
        )
        if self.completed_at < self.started_at:
            raise ValueError("extraction completion cannot precede its start")
        previous_fields = (self.previous_invocation_id, self.previous_invocation_sha256)
        if self.attempt_number == 1 and any(value is not None for value in previous_fields):
            raise ValueError("the first extraction attempt cannot name a previous invocation")
        if self.attempt_number > 1 and any(value is None for value in previous_fields):
            raise ValueError("a retry extraction attempt requires the exact previous invocation")
        if self.previous_invocation_id is not None and self.previous_invocation_sha256 is not None:
            _validate_content_reference(
                self.previous_invocation_id,
                self.previous_invocation_sha256,
                "extraction-invocation",
            )
        _identify(
            self,
            id_field="extraction_invocation_id",
            hash_field="content_sha256",
            prefix="extraction-invocation",
        )
        if self.previous_invocation_id == self.extraction_invocation_id:
            raise ValueError("an extraction retry cannot name itself as the previous attempt")
        return self


class SemanticProducerKind(StrEnum):
    DETERMINISTIC_NORMALIZER = "deterministic_normalizer"
    VERSIONED_EXTRACTION = "versioned_extraction"


class SemanticDraft(BaseModel):
    """Typed extraction/normalization result before evidence is attached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_draft_id: str = ""
    content_sha256: str = ""
    semantic_type_id: SemanticTypeId
    semantic_type_version: str = Field(pattern=_STABLE_KEY)
    payload_model_key: str = Field(pattern=_STABLE_KEY)
    payload_schema_sha256: str = Field(pattern=_SHA256)
    payload_sha256: str = Field(pattern=_SHA256)
    subject: SubjectRef
    valid_from: date
    valid_to: date
    knowable_at: datetime
    produced_at: datetime
    producer_kind: SemanticProducerKind
    producer_id: str = Field(pattern=_STABLE_KEY)
    producer_version: str = Field(pattern=_STABLE_KEY)
    producer_implementation_sha256: str = Field(pattern=_SHA256)
    model_revision_id: str | None = Field(default=None, pattern=r"^model-revision:[0-9a-f]{64}$")
    model_revision_sha256: str | None = Field(default=None, pattern=_SHA256)
    extraction_template_id: str | None = Field(default=None, pattern=r"^extraction-template:[0-9a-f]{64}$")
    extraction_template_sha256: str | None = Field(default=None, pattern=_SHA256)
    extraction_invocation_id: str | None = Field(
        default=None,
        pattern=r"^extraction-invocation:[0-9a-f]{64}$",
    )
    extraction_invocation_sha256: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("knowable_at", "produced_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> SemanticDraft:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.produced_at < self.knowable_at:
            raise ValueError("a semantic draft cannot be produced before it is knowable")
        extraction_fields = (
            self.model_revision_id,
            self.model_revision_sha256,
            self.extraction_template_id,
            self.extraction_template_sha256,
            self.extraction_invocation_id,
            self.extraction_invocation_sha256,
        )
        if self.producer_kind is SemanticProducerKind.VERSIONED_EXTRACTION and None in extraction_fields:
            raise ValueError("versioned extraction requires exact model, template, and invocation identities")
        if self.producer_kind is SemanticProducerKind.DETERMINISTIC_NORMALIZER and any(extraction_fields):
            raise ValueError("deterministic normalization cannot claim extraction identities")
        if self.producer_kind is SemanticProducerKind.VERSIONED_EXTRACTION:
            assert self.model_revision_id is not None
            assert self.model_revision_sha256 is not None
            assert self.extraction_template_id is not None
            assert self.extraction_template_sha256 is not None
            assert self.extraction_invocation_id is not None
            assert self.extraction_invocation_sha256 is not None
            _validate_content_reference(self.model_revision_id, self.model_revision_sha256, "model-revision")
            _validate_content_reference(
                self.extraction_template_id,
                self.extraction_template_sha256,
                "extraction-template",
            )
            _validate_content_reference(
                self.extraction_invocation_id,
                self.extraction_invocation_sha256,
                "extraction-invocation",
            )
        _identify(self, id_field="semantic_draft_id", hash_field="content_sha256", prefix="semantic-draft")
        return self


def validate_extraction_replay(
    *,
    draft: SemanticDraft,
    invocation: ExtractionInvocation,
    template: ExtractionTemplate,
    model_revision: ModelRevisionRef,
) -> SemanticDraft:
    """Return a stored extraction draft only when the complete frozen chain matches."""

    if draft.producer_kind is not SemanticProducerKind.VERSIONED_EXTRACTION:
        raise ValueError("only versioned extraction drafts have replay bindings")
    if (
        template.model_revision_id != model_revision.model_revision_id
        or template.model_revision_sha256 != model_revision.content_sha256
    ):
        raise ValueError("extraction template does not bind the supplied model revision")
    if (
        invocation.model_revision_id != model_revision.model_revision_id
        or invocation.model_revision_sha256 != model_revision.content_sha256
        or invocation.extraction_template_id != template.extraction_template_id
        or invocation.extraction_template_sha256 != template.content_sha256
    ):
        raise ValueError("extraction invocation does not bind the supplied model and template")
    if (
        draft.model_revision_id != model_revision.model_revision_id
        or draft.model_revision_sha256 != model_revision.content_sha256
        or draft.extraction_template_id != template.extraction_template_id
        or draft.extraction_template_sha256 != template.content_sha256
        or draft.extraction_invocation_id != invocation.extraction_invocation_id
        or draft.extraction_invocation_sha256 != invocation.content_sha256
    ):
        raise ValueError("extraction replay does not match the draft's frozen invocation chain")
    if (
        draft.semantic_type_id != template.semantic_type_id
        or draft.semantic_type_version != template.semantic_type_version
        or draft.payload_model_key != template.payload_model_key
        or draft.payload_schema_sha256 != template.output_schema_sha256
        or draft.payload_sha256 != invocation.semantic_payload_sha256
    ):
        raise ValueError("extraction replay output does not match the frozen template or invocation")
    if (
        invocation.invoker_implementation_sha256 != template.extractor_implementation_sha256
        or draft.producer_id != invocation.invoker_id
        or draft.producer_version != invocation.invoker_version
        or draft.producer_implementation_sha256 != invocation.invoker_implementation_sha256
    ):
        raise ValueError("extraction replay producer does not match the frozen extractor implementation")
    if draft.produced_at < invocation.completed_at:
        raise ValueError("extraction draft cannot predate its completed invocation")
    return draft


class NormalizedRecordRef(BaseModel):
    """Append-only normalized record with data-engine-owned evidence lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_record_id: str = ""
    content_sha256: str = ""
    draft: SemanticDraft
    document_id: str = Field(pattern=_STABLE_KEY)
    raw_object_id: str = Field(pattern=_STABLE_KEY)
    raw_object_sha256: str = Field(pattern=_SHA256)
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=_SHA256)
    mapping_version: str = Field(pattern=_STABLE_KEY)
    mapping_implementation_sha256: str = Field(pattern=_SHA256)
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    is_restatement: bool = False
    supersedes_record_id: str | None = Field(default=None, pattern=r"^normalized-record:[0-9a-f]{64}$")

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "recorded_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> NormalizedRecordRef:
        if self.recorded_at < self.draft.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        if self.is_restatement != (self.supersedes_record_id is not None):
            raise ValueError("restatements must append and name the superseded record")
        _identify(self, id_field="normalized_record_id", hash_field="content_sha256", prefix="normalized-record")
        if self.supersedes_record_id == self.normalized_record_id:
            raise ValueError("a normalized record cannot supersede itself")
        return self


class PolicyRole(StrEnum):
    MEMBERSHIP = "membership"
    IDENTITY = "identity"
    METRIC = "metric"
    FUSION = "fusion"
    EXTRACTION = "extraction"
    PRICE = "price"
    FX = "fx"
    CORPORATE_ACTION = "corporate_action"
    MARKET_CALENDAR = "market_calendar"
    APPLICABILITY = "applicability"


class PolicyBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: PolicyRole
    policy_id: str = Field(pattern=_STABLE_KEY)
    policy_version: str = Field(pattern=_STABLE_KEY)
    implementation_sha256: str = Field(pattern=_SHA256)


class SnapshotDemandCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_cell_id: str = Field(default="", pattern=r"^(?:|planned-demand-cell:[0-9a-f]{64})$")
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    semantic_type_id: SemanticTypeId
    semantic_type_version: str = Field(pattern=_STABLE_KEY)
    domain: DataDomain
    subject: SubjectRef
    partition_key: str = Field(min_length=1)
    level: RequirementLevel

    @model_validator(mode="after")
    def identify(self) -> SnapshotDemandCell:
        expected_id = planned_cell_id_for(
            requirement_id=self.requirement_id,
            capture_requirement_id=self.capture_requirement_id,
            semantic_type_id=self.semantic_type_id,
            domain=self.domain,
            subject=self.subject,
            partition_key=self.partition_key,
        )
        if self.planned_cell_id and self.planned_cell_id != expected_id:
            raise ValueError("planned_cell_id does not match frozen snapshot demand")
        object.__setattr__(self, "planned_cell_id", expected_id)
        return self

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.requirement_id, self.subject.kind.value, self.subject.id, self.partition_key


class SnapshotRequest(BaseModel):
    """Frozen atomic PIT request over explicit subjects or one exact universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_request_id: str = ""
    content_sha256: str = ""
    subjects: tuple[SubjectRef, ...] = ()
    universe: UniverseRef | None = None
    as_of: datetime
    valid_on: date
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256)
    source_registry_id: str = Field(pattern=r"^source-registry:[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=_SHA256)
    semantic_type_registry_id: str = Field(pattern=r"^semantic-type-registry:[0-9a-f]{64}$")
    semantic_type_registry_sha256: str = Field(pattern=_SHA256)
    policy_bindings: tuple[PolicyBinding, ...] = Field(min_length=1)
    demand_cells: tuple[SnapshotDemandCell, ...] = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> SnapshotRequest:
        if bool(self.subjects) == (self.universe is not None):
            raise ValueError("snapshot request requires exactly one of explicit subjects or UniverseRef")
        subjects = tuple(sorted(self.subjects, key=lambda item: (item.kind.value, item.id)))
        if len(subjects) != len(set(subjects)):
            raise ValueError("explicit snapshot subjects must be unique")
        policies = tuple(sorted(self.policy_bindings, key=lambda item: item.role.value))
        if len({item.role for item in policies}) != len(policies):
            raise ValueError("snapshot policy roles must be unique")
        required_roles = {
            PolicyRole.IDENTITY,
            PolicyRole.METRIC,
            PolicyRole.FUSION,
            PolicyRole.EXTRACTION,
            PolicyRole.PRICE,
            PolicyRole.FX,
            PolicyRole.CORPORATE_ACTION,
            PolicyRole.MARKET_CALENDAR,
            PolicyRole.APPLICABILITY,
        }
        if self.universe is not None:
            required_roles.add(PolicyRole.MEMBERSHIP)
        missing_roles = required_roles - {item.role for item in policies}
        if missing_roles:
            raise ValueError(f"snapshot policies are incomplete: {sorted(item.value for item in missing_roles)}")
        cells = tuple(sorted(self.demand_cells, key=lambda item: item.key))
        if len({item.key for item in cells}) != len(cells):
            raise ValueError("snapshot demand cells must be unique")
        if subjects:
            subject_set = set(subjects)
            outside = {cell.subject for cell in cells} - subject_set
            if outside:
                raise ValueError("snapshot demand references subjects outside explicit scope")
        object.__setattr__(self, "subjects", subjects)
        object.__setattr__(self, "policy_bindings", policies)
        object.__setattr__(self, "demand_cells", cells)
        _identify(self, id_field="snapshot_request_id", hash_field="content_sha256", prefix="snapshot-request")
        return self


class SnapshotCellSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    demand: SnapshotDemandCell
    normalized_record_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_selection(self) -> SnapshotCellSelection:
        records = tuple(sorted(set(self.normalized_record_ids)))
        reasons = tuple(sorted(set(self.reason_codes)))
        if self.demand.level is RequirementLevel.REQUIRED and not records:
            raise ValueError("required snapshot demand cannot be materialized without a normalized record")
        if records and reasons:
            raise ValueError("selected snapshot cells cannot also carry absence reasons")
        if not records and not reasons:
            raise ValueError("an empty optional snapshot cell requires explicit reason codes")
        object.__setattr__(self, "normalized_record_ids", records)
        object.__setattr__(self, "reason_codes", reasons)
        return self


class SnapshotManifest(BaseModel):
    """Immutable exact record selection resolved atomically for one request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = ""
    content_sha256: str = ""
    request: SnapshotRequest
    registry_snapshot: RegistrySnapshot
    resolved_subjects: tuple[SubjectRef, ...] = Field(min_length=1)
    universe_manifest: UniverseManifest | None = None
    universe_memberships: tuple[UniverseMembership, ...] = ()
    normalized_records: tuple[NormalizedRecordRef, ...] = Field(min_length=1)
    selections: tuple[SnapshotCellSelection, ...] = Field(min_length=1)
    resolved_at: datetime
    resolver_id: str = Field(pattern=_STABLE_KEY)
    resolver_version: str = Field(pattern=_STABLE_KEY)
    resolver_implementation_sha256: str = Field(pattern=_SHA256)

    @field_validator("resolved_at")
    @classmethod
    def validate_resolved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "resolved_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> SnapshotManifest:
        if self.resolved_at < self.request.as_of:
            raise ValueError("snapshot cannot resolve before its cutoff")
        if (
            self.request.registry_snapshot_id != self.registry_snapshot.registry_snapshot_id
            or self.request.registry_snapshot_sha256 != self.registry_snapshot.content_sha256
            or self.request.source_registry_id != self.registry_snapshot.source_registry_snapshot_id
            or self.request.source_registry_sha256 != self.registry_snapshot.source_registry_sha256
            or self.request.semantic_type_registry_id != self.registry_snapshot.semantic_type_registry_snapshot_id
            or self.request.semantic_type_registry_sha256 != self.registry_snapshot.semantic_type_registry_sha256
        ):
            raise ValueError("snapshot request does not bind the supplied registry snapshot exactly")

        subjects = tuple(sorted(self.resolved_subjects, key=lambda item: (item.kind.value, item.id)))
        if len(subjects) != len(set(subjects)):
            raise ValueError("resolved snapshot subjects must be unique")
        if self.request.subjects and subjects != self.request.subjects:
            raise ValueError("resolved subjects do not match the explicit request")
        memberships = tuple(sorted(self.universe_memberships, key=lambda item: item.membership_id))
        if self.request.universe is None:
            if memberships or self.universe_manifest is not None:
                raise ValueError("explicit-subject snapshots cannot claim universe evidence")
        else:
            if self.universe_manifest is None or self.universe_manifest.ref != self.request.universe:
                raise ValueError("universe snapshot requires the exact referenced manifest")
            if not memberships:
                raise ValueError("universe snapshots require exact membership evidence")
            if any(item.universe_id != self.request.universe.universe_id for item in memberships):
                raise ValueError("membership evidence belongs to another universe")
            membership_subjects = tuple(
                sorted({item.subject for item in memberships}, key=lambda item: (item.kind.value, item.id))
            )
            if subjects != membership_subjects:
                raise ValueError("resolved subjects do not match universe membership evidence")
            if self.universe_manifest.membership_ids:
                if tuple(sorted(item.membership_id for item in memberships)) != self.universe_manifest.membership_ids:
                    raise ValueError("fixed-cohort snapshot membership IDs do not match the universe manifest")
            for membership in memberships:
                if not (membership.valid_from <= self.request.valid_on):
                    raise ValueError("universe membership starts after snapshot valid_on")
                if membership.valid_to is not None and membership.valid_to < self.request.valid_on:
                    raise ValueError("universe membership ended before snapshot valid_on")
                if membership.knowable_at > self.request.as_of:
                    raise ValueError("future universe membership leaked into the snapshot")

        records = tuple(sorted(self.normalized_records, key=lambda item: item.normalized_record_id))
        record_map = {item.normalized_record_id: item for item in records}
        if len(record_map) != len(records):
            raise ValueError("snapshot normalized records must be unique")
        registry_types = {(item.semantic_type_id, item.version): item for item in self.registry_snapshot.semantic_types}
        registry_sources = {
            (item.source_registry_entry_id, item.content_sha256) for item in self.registry_snapshot.sources
        }
        for record in records:
            draft = record.draft
            if (draft.semantic_type_id, draft.semantic_type_version) not in registry_types:
                raise ValueError("snapshot record uses an unknown semantic type version")
            if draft.knowable_at > self.request.as_of:
                raise ValueError("future normalized record leaked into the snapshot")
            if not (draft.valid_from <= self.request.valid_on <= draft.valid_to):
                raise ValueError("snapshot record is outside valid_on")
            if (
                record.source_registry_entry_id,
                record.source_registry_entry_sha256,
            ) not in registry_sources:
                raise ValueError("snapshot record uses an unknown or drifted source registry entry")

        selections = tuple(sorted(self.selections, key=lambda item: item.demand.key))
        selection_keys = [item.demand.key for item in selections]
        request_keys = [item.key for item in self.request.demand_cells]
        if selection_keys != request_keys:
            raise ValueError("snapshot selections must be row-complete over frozen demand")
        for selection in selections:
            for record_id in selection.normalized_record_ids:
                selected_record = record_map.get(record_id)
                if selected_record is None:
                    raise ValueError("snapshot selection references a missing normalized record")
                if (
                    selected_record.draft.subject != selection.demand.subject
                    or selected_record.draft.semantic_type_id != selection.demand.semantic_type_id
                    or selected_record.draft.semantic_type_version != selection.demand.semantic_type_version
                ):
                    raise ValueError("snapshot selection record does not match frozen demand")
        selected_ids = {record_id for item in selections for record_id in item.normalized_record_ids}
        if selected_ids != set(record_map):
            raise ValueError("snapshot records and row selections must reconcile exactly")
        object.__setattr__(self, "resolved_subjects", subjects)
        object.__setattr__(self, "universe_memberships", memberships)
        object.__setattr__(self, "normalized_records", records)
        object.__setattr__(self, "selections", selections)
        _identify(self, id_field="snapshot_id", hash_field="content_sha256", prefix="snapshot")
        return self


class SnapshotRepository(Protocol):
    def put(self, snapshot: SnapshotManifest) -> bool: ...

    def get(self, snapshot_id: str) -> SnapshotManifest | None: ...


class FactorKind(StrEnum):
    BASE = "base"
    COMPOSITE = "composite"
    STRATEGY = "strategy"


class DependencyTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str = Field(pattern=_STABLE_KEY)
    template_id: str = Field(pattern=r"^factor-template:[0-9a-f]{64}$")


class FactorInvocationTemplate(BaseModel):
    """Stable catalog target, independent of a particular snapshot execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_template_id: str = ""
    content_sha256: str = ""
    factor_id: str = Field(pattern=_STABLE_KEY)
    factor_version: str = Field(pattern=_STABLE_KEY)
    factor_implementation_sha256: str = Field(pattern=_SHA256)
    factor_kind: FactorKind
    parameter_model_key: str = Field(pattern=_STABLE_KEY)
    parameter_schema_sha256: str = Field(pattern=_SHA256)
    canonical_parameters_sha256: str = Field(pattern=_SHA256)
    data_requirement_ids: tuple[str, ...] = Field(min_length=1)
    dependencies: tuple[DependencyTemplate, ...] = ()

    @field_validator("data_requirement_ids")
    @classmethod
    def validate_requirement_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(re.fullmatch(r"data-requirement:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("factor requirements must use content-addressed DataRequirement IDs")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def freeze_and_identify(self) -> FactorInvocationTemplate:
        dependencies = tuple(sorted(self.dependencies, key=lambda item: item.alias))
        if len({item.alias for item in dependencies}) != len(dependencies):
            raise ValueError("dependency aliases must be unique")
        if self.factor_kind is FactorKind.BASE and dependencies:
            raise ValueError("base factors cannot depend on materialized factor templates")
        if self.factor_kind in {FactorKind.COMPOSITE, FactorKind.STRATEGY} and not dependencies:
            raise ValueError("composite and strategy templates require declared dependencies")
        object.__setattr__(self, "dependencies", dependencies)
        _identify(self, id_field="factor_template_id", hash_field="content_sha256", prefix="factor-template")
        return self


class FactorExecution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_execution_id: str = ""
    content_sha256: str = ""
    template: FactorInvocationTemplate
    snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=_SHA256)
    ordered_subjects: tuple[SubjectRef, ...] = Field(min_length=1)
    upstream_batch_ids: tuple[str, ...] = ()
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "started_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> FactorExecution:
        if len(self.ordered_subjects) != len(set(self.ordered_subjects)):
            raise ValueError("execution subjects must be unique while preserving requested order")
        batches = tuple(sorted(set(self.upstream_batch_ids)))
        if self.template.factor_kind is FactorKind.BASE and batches:
            raise ValueError("base execution cannot consume upstream factor batches")
        if self.template.factor_kind in {FactorKind.COMPOSITE, FactorKind.STRATEGY} and not batches:
            raise ValueError("composite and strategy execution require persisted upstream batches")
        object.__setattr__(self, "upstream_batch_ids", batches)
        identity = self.model_dump(
            mode="json",
            include={"template", "snapshot_id", "snapshot_sha256", "ordered_subjects", "upstream_batch_ids"},
        )
        expected_hash = canonical_sha256(identity)
        expected_id = f"factor-execution:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match factor execution identity")
        if self.factor_execution_id and self.factor_execution_id != expected_id:
            raise ValueError("factor_execution_id does not match factor execution identity")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "factor_execution_id", expected_id)
        return self


class ProvenanceNeutralInput(BaseModel):
    """The only semantic input shape visible to factor computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: SubjectRef
    payload_model_key: str = Field(pattern=_STABLE_KEY)
    payload_sha256: str = Field(pattern=_SHA256)
    valid_from: date
    valid_to: date
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @model_validator(mode="after")
    def validate_period(self) -> ProvenanceNeutralInput:
        if self.valid_to < self.valid_from:
            raise ValueError("input valid_to must not precede valid_from")
        return self


class RequirementHandle(BaseModel):
    """Opaque runner-minted capability exposed to factor computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_handle_id: str = Field(pattern=r"^requirement-handle:[0-9a-f]{64}$")


class FactorInputCapability(BaseModel):
    """Factor-visible input with no source, record, usage, or validation metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: RequirementHandle
    observation: ProvenanceNeutralInput


class InputEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class RunnerInputBinding(BaseModel):
    """Private capability binding retained by the instrumented runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: RequirementHandle
    demand: SnapshotDemandCell
    input_id: str = Field(pattern=r"^(?:normalized-record|factor-output):[0-9a-f]{64}$")
    observation: ProvenanceNeutralInput
    evidence_status: InputEvidenceStatus
    upstream_batch_id: str | None = Field(default=None, pattern=r"^factor-batch:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_binding(self) -> RunnerInputBinding:
        if self.observation.subject != self.demand.subject:
            raise ValueError("runner observation subject does not match bound demand")
        is_upstream = self.input_id.startswith("factor-output:")
        if is_upstream != (self.upstream_batch_id is not None):
            raise ValueError("factor-output bindings must name their persisted upstream batch")
        return self


def _mint_requirement_handle(
    *,
    factor_execution_id: str,
    snapshot_id: str,
    demand: SnapshotDemandCell,
    input_id: str,
    runner_id: str,
    runner_version: str,
    runner_implementation_sha256: str,
) -> RequirementHandle:
    digest = canonical_sha256(
        {
            "factor_execution_id": factor_execution_id,
            "snapshot_id": snapshot_id,
            "requirement_id": demand.requirement_id,
            "capture_requirement_id": demand.capture_requirement_id,
            "planned_cell_id": demand.planned_cell_id,
            "input_id": input_id,
            "runner_id": runner_id,
            "runner_version": runner_version,
            "runner_implementation_sha256": runner_implementation_sha256,
        }
    )
    return RequirementHandle(requirement_handle_id=f"requirement-handle:{digest}")


class RunnerInputSelection(BaseModel):
    """Runner-owned mapping from exact snapshot records to neutral factor inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_id: str = ""
    content_sha256: str = ""
    factor_execution_id: str = Field(pattern=r"^factor-execution:[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    bindings: tuple[RunnerInputBinding, ...] = ()
    selected_at: datetime
    runner_id: str = Field(pattern=_STABLE_KEY)
    runner_version: str = Field(pattern=_STABLE_KEY)
    runner_implementation_sha256: str = Field(pattern=_SHA256)

    @field_validator("selected_at")
    @classmethod
    def validate_selected_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "selected_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> RunnerInputSelection:
        bindings = tuple(sorted(self.bindings, key=lambda item: item.handle.requirement_handle_id))
        handles = [item.handle.requirement_handle_id for item in bindings]
        if len(handles) != len(set(handles)):
            raise ValueError("runner requirement handles must be unique")
        coordinates = [(item.demand.planned_cell_id, item.input_id) for item in bindings]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("runner demand/input bindings must be unique")
        for binding in bindings:
            expected = _mint_requirement_handle(
                factor_execution_id=self.factor_execution_id,
                snapshot_id=self.snapshot_id,
                demand=binding.demand,
                input_id=binding.input_id,
                runner_id=self.runner_id,
                runner_version=self.runner_version,
                runner_implementation_sha256=self.runner_implementation_sha256,
            )
            if binding.handle != expected:
                raise ValueError("requirement handle was not minted for the exact runner demand binding")
        object.__setattr__(self, "bindings", bindings)
        _identify(self, id_field="selection_id", hash_field="content_sha256", prefix="runner-selection")
        return self

    @property
    def factor_inputs(self) -> tuple[FactorInputCapability, ...]:
        return tuple(
            FactorInputCapability(handle=binding.handle, observation=binding.observation) for binding in self.bindings
        )


def build_runner_input_selection(
    *,
    execution: FactorExecution,
    snapshot: SnapshotManifest,
    selected_at: datetime,
    runner_id: str,
    runner_version: str,
    runner_implementation_sha256: str,
) -> RunnerInputSelection:
    """Mint exact normalized-record capabilities from a frozen snapshot denominator."""

    if execution.snapshot_id != snapshot.snapshot_id or execution.snapshot_sha256 != snapshot.content_sha256:
        raise ValueError("factor execution does not bind the supplied snapshot exactly")
    declared_requirements = set(execution.template.data_requirement_ids)
    snapshot_requirements = {selection.demand.requirement_id for selection in snapshot.selections}
    missing = declared_requirements - snapshot_requirements
    if missing:
        raise ValueError(f"factor requirements are absent from the frozen snapshot demand: {sorted(missing)}")
    records = {record.normalized_record_id: record for record in snapshot.normalized_records}
    bindings: list[RunnerInputBinding] = []
    for cell in snapshot.selections:
        if cell.demand.requirement_id not in declared_requirements:
            continue
        for input_id in cell.normalized_record_ids:
            record = records[input_id]
            observation = ProvenanceNeutralInput(
                subject=record.draft.subject,
                payload_model_key=record.draft.payload_model_key,
                payload_sha256=record.draft.payload_sha256,
                valid_from=record.draft.valid_from,
                valid_to=record.draft.valid_to,
                confidence=record.confidence,
                as_of=snapshot.request.as_of,
            )
            handle = _mint_requirement_handle(
                factor_execution_id=execution.factor_execution_id,
                snapshot_id=snapshot.snapshot_id,
                demand=cell.demand,
                input_id=input_id,
                runner_id=runner_id,
                runner_version=runner_version,
                runner_implementation_sha256=runner_implementation_sha256,
            )
            bindings.append(
                RunnerInputBinding(
                    handle=handle,
                    demand=cell.demand,
                    input_id=input_id,
                    observation=observation,
                    evidence_status=(
                        InputEvidenceStatus.VERIFIED if record.confidence > 0 else InputEvidenceStatus.REJECTED
                    ),
                )
            )
    return RunnerInputSelection(
        factor_execution_id=execution.factor_execution_id,
        snapshot_id=snapshot.snapshot_id,
        bindings=tuple(bindings),
        selected_at=selected_at,
        runner_id=runner_id,
        runner_version=runner_version,
        runner_implementation_sha256=runner_implementation_sha256,
    )


class InputReadEvent(BaseModel):
    """Idempotent event emitted by the runner when factor code reads one input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_read_event_id: str = ""
    content_sha256: str = ""
    factor_execution_id: str = Field(pattern=r"^factor-execution:[0-9a-f]{64}$")
    selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    requirement_handle_id: str = Field(pattern=r"^requirement-handle:[0-9a-f]{64}$")
    output_key: str = Field(pattern=_STABLE_KEY)
    read_index: int = Field(ge=0)
    trace_id: str = Field(pattern=_STABLE_KEY)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> InputReadEvent:
        _identify(self, id_field="input_read_event_id", hash_field="content_sha256", prefix="input-read")
        return self


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    EXCLUDED = "excluded"
    LOW_CONFIDENCE = "low_confidence"
    ERROR = "error"


class FactorValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_EVALUATED = "not_evaluated"


class FactorOutputDraft(BaseModel):
    """Factor return value; lineage and confidence are deliberately absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_key: str = Field(pattern=_STABLE_KEY)
    subject: SubjectRef
    output_model_key: str = Field(pattern=_STABLE_KEY)
    output_schema_sha256: str = Field(pattern=_SHA256)
    output_payload_sha256: str = Field(pattern=_SHA256)
    availability_status: AvailabilityStatus
    factor_validation_status: FactorValidationStatus
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> FactorOutputDraft:
        reasons = tuple(sorted(set(self.reason_codes)))
        if self.availability_status is AvailabilityStatus.AVAILABLE and reasons:
            raise ValueError("available output cannot carry unavailability reasons")
        if self.availability_status is not AvailabilityStatus.AVAILABLE and not reasons:
            raise ValueError("non-available output requires explicit reason codes")
        object.__setattr__(self, "reason_codes", reasons)
        return self


class InputConsumptionLineage(BaseModel):
    """Runner-derived demand/read lineage for one actually consumed capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_lineage_id: str = ""
    content_sha256: str = ""
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    planned_cell_id: str = Field(pattern=r"^planned-demand-cell:[0-9a-f]{64}$")
    requirement_handle_id: str = Field(pattern=r"^requirement-handle:[0-9a-f]{64}$")
    input_id: str = Field(pattern=r"^(?:normalized-record|factor-output):[0-9a-f]{64}$")
    evidence_status: InputEvidenceStatus
    input_read_event_ids: tuple[str, ...] = Field(min_length=1)
    trace_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> InputConsumptionLineage:
        read_ids = tuple(sorted(self.input_read_event_ids))
        trace_ids = tuple(sorted(self.trace_ids))
        if len(read_ids) != len(set(read_ids)) or len(trace_ids) != len(set(trace_ids)):
            raise ValueError("input lineage read and trace IDs must be unique")
        object.__setattr__(self, "input_read_event_ids", read_ids)
        object.__setattr__(self, "trace_ids", trace_ids)
        _identify(self, id_field="input_lineage_id", hash_field="content_sha256", prefix="input-lineage")
        return self


class MaterializedFactorOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    materialized_output_id: str = ""
    content_sha256: str = ""
    factor_execution_id: str = Field(pattern=r"^factor-execution:[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    draft: FactorOutputDraft
    input_lineage: tuple[InputConsumptionLineage, ...]
    upstream_output_ids: tuple[str, ...] = ()
    minimum_consumed_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    trace_complete: bool = False
    materialized_at: datetime

    @field_validator("materialized_at")
    @classmethod
    def validate_materialized_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "materialized_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> MaterializedFactorOutput:
        lineage = tuple(sorted(self.input_lineage, key=lambda item: item.input_lineage_id))
        if len({item.input_lineage_id for item in lineage}) != len(lineage):
            raise ValueError("materialized input lineage rows must be unique")
        consumed = tuple(sorted(item.input_id for item in lineage))
        reads = tuple(sorted({read_id for item in lineage for read_id in item.input_read_event_ids}))
        expected_upstream = tuple(sorted(item for item in consumed if item.startswith("factor-output:")))
        upstream = tuple(sorted(set(self.upstream_output_ids)))
        if upstream != expected_upstream:
            raise ValueError("upstream links must be derived exactly from consumed capability reads")
        if self.draft.availability_status is AvailabilityStatus.AVAILABLE:
            if not lineage:
                raise ValueError("available output requires runner-derived input or upstream lineage")
            if not reads:
                raise ValueError("consumed inputs require runner-owned read events")
            if any(item.evidence_status is InputEvidenceStatus.REJECTED for item in lineage):
                raise ValueError("available output cannot consume rejected evidence")
            if not self.trace_complete:
                raise ValueError("available output cannot be materialized with incomplete trace")
        elif self.trace_complete:
            raise ValueError("unavailable output cannot claim a complete successful trace")
        if bool(lineage) != (self.minimum_consumed_confidence is not None):
            raise ValueError("minimum confidence must be derived exactly when inputs were consumed")
        object.__setattr__(self, "input_lineage", lineage)
        object.__setattr__(self, "upstream_output_ids", upstream)
        _identify(self, id_field="materialized_output_id", hash_field="content_sha256", prefix="factor-output")
        return self

    @property
    def consumed_input_ids(self) -> tuple[str, ...]:
        return tuple(sorted(item.input_id for item in self.input_lineage))

    @property
    def input_read_event_ids(self) -> tuple[str, ...]:
        return tuple(sorted({read_id for item in self.input_lineage for read_id in item.input_read_event_ids}))


def materialize_factor_output(
    *,
    execution: FactorExecution,
    selection: RunnerInputSelection,
    draft: FactorOutputDraft,
    read_events: tuple[InputReadEvent, ...],
    materialized_at: datetime,
) -> MaterializedFactorOutput:
    """Derive lineage/confidence from runner evidence, never from factor claims."""

    if selection.factor_execution_id != execution.factor_execution_id:
        raise ValueError("runner selection belongs to another factor execution")
    if selection.snapshot_id != execution.snapshot_id:
        raise ValueError("runner selection belongs to another snapshot")
    binding_map = {item.handle.requirement_handle_id: item for item in selection.bindings}
    if any(event.output_key != draft.output_key for event in read_events):
        raise ValueError("read evidence for another output cannot be mixed into this materialization")
    relevant = read_events
    if any(
        event.factor_execution_id != execution.factor_execution_id
        or event.selection_id != selection.selection_id
        or event.requirement_handle_id not in binding_map
        for event in relevant
    ):
        raise ValueError("input-read evidence is not attributable to the exact execution selection")
    read_positions = [(event.requirement_handle_id, event.read_index) for event in relevant]
    if len(read_positions) != len(set(read_positions)):
        raise ValueError("input-read evidence contains duplicate read positions")
    consumed_handles = tuple(sorted({event.requirement_handle_id for event in relevant}))
    consumed_bindings = tuple(binding_map[handle_id] for handle_id in consumed_handles)
    undeclared = {
        binding.demand.requirement_id
        for binding in consumed_bindings
        if binding.demand.requirement_id not in execution.template.data_requirement_ids
    }
    if undeclared:
        raise ValueError(f"factor read undeclared data requirements: {sorted(undeclared)}")
    lineage = tuple(
        InputConsumptionLineage(
            requirement_id=binding.demand.requirement_id,
            capture_requirement_id=binding.demand.capture_requirement_id,
            planned_cell_id=binding.demand.planned_cell_id,
            requirement_handle_id=binding.handle.requirement_handle_id,
            input_id=binding.input_id,
            evidence_status=binding.evidence_status,
            input_read_event_ids=tuple(
                event.input_read_event_id
                for event in relevant
                if event.requirement_handle_id == binding.handle.requirement_handle_id
            ),
            trace_ids=tuple(
                sorted(
                    {
                        event.trace_id
                        for event in relevant
                        if event.requirement_handle_id == binding.handle.requirement_handle_id
                    }
                )
            ),
        )
        for binding in consumed_bindings
    )
    minimum_confidence = min(
        (binding.observation.confidence for binding in consumed_bindings),
        default=None,
    )

    upstream_ids = tuple(
        sorted(binding.input_id for binding in consumed_bindings if binding.input_id.startswith("factor-output:"))
    )
    if execution.template.factor_kind is FactorKind.BASE and upstream_ids:
        raise ValueError("base factor output cannot consume upstream factor outputs")
    if execution.template.factor_kind in {FactorKind.COMPOSITE, FactorKind.STRATEGY}:
        if not upstream_ids:
            raise ValueError("composite and strategy outputs require materialized upstream outputs")
        if any(
            binding.upstream_batch_id not in execution.upstream_batch_ids
            for binding in consumed_bindings
            if binding.input_id.startswith("factor-output:")
        ):
            raise ValueError("upstream factor reads are not attributable to an execution batch")
    trace_complete = draft.availability_status is AvailabilityStatus.AVAILABLE and bool(lineage)
    return MaterializedFactorOutput(
        factor_execution_id=execution.factor_execution_id,
        snapshot_id=execution.snapshot_id,
        draft=draft,
        input_lineage=lineage,
        upstream_output_ids=upstream_ids,
        minimum_consumed_confidence=minimum_confidence,
        trace_complete=trace_complete,
        materialized_at=materialized_at,
    )


class MaterializedFactorBatch(BaseModel):
    """Durable boundary that composites must reload instead of using memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    materialized_batch_id: str = ""
    content_sha256: str = ""
    factor_execution_id: str = Field(pattern=r"^factor-execution:[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    output_ids: tuple[str, ...] = Field(min_length=1)
    repository_commit_id: str = Field(pattern=_STABLE_KEY)
    persisted_at: datetime

    @field_validator("persisted_at")
    @classmethod
    def validate_persisted_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "persisted_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> MaterializedFactorBatch:
        outputs = tuple(sorted(set(self.output_ids)))
        object.__setattr__(self, "output_ids", outputs)
        _identify(self, id_field="materialized_batch_id", hash_field="content_sha256", prefix="factor-batch")
        return self


def build_runner_upstream_input_selection(
    *,
    execution: FactorExecution,
    demand: SnapshotDemandCell,
    upstream_batch: MaterializedFactorBatch,
    upstream_output: MaterializedFactorOutput,
    selected_at: datetime,
    runner_id: str,
    runner_version: str,
    runner_implementation_sha256: str,
) -> RunnerInputSelection:
    """Mint one sanitized capability from an exact persisted upstream output."""

    if execution.template.factor_kind is FactorKind.BASE:
        raise ValueError("base factors cannot select upstream factor outputs")
    if upstream_batch.materialized_batch_id not in execution.upstream_batch_ids:
        raise ValueError("upstream batch is not bound by the factor execution")
    if upstream_output.materialized_output_id not in upstream_batch.output_ids:
        raise ValueError("upstream output is absent from the persisted batch")
    if upstream_batch.snapshot_id != execution.snapshot_id or upstream_output.snapshot_id != execution.snapshot_id:
        raise ValueError("upstream input does not belong to the execution snapshot")
    if demand.requirement_id not in execution.template.data_requirement_ids:
        raise ValueError("upstream input is not bound to a declared data requirement")
    if (
        upstream_output.draft.availability_status is not AvailabilityStatus.AVAILABLE
        or not upstream_output.trace_complete
        or upstream_output.minimum_consumed_confidence is None
    ):
        raise ValueError("upstream input must be available with complete runner-derived evidence")
    observation = ProvenanceNeutralInput(
        subject=upstream_output.draft.subject,
        payload_model_key=upstream_output.draft.output_model_key,
        payload_sha256=upstream_output.draft.output_payload_sha256,
        valid_from=execution.started_at.date(),
        valid_to=execution.started_at.date(),
        confidence=upstream_output.minimum_consumed_confidence,
        as_of=execution.started_at,
    )
    handle = _mint_requirement_handle(
        factor_execution_id=execution.factor_execution_id,
        snapshot_id=execution.snapshot_id,
        demand=demand,
        input_id=upstream_output.materialized_output_id,
        runner_id=runner_id,
        runner_version=runner_version,
        runner_implementation_sha256=runner_implementation_sha256,
    )
    return RunnerInputSelection(
        factor_execution_id=execution.factor_execution_id,
        snapshot_id=execution.snapshot_id,
        bindings=(
            RunnerInputBinding(
                handle=handle,
                demand=demand,
                input_id=upstream_output.materialized_output_id,
                observation=observation,
                evidence_status=InputEvidenceStatus.VERIFIED,
                upstream_batch_id=upstream_batch.materialized_batch_id,
            ),
        ),
        selected_at=selected_at,
        runner_id=runner_id,
        runner_version=runner_version,
        runner_implementation_sha256=runner_implementation_sha256,
    )


class DecisionSnapshot(BaseModel):
    """Only evidence knowable at the decision cutoff may enter this object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_snapshot_id: str = ""
    content_sha256: str = ""
    universe: UniverseRef
    snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=_SHA256)
    strategy_template_id: str = Field(pattern=r"^factor-template:[0-9a-f]{64}$")
    strategy_execution_id: str = Field(pattern=r"^factor-execution:[0-9a-f]{64}$")
    materialized_batch_ids: tuple[str, ...] = Field(min_length=1)
    decision_output_ids: tuple[str, ...] = Field(min_length=1)
    cutoff: datetime
    valid_on: date
    created_at: datetime

    @field_validator("cutoff", "created_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> DecisionSnapshot:
        if self.created_at < self.cutoff:
            raise ValueError("decision snapshot cannot be created before its cutoff")
        object.__setattr__(self, "materialized_batch_ids", tuple(sorted(set(self.materialized_batch_ids))))
        object.__setattr__(self, "decision_output_ids", tuple(sorted(set(self.decision_output_ids))))
        _identify(self, id_field="decision_snapshot_id", hash_field="content_sha256", prefix="decision-snapshot")
        return self


class SimulationEventKind(StrEnum):
    UNADJUSTED_PRICE = "unadjusted_price"
    CORPORATE_ACTION = "corporate_action"
    FX_FIXING = "fx_fixing"
    MARKET_SESSION = "market_session"


class SimulationEvent(BaseModel):
    """Monotonic future event delivered by the simulation clock after decisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    simulation_event_id: str = ""
    content_sha256: str = ""
    kind: SimulationEventKind
    subject: SubjectRef
    source_record_id: str = Field(pattern=_CONTENT_ID)
    payload_schema_sha256: str = Field(pattern=_SHA256)
    payload_sha256: str = Field(pattern=_SHA256)
    event_at: datetime
    knowable_at: datetime
    recorded_at: datetime

    @field_validator("event_at", "knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> SimulationEvent:
        if self.recorded_at < self.knowable_at:
            raise ValueError("simulation event cannot be recorded before it is knowable")
        _identify(self, id_field="simulation_event_id", hash_field="content_sha256", prefix="simulation-event")
        return self


class ReplayEventStream(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: DecisionSnapshot
    events: tuple[SimulationEvent, ...]

    @model_validator(mode="after")
    def validate_monotonic_future_stream(self) -> ReplayEventStream:
        events = tuple(
            sorted(self.events, key=lambda item: (item.knowable_at, item.event_at, item.simulation_event_id))
        )
        if len({item.simulation_event_id for item in events}) != len(events):
            raise ValueError("simulation event stream contains duplicates")
        if any(item.knowable_at <= self.decision.cutoff for item in events):
            raise ValueError("evidence knowable at the decision cutoff belongs in the PIT snapshot")
        object.__setattr__(self, "events", events)
        return self


class TraceNodeKind(StrEnum):
    STRATEGY_RUN = "strategy_run"
    DECISION = "decision"
    TRADE = "trade"
    METRIC = "metric"
    FACTOR_OUTPUT = "factor_output"
    FACTOR_EXECUTION = "factor_execution"
    SNAPSHOT = "snapshot"
    NORMALIZED_RECORD = "normalized_record"
    RAW_OBJECT = "raw_object"
    QUALITY_EVIDENCE = "quality_evidence"


class TraceNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    kind: TraceNodeKind
    content_sha256: str = Field(pattern=_SHA256)


class TraceEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    downstream_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    relation: str = Field(pattern=_STABLE_KEY)


class TraceBundle(BaseModel):
    """Self-contained reverse lineage projection with no strategy-supplied edges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_bundle_id: str = ""
    content_sha256: str = ""
    root_node_id: str = Field(min_length=1)
    nodes: tuple[TraceNode, ...] = Field(min_length=1)
    edges: tuple[TraceEdge, ...] = Field(min_length=1)
    built_by: str = Field(pattern=_STABLE_KEY)
    builder_version: str = Field(pattern=_STABLE_KEY)
    builder_implementation_sha256: str = Field(pattern=_SHA256)
    built_at: datetime

    @field_validator("built_at")
    @classmethod
    def validate_built_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "built_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> TraceBundle:
        nodes = tuple(sorted(self.nodes, key=lambda item: item.node_id))
        if len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("trace nodes must be unique")
        node_ids = {item.node_id for item in nodes}
        if self.root_node_id not in node_ids:
            raise ValueError("trace root node is missing")
        edges = tuple(sorted(set(self.edges), key=lambda item: (item.downstream_id, item.upstream_id, item.relation)))
        if any(edge.downstream_id not in node_ids or edge.upstream_id not in node_ids for edge in edges):
            raise ValueError("trace edges must reference bundled nodes")
        if any(edge.downstream_id == edge.upstream_id for edge in edges):
            raise ValueError("trace cannot contain self edges")
        reachable = {self.root_node_id}
        changed = True
        while changed:
            changed = False
            for edge in edges:
                if edge.downstream_id in reachable and edge.upstream_id not in reachable:
                    reachable.add(edge.upstream_id)
                    changed = True
        if reachable != node_ids:
            raise ValueError("trace contains nodes without a reverse path from the requested root")
        required_kinds = {
            TraceNodeKind.FACTOR_EXECUTION,
            TraceNodeKind.SNAPSHOT,
            TraceNodeKind.NORMALIZED_RECORD,
            TraceNodeKind.RAW_OBJECT,
            TraceNodeKind.QUALITY_EVIDENCE,
        }
        kinds = {item.kind for item in nodes}
        missing = required_kinds - kinds
        if missing:
            raise ValueError(f"reverse trace is incomplete: {sorted(item.value for item in missing)}")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        _identify(self, id_field="trace_bundle_id", hash_field="content_sha256", prefix="trace-bundle")
        return self


class TraceRepository(Protocol):
    def reverse_trace(self, root_node_id: str) -> TraceBundle | None: ...


__all__ = [
    "AvailabilityStatus",
    "DecisionSnapshot",
    "DependencyTemplate",
    "ExtractionInvocation",
    "ExtractionTemplate",
    "FactorExecution",
    "FactorInputCapability",
    "FactorInvocationTemplate",
    "FactorKind",
    "FactorOutputDraft",
    "FactorValidationStatus",
    "InputReadEvent",
    "InputConsumptionLineage",
    "InputEvidenceStatus",
    "MaterializedFactorBatch",
    "MaterializedFactorOutput",
    "ModelRevisionRef",
    "NormalizedRecordRef",
    "PolicyBinding",
    "PolicyRole",
    "ProvenanceNeutralInput",
    "ReplayEventStream",
    "RequirementHandle",
    "RunnerInputBinding",
    "RunnerInputSelection",
    "SemanticDraft",
    "SemanticProducerKind",
    "SimulationEvent",
    "SimulationEventKind",
    "SnapshotCellSelection",
    "SnapshotDemandCell",
    "SnapshotManifest",
    "SnapshotRepository",
    "SnapshotRequest",
    "TraceBundle",
    "TraceEdge",
    "TraceNode",
    "TraceNodeKind",
    "TraceRepository",
    "build_runner_input_selection",
    "build_runner_upstream_input_selection",
    "materialize_factor_output",
    "validate_extraction_replay",
]
