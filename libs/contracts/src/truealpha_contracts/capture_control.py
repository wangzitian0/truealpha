"""Immutable contracts for list-oriented capture control."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts import SubjectRef, UniverseRef, canonical_sha256


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
        if any(re.fullmatch(r"list-obligation:[0-9a-f]{64}", value) is None for value in values):
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
