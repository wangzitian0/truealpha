"""Backend-neutral evidence-graph contracts (ADR A1)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import (
    CaptureEnvironment,
    ContentHash,
    FrozenModel,
    IdentifiedFrozenModel,
    Sha256,
    StableId,
    content_id,
    require_checks,
    require_stable_and_immutable,
)


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
        require_checks(
            (
                self.ref.content_sha256 != self.content_sha256,
                "content_sha256 integrity column does not match the node id",
            ),
            (
                self.supersedes is not None and self.supersedes.kind != self.ref.kind,
                "a restatement supersedes a node of the same kind",
            ),
            (
                self.supersedes is not None and self.supersedes.node_id == self.ref.node_id,
                "a node cannot supersede itself",
            ),
        )
        return self


class EvidenceEdge(IdentifiedFrozenModel):
    id_field = "edge_id"
    id_prefix = "evidence-edge"
    edge_id: str = content_id("evidence-edge")
    content_sha256: ContentHash = ""
    from_ref: EvidenceNodeRef
    to_ref: EvidenceNodeRef
    relation: EvidenceRelation
    stamp: BitemporalStamp

    def _canonicalize(self) -> None:
        require_checks(
            (self.from_ref == self.to_ref, "an evidence edge cannot connect a node to itself"),
            (
                self.relation is EvidenceRelation.SUPERSEDES and self.from_ref.kind != self.to_ref.kind,
                "supersedes connects nodes of the same kind",
            ),
        )


class CurrentPointerKey(FrozenModel):
    environment: CaptureEnvironment
    universe_id: StableId
    universe_version: StableId
    factor_id: StableId

    @field_validator("universe_version", "factor_id")
    @classmethod
    def reject_mutable(cls, value: str) -> str:
        return require_stable_and_immutable(value, "pointer scope identities", pattern=None)


class CurrentPointer(IdentifiedFrozenModel):
    id_field = "pointer_id"
    id_prefix = "current-pointer"
    pointer_id: str = content_id("current-pointer")
    content_sha256: ContentHash = ""
    key: CurrentPointerKey
    target_run: EvidenceNodeRef
    sequence: int = Field(ge=0)
    previous_run: EvidenceNodeRef | None = None
    advanced_at: datetime

    def _canonicalize(self) -> None:
        require_checks(
            (
                self.target_run.kind is not EvidenceNodeKind.CAPTURE_RUN,
                "a current pointer resolves to a capture-run node",
            ),
            (
                self.previous_run is not None and self.previous_run.kind is not EvidenceNodeKind.CAPTURE_RUN,
                "previous_run must be a capture-run node",
            ),
            (
                self.previous_run is not None and self.previous_run.node_id == self.target_run.node_id,
                "a pointer advance must change the target run",
            ),
            (
                self.previous_run is not None and self.sequence == 0,
                "the first pointer at sequence 0 has no previous run",
            ),
            (
                self.previous_run is None and self.sequence != 0,
                "a later advance must name the previous run it supersedes",
            ),
        )


class ProvenanceClosure(FrozenModel):
    root: EvidenceNodeRef
    reverse: bool = False
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]
    truncated: bool = False

    @model_validator(mode="after")
    def check_shape(self) -> ProvenanceClosure:
        refs = {node.ref for node in self.nodes}
        if self.root not in refs:
            raise ValueError("the closure must contain its root node")
        if any(edge.from_ref not in refs or edge.to_ref not in refs for edge in self.edges):
            raise ValueError("every closure edge must connect nodes present in the closure")
        return self


@runtime_checkable
class EvidenceGraphWriter(Protocol):
    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None: ...


@runtime_checkable
class CurrentPointerRegistry(Protocol):
    def head(self, key: CurrentPointerKey) -> CurrentPointer | None: ...
    def advance(self, pointer: CurrentPointer) -> CurrentPointer: ...
