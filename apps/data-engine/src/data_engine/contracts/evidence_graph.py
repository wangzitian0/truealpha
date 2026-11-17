"""Backend-neutral evidence-graph contracts (ADR A1)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator
from truealpha_contracts.common import (
    DEFAULT_MUTABLE_TOKENS,
    CaptureEnvironment,
    ContentHash,
    FrozenModel,
    IdentifiedFrozenModel,
    Sha256,
    StableId,
    content_id,
)


def _reject_mutable_version(value: str, field_name: str) -> str:
    if {token for token in re.split(r"[._:/@+-]", value.lower()) if token} & DEFAULT_MUTABLE_TOKENS:
        raise ValueError(f"{field_name} must be immutable")
    return value


class EvidenceNodeKind(StrEnum):
    RAW_FETCH = "raw_fetch"
    SOURCE_VINTAGE = "source_vintage"
    NORMALIZED_OBSERVATION = "normalized_observation"
    SNAPSHOT = "snapshot"
    FACTOR_INVOCATION = "factor_invocation"
    MATERIALIZED_RESULT = "materialized_result"
    CAPTURE_RUN = "capture_run"
    OBLIGATION = "obligation"
    QUALITY_CELL = "quality_cell"
    RELEASE_MANIFEST = "release_manifest"


class EvidenceRelation(StrEnum):
    DERIVED_FROM = "derived_from"
    SELECTED_FROM = "selected_from"
    MEMBER_OF = "member_of"
    BOUND_TO = "bound_to"
    ATTESTED_BY = "attested_by"
    SUPERSEDES = "supersedes"


_KIND_PREFIX: dict[EvidenceNodeKind, str] = {
    EvidenceNodeKind.RAW_FETCH: "raw-fetch",
    EvidenceNodeKind.SOURCE_VINTAGE: "source-vintage",
    EvidenceNodeKind.NORMALIZED_OBSERVATION: "normalized-observation",
    EvidenceNodeKind.SNAPSHOT: "snapshot",
    EvidenceNodeKind.FACTOR_INVOCATION: "factor-invocation",
    EvidenceNodeKind.MATERIALIZED_RESULT: "materialized-result",
    EvidenceNodeKind.CAPTURE_RUN: "capture-run",
    EvidenceNodeKind.OBLIGATION: "capture-obligation",
    EvidenceNodeKind.QUALITY_CELL: "datahub-quality-cell",
    EvidenceNodeKind.RELEASE_MANIFEST: "release-manifest",
}


class EvidenceNodeRef(FrozenModel):
    kind: EvidenceNodeKind
    node_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_prefix(self) -> EvidenceNodeRef:
        prefix = _KIND_PREFIX[self.kind]
        if not re.fullmatch(rf"{re.escape(prefix)}:[0-9a-f]{{64}}", self.node_id):
            raise ValueError(f"{self.kind} node_id must match {prefix}:<sha256>")
        return self

    @property
    def content_sha256(self) -> str:
        return self.node_id.split(":", 1)[1]


class BitemporalStamp(FrozenModel):
    valid_from: date
    valid_to: date | None = None
    transaction_time: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def check_period(self) -> BitemporalStamp:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        return self


class EvidenceNode(FrozenModel):
    ref: EvidenceNodeRef
    content_sha256: Sha256
    stamp: BitemporalStamp
    supersedes: EvidenceNodeRef | None = None

    @model_validator(mode="after")
    def check_integrity(self) -> EvidenceNode:
        if self.ref.content_sha256 != self.content_sha256:
            raise ValueError("content_sha256 integrity column does not match the node id")
        if self.supersedes is not None and (
            self.supersedes.kind != self.ref.kind or self.supersedes.node_id == self.ref.node_id
        ):
            raise ValueError("invalid supersedes target on EvidenceNode")
        return self


class EvidenceEdge(IdentifiedFrozenModel):
    id_field, id_prefix = "edge_id", "evidence-edge"
    edge_id: str = content_id("evidence-edge")
    content_sha256: ContentHash = ""
    from_ref: EvidenceNodeRef
    to_ref: EvidenceNodeRef
    relation: EvidenceRelation
    stamp: BitemporalStamp

    def _canonicalize(self) -> None:
        if self.from_ref == self.to_ref or (
            self.relation is EvidenceRelation.SUPERSEDES and self.from_ref.kind != self.to_ref.kind
        ):
            raise ValueError("invalid EvidenceEdge")


class CurrentPointerKey(FrozenModel):
    environment: CaptureEnvironment
    universe_id: StableId
    universe_version: StableId
    factor_id: StableId

    @field_validator("universe_version", "factor_id")
    @classmethod
    def reject_mutable(cls, value: str) -> str:
        return _reject_mutable_version(value, "pointer scope identities")


class CurrentPointer(IdentifiedFrozenModel):
    id_field, id_prefix = "pointer_id", "current-pointer"
    pointer_id: str = content_id("current-pointer")
    content_sha256: ContentHash = ""
    key: CurrentPointerKey
    target_run: EvidenceNodeRef
    sequence: int = Field(ge=0)
    previous_run: EvidenceNodeRef | None = None
    advanced_at: datetime

    def _canonicalize(self) -> None:
        if self.target_run.kind is not EvidenceNodeKind.CAPTURE_RUN:
            raise ValueError("target_run must be a capture-run node")
        if self.previous_run is not None:
            if (
                self.previous_run.kind is not EvidenceNodeKind.CAPTURE_RUN
                or self.previous_run.node_id == self.target_run.node_id
                or self.sequence == 0
            ):
                raise ValueError("invalid previous_run for CurrentPointer")
        elif self.sequence != 0:
            raise ValueError("a later advance must name the previous run it supersedes")


class ProvenanceClosure(FrozenModel):
    root: EvidenceNodeRef
    reverse: bool = False
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]
    truncated: bool = False

    @model_validator(mode="after")
    def check_shape(self) -> ProvenanceClosure:
        refs = {node.ref for node in self.nodes}
        if self.root not in refs or any(edge.from_ref not in refs or edge.to_ref not in refs for edge in self.edges):
            raise ValueError("ProvenanceClosure root/edges must reference present nodes")
        return self


@runtime_checkable
class EvidenceGraphWriter(Protocol):
    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None: ...
