"""Immutable contracts for list-oriented capture control."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.common import identify_by_grain as _freeze_wrapped
from truealpha_contracts.datahub import ListObligation
from truealpha_contracts.universe import SubjectRef, UniverseRef


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


__all__ = [
    "CaptureListObligation",
    "CaptureListVersion",
    "CaptureObligationWorkBinding",
]
