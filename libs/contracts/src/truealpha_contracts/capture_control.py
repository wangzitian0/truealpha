"""Immutable contracts for list-oriented capture control."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts import SubjectRef, UniverseRef, canonical_sha256
from truealpha_contracts.datahub import ListObligation, RecapturePredicate


def _freeze(model: BaseModel, *, id_field: str, prefix: str, identity_fields: tuple[str, ...]) -> None:
    identity = model.model_dump(mode="json", include=set(identity_fields))
    expected_id = f"{prefix}:{canonical_sha256(identity)}"
    content = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_content = canonical_sha256(content)
    if getattr(model, id_field) not in {"", expected_id}:
        raise ValueError(f"{id_field} does not match canonical identity")
    if getattr(model, "content_sha256") not in {"", expected_content}:
        raise ValueError("content_sha256 does not match canonical content")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", expected_content)


def _freeze_wrapped(model: BaseModel, *, id_field: str, prefix: str, identity_fields: tuple[str, ...]) -> None:
    identity = model.model_dump(mode="json", include=set(identity_fields))
    expected_id = f"{prefix}:{canonical_sha256({'kind': prefix, 'identity': identity})}"
    content = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_content = canonical_sha256(content)
    if getattr(model, id_field) not in {"", expected_id}:
        raise ValueError(f"{id_field} does not match canonical identity")
    if getattr(model, "content_sha256") not in {"", expected_content}:
        raise ValueError("content_sha256 does not match canonical content")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", expected_content)


class CheckpointPhase(StrEnum):
    PLANNED = "planned"
    RAW_LANDED = "raw_landed"
    NORMALIZED = "normalized"
    MANIFEST_PERSISTED = "manifest_persisted"


class CaptureListVersion(BaseModel):
    """One content-addressed list version; mutable aliases are never accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    list_version_id: str = Field(default="", pattern=r"^(?:|list-version:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    universe: UniverseRef
    members: tuple[SubjectRef, ...] = Field(min_length=1)
    effective_at: datetime

    @field_validator("effective_at")
    @classmethod
    def aware_effective_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> Self:
        members = tuple(sorted(self.members, key=lambda member: (member.kind.value, member.id)))
        if len(members) != len(set(members)):
            raise ValueError("members must not contain duplicates")
        object.__setattr__(self, "members", members)
        _freeze(
            self,
            id_field="list_version_id",
            prefix="list-version",
            identity_fields=("universe", "members", "effective_at"),
        )
        return self


class CaptureListObligation(BaseModel):
    """A D4 obligation bound to the exact list version that created it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    obligation_id: str = Field(default="", pattern=r"^(?:|capture-list-obligation:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    list_version_id: str = Field(pattern=r"^list-version:[0-9a-f]{64}$")
    obligation: ListObligation

    @model_validator(mode="after")
    def identify(self) -> Self:
        _freeze(
            self,
            id_field="obligation_id",
            prefix="capture-list-obligation",
            identity_fields=("list_version_id", "obligation"),
        )
        return self

    @property
    def run_id(self) -> str:
        return self.obligation.run_id

    @property
    def universe_ref(self) -> UniverseRef:
        return self.obligation.universe_ref

    @property
    def subject(self) -> SubjectRef:
        return self.obligation.subject

    @property
    def capture_requirement_id(self) -> str:
        return self.obligation.capture_requirement_id

    @property
    def partition(self) -> str:
        return self.obligation.partition


class CaptureObligationWorkBinding(BaseModel):
    """Bind one D5 list-bound obligation to one campaign work item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str = Field(default="", pattern=r"^(?:|capture-obligation-work-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    obligation_id: str = Field(pattern=r"^capture-list-obligation:[0-9a-f]{64}$")
    work_item_id: str = Field(pattern=r"^capture-work-item:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identify(self) -> Self:
        _freeze_wrapped(
            self,
            id_field="binding_id",
            prefix="capture-obligation-work-binding",
            identity_fields=("obligation_id", "work_item_id"),
        )
        return self


class CaptureCheckpoint(BaseModel):
    """One append-only resume checkpoint for a capture run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str = Field(default="", pattern=r"^(?:|capture-checkpoint:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=r"^capture-run:[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    phase: CheckpointPhase
    completed_obligation_ids: tuple[str, ...]
    recorded_at: datetime

    @field_validator("completed_obligation_ids")
    @classmethod
    def canonical_obligations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("completed_obligation_ids must not contain duplicates")
        if any(re.fullmatch(r"capture-list-obligation:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("completed_obligation_ids must use canonical identities")
        return tuple(sorted(values))

    @field_validator("recorded_at")
    @classmethod
    def aware_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> Self:
        _freeze(
            self,
            id_field="checkpoint_id",
            prefix="capture-checkpoint",
            identity_fields=("run_id", "sequence"),
        )
        return self


class CaptureRecapturePlan(BaseModel):
    """A bounded D5 dry run over list-bound obligation identities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(default="", pattern=r"^(?:|capture-list-recapture-plan:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    selection_cutoff: datetime
    predicate: RecapturePredicate
    selected_obligation_ids: tuple[str, ...] = Field(min_length=1)
    planner_version: str

    @field_validator("planner_version")
    @classmethod
    def immutable_planner_version(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*", value) is None:
            raise ValueError("planner_version must be a stable coordinate")
        mutable_tokens = {"latest", "current", "default", "stable", "main", "head", "tip"}
        if any(token in mutable_tokens for token in re.split(r"[._:/@+\-]", value.lower())):
            raise ValueError("planner_version must not be mutable")
        return value

    @field_validator("selection_cutoff")
    @classmethod
    def aware_selection_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection_cutoff must be timezone-aware")
        return value

    @field_validator("selected_obligation_ids")
    @classmethod
    def canonical_selection(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("selected_obligation_ids must not contain duplicates")
        if any(re.fullmatch(r"capture-list-obligation:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("selected_obligation_ids must use canonical identities")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def identify(self) -> Self:
        if not self.predicate:
            raise ValueError("predicate must not be empty")
        _freeze_wrapped(
            self,
            id_field="plan_id",
            prefix="capture-list-recapture-plan",
            identity_fields=("selection_cutoff", "predicate", "selected_obligation_ids", "planner_version"),
        )
        return self
