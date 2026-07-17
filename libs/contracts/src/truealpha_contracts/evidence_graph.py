"""Backend-neutral evidence-graph contracts (ADR A1).

The evidence chain — provenance, lineage, and point-in-time validity — is modeled here as a
storage-neutral graph of nodes and typed edges with bitemporal stamps. Postgres/RDS today
and a future graph database are adapters over this contract; factors and consumers depend
only on these DTOs and the ports below, never on a backend.

Content hashes are integrity columns, not the reference key. A node is referenced by a
typed surrogate identity (`kind` + the underlying artifact's content-addressed id), its
`content_sha256` is the integrity column matching that id, and downstream discovery resolves
a governed `CurrentPointer` rather than a hand-carried hash tuple. Restatements append a new
node/edge with a `supersedes` link; history is never rewritten in place.

See `docs/architecture-decisions/A1-evidence-chain-in-database.md`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import CaptureEnvironment, canonical_sha256

_SHA256 = r"^[0-9a-f]{64}$"
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


def _reject_mutable_version(value: str, field_name: str) -> str:
    tokens = {token for token in re.split(r"[._:/@+-]", value.lower()) if token}
    if tokens & _MUTABLE_VERSION_TOKENS:
        raise ValueError(f"{field_name} must be immutable")
    return value


class EvidenceNodeKind(StrEnum):
    """The provenance-graph node kinds. Each maps to an existing content-addressed id."""

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
    """Typed provenance edges between nodes."""

    DERIVED_FROM = "derived_from"
    SELECTED_FROM = "selected_from"
    MEMBER_OF = "member_of"
    BOUND_TO = "bound_to"
    ATTESTED_BY = "attested_by"
    SUPERSEDES = "supersedes"


# The content-addressed id prefix each node kind reuses from the existing #58 contracts.
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


class EvidenceNodeRef(BaseModel):
    """A typed reference to an evidence node by its content-addressed identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class BitemporalStamp(BaseModel):
    """Valid time (what the fact describes) versus transaction time (when it was knowable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid_from: date
    valid_to: date | None = None
    # Knowable-at, taken from a source property; never an insertion clock.
    transaction_time: datetime
    # Ingestion audit only.
    recorded_at: datetime

    @model_validator(mode="after")
    def check_period(self) -> BitemporalStamp:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        return self


class EvidenceNode(BaseModel):
    """One append-only node. `content_sha256` is the integrity column matching `ref`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: EvidenceNodeRef
    content_sha256: str = Field(pattern=_SHA256)
    stamp: BitemporalStamp
    supersedes: EvidenceNodeRef | None = None

    @model_validator(mode="after")
    def check_integrity(self) -> EvidenceNode:
        if self.ref.content_sha256 != self.content_sha256:
            raise ValueError("content_sha256 integrity column does not match the node id")
        if self.supersedes is not None:
            if self.supersedes.kind != self.ref.kind:
                raise ValueError("a restatement supersedes a node of the same kind")
            if self.supersedes.node_id == self.ref.node_id:
                raise ValueError("a node cannot supersede itself")
        return self


class EvidenceEdge(BaseModel):
    """One append-only typed provenance edge, content-identified by its endpoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(default="", pattern=r"^(?:|evidence-edge:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    from_ref: EvidenceNodeRef
    to_ref: EvidenceNodeRef
    relation: EvidenceRelation
    stamp: BitemporalStamp

    @model_validator(mode="after")
    def freeze_and_identify(self) -> EvidenceEdge:
        if self.from_ref == self.to_ref:
            raise ValueError("an evidence edge cannot connect a node to itself")
        if self.relation is EvidenceRelation.SUPERSEDES and self.from_ref.kind != self.to_ref.kind:
            raise ValueError("supersedes connects nodes of the same kind")
        _identify(self, id_field="edge_id", hash_field="content_sha256", prefix="evidence-edge")
        return self


class CurrentPointerKey(BaseModel):
    """The governed head a consumer resolves: one pointer per environment/universe/factor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: CaptureEnvironment
    universe_id: str = Field(pattern=_STABLE_KEY)
    universe_version: str = Field(pattern=_STABLE_KEY)
    factor_id: str = Field(pattern=_STABLE_KEY)

    @field_validator("universe_version", "factor_id")
    @classmethod
    def reject_mutable(cls, value: str) -> str:
        return _reject_mutable_version(value, "pointer scope identities")


class CurrentPointer(BaseModel):
    """A governed head that resolves to an immutable exact run; advances forward only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pointer_id: str = Field(default="", pattern=r"^(?:|current-pointer:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    key: CurrentPointerKey
    # The immutable exact run this head resolves to.
    target_run: EvidenceNodeRef
    # Monotonic advance sequence; the registry rejects a non-increasing advance.
    sequence: int = Field(ge=0)
    previous_run: EvidenceNodeRef | None = None
    advanced_at: datetime

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CurrentPointer:
        if self.target_run.kind is not EvidenceNodeKind.CAPTURE_RUN:
            raise ValueError("a current pointer resolves to a capture-run node")
        if self.previous_run is not None:
            if self.previous_run.kind is not EvidenceNodeKind.CAPTURE_RUN:
                raise ValueError("previous_run must be a capture-run node")
            if self.previous_run.node_id == self.target_run.node_id:
                raise ValueError("a pointer advance must change the target run")
            if self.sequence == 0:
                raise ValueError("the first pointer at sequence 0 has no previous run")
        elif self.sequence != 0:
            raise ValueError("a later advance must name the previous run it supersedes")
        _identify(self, id_field="pointer_id", hash_field="content_sha256", prefix="current-pointer")
        return self


class ProvenanceClosure(BaseModel):
    """A bounded traversal from a root node to its provenance (or reverse)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        for edge in self.edges:
            if edge.from_ref not in refs or edge.to_ref not in refs:
                raise ValueError("every closure edge must connect nodes present in the closure")
        return self


@runtime_checkable
class EvidenceGraphWriter(Protocol):
    """Append-only, transactional writer. One call is one unit-of-work per run."""

    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None:
        """Atomically append nodes and edges. Re-appending identical content is idempotent;
        mutating or deleting existing rows is never permitted."""
        ...


@runtime_checkable
class EvidenceGraphReader(Protocol):
    """Bounded, read-only provenance access."""

    def resolve_pointer(self, key: CurrentPointerKey) -> CurrentPointer | None: ...

    def closure(self, root: EvidenceNodeRef, *, reverse: bool = False, max_nodes: int = 1000) -> ProvenanceClosure: ...


@runtime_checkable
class CurrentPointerRegistry(Protocol):
    """The governed head registry; advances are forward-only and append-only."""

    def head(self, key: CurrentPointerKey) -> CurrentPointer | None: ...

    def advance(self, pointer: CurrentPointer) -> CurrentPointer:
        """Record a forward advance. The adapter rejects a non-increasing sequence or a
        pointer whose `previous_run` does not match the current head."""
        ...
