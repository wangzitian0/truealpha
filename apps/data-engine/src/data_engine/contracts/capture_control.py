"""Immutable contracts for list-oriented capture control."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, model_validator
from truealpha_contracts.common import (
    AwareDatetime,
    ContentHash,
    FrozenModel,
    IdentifiedFrozenModel,
    canonical_sha256,
    content_id,
    content_ref,
)
from truealpha_contracts.universe import SubjectRef, UniverseRef

from data_engine.contracts.datahub import ListObligation


def _freeze(m: BaseModel, id_field: str, prefix: str, id_fields: tuple[str, ...]) -> None:
    eid = f"{prefix}:{canonical_sha256(m.model_dump(mode='json', include=set(id_fields)))}"
    eh = canonical_sha256(m.model_dump(mode="json", exclude={id_field, "content_sha256"}))
    if getattr(m, id_field) not in {"", eid} or getattr(m, "content_sha256") not in {"", eh}:
        raise ValueError(f"{id_field}/content_sha256 does not match canonical identity")
    object.__setattr__(m, id_field, eid)
    object.__setattr__(m, "content_sha256", eh)


class CaptureListVersion(FrozenModel):
    list_version_id: str = content_id("list-version")
    content_sha256: ContentHash = ""
    universe: UniverseRef
    members: tuple[SubjectRef, ...] = Field(min_length=1)
    effective_at: AwareDatetime

    @model_validator(mode="after")
    def identify(self) -> Self:
        members = tuple(sorted(self.members, key=lambda m: (m.kind.value, m.id)))
        if len(members) != len(set(members)):
            raise ValueError("members must not contain duplicates")
        object.__setattr__(self, "members", members)
        _freeze(self, "list_version_id", "list-version", ("universe", "members", "effective_at"))
        return self


class CaptureListObligation(FrozenModel):
    obligation_id: str = content_id("capture-list-obligation")
    content_sha256: ContentHash = ""
    list_version_id: str = content_ref("list-version")
    obligation: ListObligation

    @model_validator(mode="after")
    def identify(self) -> Self:
        _freeze(self, "obligation_id", "capture-list-obligation", ("list_version_id", "obligation"))
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


class CaptureObligationWorkBinding(IdentifiedFrozenModel):
    id_field, id_prefix = "binding_id", "capture-obligation-work-binding"
    identity_fields = ("obligation_id", "work_item_id")
    binding_id: str = content_id("capture-obligation-work-binding")
    content_sha256: ContentHash = ""
    obligation_id: str = content_ref("capture-list-obligation")
    work_item_id: str = content_ref("capture-work-item")


__all__ = ["CaptureListObligation", "CaptureListVersion", "CaptureObligationWorkBinding"]
