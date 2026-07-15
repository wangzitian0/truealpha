"""Experimental DataHub control-plane and trust contracts.

The contracts separate logical identity from append-only record content. A
logical ID is derived only from its declared unique-key grain, while
``content_sha256`` covers the complete record. Reusing a logical ID with changed
content is therefore an explicit conflict rather than an implicit overwrite.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import SubjectRef, UniverseRef

_SHA256 = r"^[0-9a-f]{64}$"
_CONTENT_ID = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_STABLE_COORDINATE = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$"
_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})
_T = TypeVar("_T")


def _reject_mutable_coordinate(value: str, field_name: str) -> str:
    tokens = {token for token in re.split(r"[._:/@+\-]", value.lower()) if token}
    if tokens & _MUTABLE_TOKENS:
        raise ValueError(f"{field_name} must name an immutable version")
    return value


def _sorted_unique_strings(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool = True,
    immutable: bool = False,
) -> tuple[str, ...]:
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    for value in values:
        if re.fullmatch(_STABLE_COORDINATE, value) is None:
            raise ValueError(f"{field_name} must contain stable coordinates")
        if immutable:
            _reject_mutable_coordinate(value, field_name)
    return tuple(sorted(values))


def _freeze_identity(
    model: BaseModel,
    *,
    id_field: str,
    prefix: str,
    identity_fields: tuple[str, ...],
) -> None:
    identity = model.model_dump(mode="json", include=set(identity_fields))
    identity_sha256 = canonical_sha256({"kind": prefix, "identity": identity})
    expected_id = f"{prefix}:{identity_sha256}"
    content = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_content_sha256 = canonical_sha256(content)
    supplied_id = getattr(model, id_field)
    supplied_content_sha256 = getattr(model, "content_sha256")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match its declared identity grain")
    if supplied_content_sha256 and supplied_content_sha256 != expected_content_sha256:
        raise ValueError("content_sha256 does not match the complete canonical record")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", expected_content_sha256)


def _decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    return value


class FetchAttemptOutcome(StrEnum):
    RATE_LIMITED = "rate_limited"
    TRANSPORT_ERROR = "transport_error"
    SERVER_ERROR = "server_error"
    SUCCESS = "success"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ObligationTerminalState(StrEnum):
    SUCCESS = "success"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"
    SKIPPED_BY_POLICY = "skipped_by_policy"
    FAILED = "failed"


class AssessmentAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class AssessmentFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class AssessmentApplicability(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    NOT_YET_KNOWABLE = "not_yet_knowable"


class AssessmentQuality(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    CONFLICTED = "conflicted"
    INVALID = "invalid"
    NOT_ASSESSED = "not_assessed"


class ConfidenceComponentKind(StrEnum):
    SOURCE_AUTHORITY = "source_authority"
    PARSER_SCHEMA_VALIDITY = "parser_schema_validity"
    SEMANTIC_COMPLETENESS = "semantic_completeness"
    IDENTITY_RESOLUTION = "identity_resolution"
    PIT_KNOWABILITY = "pit_knowability"
    INDEPENDENT_RECONCILIATION = "independent_reconciliation"
    CONFLICT_STATE = "conflict_state"


class ProvenanceNodeKind(StrEnum):
    CAMPAIGN = "campaign"
    RUN = "run"
    LIST_OBLIGATION = "list_obligation"
    WORK_ITEM = "work_item"
    FETCH_ATTEMPT = "fetch_attempt"
    RAW_OBJECT = "raw_object"
    SOURCE_VINTAGE = "source_vintage"
    NORMALIZED_OBSERVATION = "normalized_observation"
    CONFIDENCE_ASSESSMENT = "confidence_assessment"
    SNAPSHOT = "snapshot"
    FACTOR_INPUT = "factor_input"
    MART_OUTPUT = "mart_output"


class ProvenanceEdgeKind(StrEnum):
    CONTAINS = "contains"
    REQUIRES = "requires"
    SATISFIED_BY = "satisfied_by"
    ATTEMPTED_BY = "attempted_by"
    OBSERVED = "observed"
    NORMALIZED_AS = "normalized_as"
    RECONCILES_WITH = "reconciles_with"
    SUPERSEDES = "supersedes"
    ASSESSED_BY = "assessed_by"
    SELECTED_INTO = "selected_into"
    PROJECTS = "projects"
    MATERIALIZES = "materializes"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(ge=1, le=20)
    retryable_outcomes: tuple[FetchAttemptOutcome, ...] = Field(min_length=1)
    terminal_outcomes: tuple[FetchAttemptOutcome, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_outcome_partition(self) -> Self:
        retryable = tuple(sorted(set(self.retryable_outcomes), key=lambda value: value.value))
        terminal = tuple(sorted(set(self.terminal_outcomes), key=lambda value: value.value))
        if len(retryable) != len(self.retryable_outcomes):
            raise ValueError("retryable_outcomes must not contain duplicates")
        if len(terminal) != len(self.terminal_outcomes):
            raise ValueError("terminal_outcomes must not contain duplicates")
        if set(retryable) & set(terminal):
            raise ValueError("retryable and terminal outcomes must be disjoint")
        required_terminal = {
            FetchAttemptOutcome.SUCCESS,
            FetchAttemptOutcome.UNCHANGED,
            FetchAttemptOutcome.UNAVAILABLE,
            FetchAttemptOutcome.FAILED,
        }
        if not required_terminal.issubset(terminal):
            raise ValueError("terminal_outcomes must classify every terminal DataHub outcome")
        object.__setattr__(self, "retryable_outcomes", retryable)
        object.__setattr__(self, "terminal_outcomes", terminal)
        return self


class CaptureSchedulePolicy(BaseModel):
    """Versioned demand, provider, freshness, and retry cadence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_policy_id: str = Field(default="", pattern=r"^(?:|schedule-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    policy_version: str = Field(pattern=_STABLE_COORDINATE)
    demanded_cadence: timedelta
    provider_availability_cadence: str = Field(pattern=_STABLE_COORDINATE)
    freshness_max_age: timedelta
    retry: RetryPolicy

    @field_validator("policy_version", "provider_availability_cadence")
    @classmethod
    def validate_immutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.demanded_cadence <= timedelta(0) or self.freshness_max_age <= timedelta(0):
            raise ValueError("cadence and freshness_max_age must be positive")
        _freeze_identity(
            self,
            id_field="schedule_policy_id",
            prefix="schedule-policy",
            identity_fields=(
                "policy_version",
                "demanded_cadence",
                "provider_availability_cadence",
                "freshness_max_age",
                "retry",
            ),
        )
        return self


class CaptureCampaign(BaseModel):
    """One deduplicated provider-work campaign over exact list versions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str = Field(default="", pattern=r"^(?:|capture-campaign:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    campaign_policy_id: str = Field(pattern=_STABLE_COORDINATE)
    environment: CaptureEnvironment
    cutoff: datetime
    universe_refs: tuple[UniverseRef, ...] = Field(min_length=1)

    @field_validator("campaign_policy_id")
    @classmethod
    def validate_policy(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "campaign_policy_id")

    @field_validator("cutoff")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        refs = tuple(
            sorted(
                self.universe_refs,
                key=lambda ref: (ref.universe_id, ref.universe_version, ref.content_sha256),
            )
        )
        identities = [(ref.universe_id, ref.universe_version, ref.content_sha256) for ref in refs]
        if len(identities) != len(set(identities)):
            raise ValueError("universe_refs must not contain duplicates")
        object.__setattr__(self, "universe_refs", refs)
        _freeze_identity(
            self,
            id_field="campaign_id",
            prefix="capture-campaign",
            identity_fields=("campaign_policy_id", "environment", "cutoff", "universe_refs"),
        )
        return self


class CaptureRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default="", pattern=r"^(?:|capture-run:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    campaign_id: str = Field(pattern=r"^capture-campaign:[0-9a-f]{64}$")
    run_sequence: int = Field(ge=1)
    schedule_policy_id: str = Field(pattern=r"^schedule-policy:[0-9a-f]{64}$")
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="run_id",
            prefix="capture-run",
            identity_fields=("campaign_id", "run_sequence", "schedule_policy_id", "capture_scope_id"),
        )
        return self


class ListObligation(BaseModel):
    """One exact list cell, retained even when provider work is shared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    obligation_id: str = Field(default="", pattern=r"^(?:|list-obligation:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=r"^capture-run:[0-9a-f]{64}$")
    universe_ref: UniverseRef
    subject: SubjectRef
    capture_requirement_id: str = Field(pattern=_STABLE_COORDINATE)
    partition: str = Field(pattern=_STABLE_COORDINATE)

    @field_validator("capture_requirement_id")
    @classmethod
    def validate_requirement(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "capture_requirement_id")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="obligation_id",
            prefix="list-obligation",
            identity_fields=("run_id", "universe_ref", "subject", "capture_requirement_id", "partition"),
        )
        return self


class CaptureWorkItem(BaseModel):
    """One canonical request; list bindings deliberately are not in its key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_item_id: str = Field(default="", pattern=r"^(?:|capture-work-item:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=r"^capture-run:[0-9a-f]{64}$")
    source_policy_id: str = Field(pattern=_STABLE_COORDINATE)
    canonical_request_sha256: str = Field(pattern=_SHA256)
    partition: str = Field(pattern=_STABLE_COORDINATE)

    @field_validator("source_policy_id")
    @classmethod
    def validate_source_policy(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "source_policy_id")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="work_item_id",
            prefix="capture-work-item",
            identity_fields=("run_id", "source_policy_id", "canonical_request_sha256", "partition"),
        )
        return self


class ObligationWorkBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str = Field(default="", pattern=r"^(?:|obligation-work-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    obligation_id: str = Field(pattern=r"^list-obligation:[0-9a-f]{64}$")
    work_item_id: str = Field(pattern=r"^capture-work-item:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="binding_id",
            prefix="obligation-work-binding",
            identity_fields=("obligation_id", "work_item_id"),
        )
        return self


class RawObjectIdentity(BaseModel):
    """Content-addressed source bytes, reusable by many distinct attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_object_id: str = Field(default="", pattern=r"^(?:|raw-object:[0-9a-f]{64})$")
    payload_sha256: str = Field(pattern=_SHA256)
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        expected_id = f"raw-object:{self.payload_sha256}"
        if self.raw_object_id and self.raw_object_id != expected_id:
            raise ValueError("raw_object_id must be the exact payload SHA-256")
        if self.content_sha256 and self.content_sha256 != self.payload_sha256:
            raise ValueError("raw object content hash must be the payload SHA-256")
        object.__setattr__(self, "raw_object_id", expected_id)
        object.__setattr__(self, "content_sha256", self.payload_sha256)
        return self


class FetchAttempt(BaseModel):
    """Append-only evidence for every source interaction, including unchanged bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str = Field(default="", pattern=r"^(?:|fetch-attempt:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    work_item_id: str = Field(pattern=r"^capture-work-item:[0-9a-f]{64}$")
    attempt_number: int = Field(ge=1)
    observed_at: datetime
    outcome: FetchAttemptOutcome
    raw_object_id: str | None = Field(default=None, pattern=r"^raw-object:[0-9a-f]{64}$")
    status_code: int | None = Field(default=None, ge=100, le=599)
    reason_codes: tuple[str, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "observed_at")

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "reason_codes")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.outcome in {FetchAttemptOutcome.SUCCESS, FetchAttemptOutcome.UNCHANGED} and not self.raw_object_id:
            raise ValueError("success and unchanged attempts must retain a raw object identity")
        _freeze_identity(
            self,
            id_field="attempt_id",
            prefix="fetch-attempt",
            identity_fields=("work_item_id", "attempt_number"),
        )
        return self


class ListObligationResult(BaseModel):
    """Persisted terminal result for one obligation, never inferred from missing rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(default="", pattern=r"^(?:|list-obligation-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    obligation_id: str = Field(pattern=r"^list-obligation:[0-9a-f]{64}$")
    terminal_state: ObligationTerminalState
    completed_at: datetime
    final_attempt_id: str | None = Field(default=None, pattern=r"^fetch-attempt:[0-9a-f]{64}$")
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "completed_at")

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "reason_codes", allow_empty=False)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.terminal_state is ObligationTerminalState.SKIPPED_BY_POLICY:
            if self.final_attempt_id is not None:
                raise ValueError("a policy-skipped obligation cannot name a source attempt")
        elif self.final_attempt_id is None:
            raise ValueError("a non-skipped obligation must name its terminal source attempt")
        _freeze_identity(
            self,
            id_field="result_id",
            prefix="list-obligation-result",
            identity_fields=("obligation_id", "terminal_state", "final_attempt_id"),
        )
        return self


class NormalizedObservation(BaseModel):
    """Append-only PIT observation identity; restatements supersede instead of update."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(default="", pattern=r"^(?:|normalized-observation:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    semantic_type: str = Field(pattern=_STABLE_COORDINATE)
    semantic_version: str = Field(pattern=_STABLE_COORDINATE)
    subject: SubjectRef
    valid_from: datetime
    valid_to: datetime | None = None
    knowable_at: datetime
    source_vintage: str = Field(pattern=_STABLE_COORDINATE)
    parser_version: str = Field(pattern=_STABLE_COORDINATE)
    mapping_version: str = Field(pattern=_STABLE_COORDINATE)
    payload_identity: str = Field(pattern=_STABLE_COORDINATE)
    is_restatement: bool = False
    supersedes_observation_id: str | None = Field(
        default=None,
        pattern=r"^normalized-observation:[0-9a-f]{64}$",
    )

    @field_validator("semantic_version", "source_vintage", "parser_version", "mapping_version")
    @classmethod
    def validate_versioned_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @field_validator("valid_from", "valid_to", "knowable_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")
        if self.is_restatement != (self.supersedes_observation_id is not None):
            raise ValueError("only a restatement may name, and every restatement must name, its predecessor")
        _freeze_identity(
            self,
            id_field="observation_id",
            prefix="normalized-observation",
            identity_fields=(
                "semantic_type",
                "semantic_version",
                "subject",
                "valid_from",
                "valid_to",
                "knowable_at",
                "source_vintage",
                "mapping_version",
                "payload_identity",
            ),
        )
        if self.supersedes_observation_id == self.observation_id:
            raise ValueError("a restatement cannot supersede itself")
        return self


class ConfidenceComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ConfidenceComponentKind
    score: Decimal = Field(ge=0, le=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("score", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("evidence_ids", "reason_codes")
    @classmethod
    def validate_coordinates(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _sorted_unique_strings(values, info.field_name, allow_empty=False)


class ConfidenceAssessment(BaseModel):
    """Versioned evidence dimensions plus a scalar or explicit abstention."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessment_id: str = Field(default="", pattern=r"^(?:|confidence-assessment:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    observation_id: str | None = Field(default=None, pattern=r"^normalized-observation:[0-9a-f]{64}$")
    obligation_id: str | None = Field(default=None, pattern=r"^list-obligation:[0-9a-f]{64}$")
    assessment_policy_id: str = Field(pattern=_STABLE_COORDINATE)
    evidence_set_id: str = Field(pattern=_STABLE_COORDINATE)
    components: tuple[ConfidenceComponent, ...] = ()
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    availability: AssessmentAvailability
    freshness: AssessmentFreshness
    applicability: AssessmentApplicability
    quality: AssessmentQuality
    reason_codes: tuple[str, ...] = Field(min_length=1)
    evaluation_cutoff: datetime
    assessed_at: datetime

    @field_validator("assessment_policy_id")
    @classmethod
    def validate_policy(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "assessment_policy_id")

    @field_validator("confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("evaluation_cutoff", "assessed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "reason_codes", allow_empty=False)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if (self.observation_id is None) == (self.obligation_id is None):
            raise ValueError("assessment must target exactly one observation or unavailable obligation")
        components = tuple(sorted(self.components, key=lambda value: value.kind.value))
        kinds = [component.kind for component in components]
        if len(kinds) != len(set(kinds)):
            raise ValueError("confidence component kinds must be unique")
        object.__setattr__(self, "components", components)
        if self.confidence is not None and set(kinds) != set(ConfidenceComponentKind):
            raise ValueError("a scalar confidence requires every explainable component")
        if self.observation_id is None and self.confidence is not None:
            raise ValueError("an unavailable observation cannot receive scalar confidence")
        if self.availability is AssessmentAvailability.UNAVAILABLE and self.observation_id is not None:
            raise ValueError("an unavailable assessment must target the missing obligation")
        if self.assessed_at < self.evaluation_cutoff:
            raise ValueError("assessment cannot be created before its evaluation cutoff")
        _freeze_identity(
            self,
            id_field="assessment_id",
            prefix="confidence-assessment",
            identity_fields=("observation_id", "obligation_id", "assessment_policy_id", "evidence_set_id"),
        )
        return self


class ProvenanceNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(pattern=_STABLE_COORDINATE)
    kind: ProvenanceNodeKind
    content_sha256: str | None = Field(default=None, pattern=_SHA256)


class ProvenanceEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(default="", pattern=r"^(?:|provenance-edge:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    from_node_id: str = Field(pattern=_STABLE_COORDINATE)
    edge_type: ProvenanceEdgeKind
    to_node_id: str = Field(pattern=_STABLE_COORDINATE)
    edge_ordinal: int = Field(ge=0)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.from_node_id == self.to_node_id:
            raise ValueError("provenance self-edges are forbidden")
        _freeze_identity(
            self,
            id_field="edge_id",
            prefix="provenance-edge",
            identity_fields=("from_node_id", "edge_type", "to_node_id", "edge_ordinal"),
        )
        return self


class ProvenanceGraph(BaseModel):
    """Closed indexed graph with deterministic forward and reverse traversal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(pattern=_STABLE_COORDINATE)
    nodes: tuple[ProvenanceNode, ...] = Field(min_length=1)
    edges: tuple[ProvenanceEdge, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "schema_version")

    @model_validator(mode="after")
    def validate_closed_graph(self) -> Self:
        nodes = tuple(sorted(self.nodes, key=lambda value: value.node_id))
        if len({node.node_id for node in nodes}) != len(nodes):
            raise ValueError("provenance nodes must be unique")
        node_ids = {node.node_id for node in nodes}
        edges = tuple(sorted(self.edges, key=lambda value: value.edge_id))
        if len({edge.edge_id for edge in edges}) != len(edges):
            raise ValueError("provenance edges must be unique")
        if any(edge.from_node_id not in node_ids or edge.to_node_id not in node_ids for edge in edges):
            raise ValueError("every provenance edge must reference bundled nodes")
        outgoing: dict[str, set[str]] = defaultdict(set)
        indegree = {node_id: 0 for node_id in node_ids}
        for edge in edges:
            if edge.to_node_id not in outgoing[edge.from_node_id]:
                outgoing[edge.from_node_id].add(edge.to_node_id)
                indegree[edge.to_node_id] += 1
        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        visited = 0
        while queue:
            node_id = queue.popleft()
            visited += 1
            for target in sorted(outgoing[node_id]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(node_ids):
            raise ValueError("provenance graph must be acyclic")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        return self

    def forward_node_ids(self, start_node_id: str) -> tuple[str, ...]:
        return self._reachable(start_node_id, reverse=False)

    def reverse_node_ids(self, start_node_id: str) -> tuple[str, ...]:
        return self._reachable(start_node_id, reverse=True)

    def _reachable(self, start_node_id: str, *, reverse: bool) -> tuple[str, ...]:
        node_ids = {node.node_id for node in self.nodes}
        if start_node_id not in node_ids:
            raise ValueError("trace start node is missing")
        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in self.edges:
            source, target = (
                (edge.to_node_id, edge.from_node_id)
                if reverse
                else (edge.from_node_id, edge.to_node_id)
            )
            adjacency[source].add(target)
        reached: set[str] = set()
        queue = deque([start_node_id])
        while queue:
            current = queue.popleft()
            for target in sorted(adjacency[current]):
                if target not in reached:
                    reached.add(target)
                    queue.append(target)
        return tuple(sorted(reached))


class RecapturePredicate(BaseModel):
    """Explicit bounded vocabulary for selecting prior list obligations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate_id: str = Field(default="", pattern=r"^(?:|recapture-predicate:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    universe_refs: tuple[UniverseRef, ...] = ()
    subject_ids: tuple[str, ...] = ()
    source_policy_ids: tuple[str, ...] = ()
    semantic_types: tuple[str, ...] = ()
    partitions: tuple[str, ...] = ()
    terminal_states: tuple[ObligationTerminalState, ...] = ()
    freshness_states: tuple[AssessmentFreshness, ...] = ()
    parser_versions: tuple[str, ...] = ()
    mapping_versions: tuple[str, ...] = ()
    assessment_policy_ids: tuple[str, ...] = ()

    @field_validator(
        "subject_ids",
        "semantic_types",
        "partitions",
    )
    @classmethod
    def validate_coordinates(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _sorted_unique_strings(values, info.field_name)

    @field_validator("source_policy_ids", "parser_versions", "mapping_versions", "assessment_policy_ids")
    @classmethod
    def validate_versions(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _sorted_unique_strings(values, info.field_name, immutable=True)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        refs = tuple(
            sorted(
                self.universe_refs,
                key=lambda ref: (ref.universe_id, ref.universe_version, ref.content_sha256),
            )
        )
        if len(refs) != len(set(refs)):
            raise ValueError("universe_refs must not contain duplicates")
        object.__setattr__(self, "universe_refs", refs)
        object.__setattr__(self, "terminal_states", tuple(sorted(set(self.terminal_states), key=lambda x: x.value)))
        object.__setattr__(
            self,
            "freshness_states",
            tuple(sorted(set(self.freshness_states), key=lambda x: x.value)),
        )
        dimensions = (
            self.universe_refs,
            self.subject_ids,
            self.source_policy_ids,
            self.semantic_types,
            self.partitions,
            self.terminal_states,
            self.freshness_states,
            self.parser_versions,
            self.mapping_versions,
            self.assessment_policy_ids,
        )
        if not any(dimensions):
            raise ValueError("an unbounded recapture predicate is forbidden")
        _freeze_identity(
            self,
            id_field="predicate_id",
            prefix="recapture-predicate",
            identity_fields=(
                "universe_refs",
                "subject_ids",
                "source_policy_ids",
                "semantic_types",
                "partitions",
                "terminal_states",
                "freshness_states",
                "parser_versions",
                "mapping_versions",
                "assessment_policy_ids",
            ),
        )
        return self


class RecapturePlan(BaseModel):
    """Content-hashed dry run; execution must present exactly its selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(default="", pattern=r"^(?:|recapture-plan:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    selection_cutoff: datetime
    predicate: RecapturePredicate
    selected_obligation_ids: tuple[str, ...] = Field(min_length=1)
    planner_version: str = Field(pattern=_STABLE_COORDINATE)

    @field_validator("selection_cutoff")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        return _require_aware(value, "selection_cutoff")

    @field_validator("selected_obligation_ids")
    @classmethod
    def validate_selection(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _sorted_unique_strings(values, "selected_obligation_ids", allow_empty=False)
        if any(re.fullmatch(r"list-obligation:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("selected_obligation_ids must contain list obligation identities")
        return values

    @field_validator("planner_version")
    @classmethod
    def validate_planner_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "planner_version")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="plan_id",
            prefix="recapture-plan",
            identity_fields=("selection_cutoff", "predicate", "selected_obligation_ids", "planner_version"),
        )
        return self

    def authorize_execution(self, obligation_ids: tuple[str, ...]) -> tuple[str, ...]:
        candidate = _sorted_unique_strings(obligation_ids, "execution obligation_ids", allow_empty=False)
        if candidate != self.selected_obligation_ids:
            raise ValueError("recapture execution differs from its frozen dry-run selection")
        return candidate


class DataHubInterfaceBundle(BaseModel):
    """Closed E0 contract slice used to reject cross-record identity drift."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_policies: tuple[CaptureSchedulePolicy, ...] = Field(min_length=1)
    campaigns: tuple[CaptureCampaign, ...] = Field(min_length=1)
    runs: tuple[CaptureRun, ...] = Field(min_length=1)
    obligations: tuple[ListObligation, ...] = Field(min_length=1)
    work_items: tuple[CaptureWorkItem, ...] = Field(min_length=1)
    bindings: tuple[ObligationWorkBinding, ...] = Field(min_length=1)
    attempts: tuple[FetchAttempt, ...] = Field(min_length=1)
    raw_objects: tuple[RawObjectIdentity, ...] = ()
    results: tuple[ListObligationResult, ...] = Field(min_length=1)
    observations: tuple[NormalizedObservation, ...] = ()
    assessments: tuple[ConfidenceAssessment, ...] = ()
    provenance: ProvenanceGraph

    @model_validator(mode="after")
    def validate_closed_bundle(self) -> Self:
        policies = self._index(self.schedule_policies, "schedule_policy_id")
        campaigns = self._index(self.campaigns, "campaign_id")
        runs = self._index(self.runs, "run_id")
        obligations = self._index(self.obligations, "obligation_id")
        work_items = self._index(self.work_items, "work_item_id")
        self._index(self.bindings, "binding_id")
        attempts = self._index(self.attempts, "attempt_id")
        raw_objects = self._index(self.raw_objects, "raw_object_id")
        results = self._index(self.results, "result_id")
        observations = self._index(self.observations, "observation_id")
        self._index(self.assessments, "assessment_id")

        for run in self.runs:
            if run.campaign_id not in campaigns or run.schedule_policy_id not in policies:
                raise ValueError("run references an unknown campaign or schedule policy")
        for obligation in self.obligations:
            obligation_run = runs.get(obligation.run_id)
            if obligation_run is None:
                raise ValueError("obligation references an unknown run")
            campaign = campaigns[obligation_run.campaign_id]
            if obligation.universe_ref not in campaign.universe_refs:
                raise ValueError("obligation universe is outside its frozen campaign")
        for work_item in self.work_items:
            if work_item.run_id not in runs:
                raise ValueError("work item references an unknown run")

        bound_work_by_obligation: dict[str, str] = {}
        for binding in self.bindings:
            bound_obligation = obligations.get(binding.obligation_id)
            bound_work_item = work_items.get(binding.work_item_id)
            if bound_obligation is None or bound_work_item is None:
                raise ValueError("binding references an unknown obligation or work item")
            if bound_obligation.run_id != bound_work_item.run_id:
                raise ValueError("binding cannot cross capture runs")
            if binding.obligation_id in bound_work_by_obligation:
                raise ValueError("each obligation must bind exactly one work item")
            bound_work_by_obligation[binding.obligation_id] = binding.work_item_id
        if set(bound_work_by_obligation) != set(obligations):
            raise ValueError("every obligation must retain an explicit work binding")

        attempts_by_work: dict[str, list[FetchAttempt]] = defaultdict(list)
        for attempt in self.attempts:
            if attempt.work_item_id not in work_items:
                raise ValueError("attempt references an unknown work item")
            if attempt.raw_object_id is not None and attempt.raw_object_id not in raw_objects:
                raise ValueError("attempt references an unknown raw object")
            attempts_by_work[attempt.work_item_id].append(attempt)
        if set(attempts_by_work) != set(work_items):
            raise ValueError("every work item must retain attempt evidence")
        terminal_attempt_by_work: dict[str, FetchAttempt] = {}
        for work_item_id, work_attempts in attempts_by_work.items():
            work_item = work_items[work_item_id]
            policy = policies[runs[work_item.run_id].schedule_policy_id]
            ordered = sorted(work_attempts, key=lambda item: item.attempt_number)
            if [item.attempt_number for item in ordered] != list(range(1, len(ordered) + 1)):
                raise ValueError("fetch attempts must be contiguous from one")
            if len(ordered) > policy.retry.max_attempts:
                raise ValueError("fetch attempts exceed the frozen retry budget")
            for index, attempt in enumerate(ordered):
                is_terminal = attempt.outcome in policy.retry.terminal_outcomes
                if is_terminal and index != len(ordered) - 1:
                    raise ValueError("a fetch attempt cannot follow a terminal outcome")
                if not is_terminal and attempt.outcome not in policy.retry.retryable_outcomes:
                    raise ValueError("a nonterminal outcome is not retryable under the frozen policy")
            if ordered[-1].outcome not in policy.retry.terminal_outcomes:
                raise ValueError("every work item must finish with explicit terminal evidence")
            terminal_attempt_by_work[work_item_id] = ordered[-1]

        result_by_obligation: dict[str, ListObligationResult] = {}
        for result in results.values():
            if result.obligation_id not in obligations:
                raise ValueError("result references an unknown obligation")
            if result.obligation_id in result_by_obligation:
                raise ValueError("every obligation may have only one terminal result in a bundle")
            if result.final_attempt_id is not None:
                final_attempt = attempts.get(result.final_attempt_id)
                work_item_id = bound_work_by_obligation[result.obligation_id]
                if final_attempt is None or final_attempt != terminal_attempt_by_work[work_item_id]:
                    raise ValueError("result must name its bound work item's terminal attempt")
                expected_state = ObligationTerminalState(final_attempt.outcome.value)
                if result.terminal_state is not expected_state:
                    raise ValueError("obligation result disagrees with its terminal attempt")
            result_by_obligation[result.obligation_id] = result
        if set(result_by_obligation) != set(obligations):
            raise ValueError("every obligation must retain a persisted terminal result")

        for observation in self.observations:
            predecessor_id = observation.supersedes_observation_id
            if predecessor_id is None:
                continue
            predecessor = observations.get(predecessor_id)
            if predecessor is None:
                raise ValueError("restatement predecessor is missing")
            semantic_key = (observation.semantic_type, observation.semantic_version, observation.subject)
            predecessor_key = (predecessor.semantic_type, predecessor.semantic_version, predecessor.subject)
            if semantic_key != predecessor_key or observation.valid_from != predecessor.valid_from:
                raise ValueError("restatement must preserve the superseded semantic coordinate")
            if observation.knowable_at <= predecessor.knowable_at:
                raise ValueError("restatement must append at a later knowable time")
            if observation.payload_identity == predecessor.payload_identity:
                raise ValueError("an unchanged payload is not a restatement")

        for assessment in self.assessments:
            if assessment.observation_id is not None:
                assessed_observation = observations.get(assessment.observation_id)
                if assessed_observation is None:
                    raise ValueError("assessment references an unknown observation")
                future = assessed_observation.knowable_at > assessment.evaluation_cutoff
                if future and (
                    assessment.applicability is not AssessmentApplicability.NOT_YET_KNOWABLE
                    or assessment.confidence is not None
                ):
                    raise ValueError("future knowledge must be excluded rather than scored")
            elif assessment.obligation_id not in obligations:
                raise ValueError("absence assessment references an unknown obligation")

        required_provenance_nodes: dict[str, ProvenanceNodeKind] = {}
        for values, id_field, kind in (
            (self.campaigns, "campaign_id", ProvenanceNodeKind.CAMPAIGN),
            (self.runs, "run_id", ProvenanceNodeKind.RUN),
            (self.obligations, "obligation_id", ProvenanceNodeKind.LIST_OBLIGATION),
            (self.work_items, "work_item_id", ProvenanceNodeKind.WORK_ITEM),
            (self.attempts, "attempt_id", ProvenanceNodeKind.FETCH_ATTEMPT),
            (self.raw_objects, "raw_object_id", ProvenanceNodeKind.RAW_OBJECT),
            (self.observations, "observation_id", ProvenanceNodeKind.NORMALIZED_OBSERVATION),
            (self.assessments, "assessment_id", ProvenanceNodeKind.CONFIDENCE_ASSESSMENT),
        ):
            required_provenance_nodes.update({getattr(value, id_field): kind for value in values})
        actual_provenance_nodes = {node.node_id: node.kind for node in self.provenance.nodes}
        missing = set(required_provenance_nodes) - set(actual_provenance_nodes)
        if missing:
            raise ValueError(f"provenance graph is missing core nodes: {sorted(missing)}")
        mismatched = {
            node_id
            for node_id, expected_kind in required_provenance_nodes.items()
            if actual_provenance_nodes[node_id] is not expected_kind
        }
        if mismatched:
            raise ValueError(f"provenance graph has incorrectly typed core nodes: {sorted(mismatched)}")
        incoming_node_ids = {edge.to_node_id for edge in self.provenance.edges}
        non_root_ids = {
            node_id
            for node_id, kind in required_provenance_nodes.items()
            if kind is not ProvenanceNodeKind.CAMPAIGN
        }
        disconnected = non_root_ids - incoming_node_ids
        if disconnected:
            raise ValueError(f"provenance graph has unlinked core nodes: {sorted(disconnected)}")
        return self

    @staticmethod
    def _index(values: tuple[_T, ...], id_field: str) -> dict[str, _T]:
        index: dict[str, _T] = {}
        for value in values:
            identifier = getattr(value, id_field)
            if identifier in index:
                existing = index[identifier]
                if getattr(existing, "content_sha256") != getattr(value, "content_sha256"):
                    raise ValueError(f"{id_field} collision has conflicting append-only content")
                raise ValueError(f"duplicate {id_field} is forbidden")
            index[identifier] = value
        return index
