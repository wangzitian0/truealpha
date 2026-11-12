"""DataHub capture control-plane and append-only observation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import (
    AwareDatetime,
    CaptureEnvironment,
    ContentHash,
    FrozenModel,
    IdentifiedFrozenModel,
    ImmutableId,
    Sha256,
    SortedUniqueIds,
    StableId,
    content_id,
    content_ref,
    sorted_unique_coordinates,
)
from truealpha_contracts.universe import SubjectRef, UniverseRef


class FetchAttemptOutcome(StrEnum):
    RATE_LIMITED = "rate_limited"
    TRANSPORT_ERROR = "transport_error"
    SERVER_ERROR = "server_error"
    INTERRUPTED = "interrupted"
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


class AssessmentFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class RetryPolicy(FrozenModel):
    max_attempts: int = Field(ge=1, le=20)
    retryable_outcomes: tuple[FetchAttemptOutcome, ...] = Field(min_length=1)
    terminal_outcomes: tuple[FetchAttemptOutcome, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_outcome_partition(self) -> Self:
        retryable = tuple(sorted(set(self.retryable_outcomes), key=lambda v: v.value))
        terminal = tuple(sorted(set(self.terminal_outcomes), key=lambda v: v.value))
        if len(retryable) != len(self.retryable_outcomes):
            raise ValueError("retryable_outcomes must not contain duplicates")
        if len(terminal) != len(self.terminal_outcomes):
            raise ValueError("terminal_outcomes must not contain duplicates")
        if set(retryable) & set(terminal):
            raise ValueError("retryable and terminal outcomes must be disjoint")
        required = {
            FetchAttemptOutcome.SUCCESS,
            FetchAttemptOutcome.UNCHANGED,
            FetchAttemptOutcome.UNAVAILABLE,
            FetchAttemptOutcome.FAILED,
        }
        if not required.issubset(terminal):
            raise ValueError("terminal_outcomes must classify every terminal DataHub outcome")
        object.__setattr__(self, "retryable_outcomes", retryable)
        object.__setattr__(self, "terminal_outcomes", terminal)
        return self


class CaptureSchedulePolicy(IdentifiedFrozenModel):
    id_field = "schedule_policy_id"
    id_prefix = "schedule-policy"
    identity_fields = (
        "policy_version",
        "demanded_cadence",
        "provider_availability_cadence",
        "freshness_max_age",
        "semantic_freshness_max_age",
        "retry",
    )
    schedule_policy_id: str = content_id("schedule-policy")
    content_sha256: ContentHash = ""
    policy_version: ImmutableId
    demanded_cadence: timedelta
    provider_availability_cadence: ImmutableId
    freshness_max_age: timedelta
    semantic_freshness_max_age: Mapping[str, timedelta] = Field(default_factory=dict)
    retry: RetryPolicy

    def _canonicalize(self) -> None:
        if self.demanded_cadence <= timedelta(0) or self.freshness_max_age <= timedelta(0):
            raise ValueError("cadence and freshness_max_age must be positive")
        if any(w <= timedelta(0) for w in self.semantic_freshness_max_age.values()):
            raise ValueError("semantic freshness windows must be positive")


class CaptureCampaign(IdentifiedFrozenModel):
    id_field = "campaign_id"
    id_prefix = "capture-campaign"
    identity_fields = ("campaign_policy_id", "environment", "cutoff", "universe_refs")
    campaign_id: str = content_id("capture-campaign")
    content_sha256: ContentHash = ""
    campaign_policy_id: ImmutableId
    environment: CaptureEnvironment
    cutoff: AwareDatetime
    universe_refs: tuple[UniverseRef, ...] = Field(min_length=1)

    def _canonicalize(self) -> None:
        refs = tuple(sorted(self.universe_refs, key=lambda r: (r.universe_id, r.universe_version, r.content_sha256)))
        if len(refs) != len(set(refs)):
            raise ValueError("universe_refs must not contain duplicates")
        object.__setattr__(self, "universe_refs", refs)


class CaptureRun(IdentifiedFrozenModel):
    id_field = "run_id"
    id_prefix = "capture-run"
    identity_fields = ("campaign_id", "run_sequence", "schedule_policy_id", "capture_scope_id")
    run_id: str = content_id("capture-run")
    content_sha256: ContentHash = ""
    campaign_id: str = content_ref("capture-campaign")
    run_sequence: int = Field(ge=1)
    schedule_policy_id: str = content_ref("schedule-policy")
    capture_scope_id: str = content_ref("capture-scope")


class ListObligation(IdentifiedFrozenModel):
    id_field = "obligation_id"
    id_prefix = "list-obligation"
    identity_fields = ("run_id", "universe_ref", "subject", "capture_requirement_id", "partition")
    obligation_id: str = content_id("list-obligation")
    content_sha256: ContentHash = ""
    run_id: str = content_ref("capture-run")
    universe_ref: UniverseRef
    subject: SubjectRef
    capture_requirement_id: ImmutableId
    partition: StableId


class SourceRequest(IdentifiedFrozenModel):
    id_field = "source_request_id"
    id_prefix = "source-request"
    identity_fields = (
        "source_registry_entry_id",
        "source_policy_id",
        "request_fingerprint_version",
        "canonical_request_sha256",
        "subject_refs",
        "capture_requirement_ids",
        "partition",
    )
    source_request_id: str = content_id("source-request")
    content_sha256: ContentHash = ""
    source_registry_entry_id: str = content_ref("source-registry-entry")
    source_policy_id: ImmutableId
    request_fingerprint_version: ImmutableId
    canonical_request_sha256: Sha256
    subject_refs: tuple[SubjectRef, ...] = Field(min_length=1)
    capture_requirement_ids: tuple[str, ...] = Field(min_length=1)
    partition: StableId

    @field_validator("capture_requirement_ids")
    @classmethod
    def validate_requirements(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return sorted_unique_coordinates(values, "capture_requirement_ids", allow_empty=False, immutable=True)

    def _canonicalize(self) -> None:
        subjects = tuple(sorted(self.subject_refs, key=lambda s: (s.kind.value, s.id)))
        if len(subjects) != len(set(subjects)):
            raise ValueError("subject_refs must not contain duplicates")
        object.__setattr__(self, "subject_refs", subjects)


class CaptureWorkItem(IdentifiedFrozenModel):
    id_field = "work_item_id"
    id_prefix = "capture-work-item"
    identity_fields = ("campaign_id", "source_request_id", "schedule_policy_id")
    work_item_id: str = content_id("capture-work-item")
    content_sha256: ContentHash = ""
    campaign_id: str = content_ref("capture-campaign")
    source_request_id: str = content_ref("source-request")
    schedule_policy_id: str = content_ref("schedule-policy")


class FetchAttempt(IdentifiedFrozenModel):
    id_field = "attempt_id"
    id_prefix = "fetch-attempt"
    identity_fields = ("work_item_id", "attempt_number")
    attempt_id: str = content_id("fetch-attempt")
    content_sha256: ContentHash = ""
    work_item_id: str = content_ref("capture-work-item")
    attempt_number: int = Field(ge=1)
    started_at: AwareDatetime


class FetchAttemptResult(IdentifiedFrozenModel):
    id_field = "attempt_result_id"
    id_prefix = "fetch-attempt-result"
    identity_fields = ("attempt_id",)
    attempt_result_id: str = content_id("fetch-attempt-result")
    content_sha256: ContentHash = ""
    attempt_id: str = content_ref("fetch-attempt")
    completed_at: AwareDatetime
    outcome: FetchAttemptOutcome
    status_code: int | None = Field(default=None, ge=100, le=599)
    source_vintage_id: str | None = content_ref("source-vintage", default=None)
    reused_source_vintage_id: str | None = content_ref("source-vintage", default=None)
    reason_codes: SortedUniqueIds = Field(min_length=1)

    def _canonicalize(self) -> None:
        if self.outcome is FetchAttemptOutcome.SUCCESS:
            if self.source_vintage_id is None or self.reused_source_vintage_id is not None:
                raise ValueError("a successful attempt result must create exactly one source vintage")
        elif self.outcome is FetchAttemptOutcome.UNCHANGED:
            if self.reused_source_vintage_id is None or self.source_vintage_id is not None:
                raise ValueError("an unchanged attempt result must reuse exactly one source vintage")
        elif self.source_vintage_id is not None or self.reused_source_vintage_id is not None:
            raise ValueError("a non-content attempt result cannot name a source vintage")


class SourceVintage(IdentifiedFrozenModel):
    id_field = "source_vintage_id"
    id_prefix = "source-vintage"
    identity_fields = ("source_request_id", "source_record_id", "source_published_at", "raw_object_id")
    source_vintage_id: str = content_id("source-vintage")
    content_sha256: ContentHash = ""
    source_request_id: str = content_ref("source-request")
    source_record_id: StableId
    source_published_at: AwareDatetime | None = None
    raw_object_id: str = content_ref("raw-object")


class ListObligationResult(IdentifiedFrozenModel):
    id_field = "result_id"
    id_prefix = "list-obligation-result"
    identity_fields = ("obligation_id", "terminal_state", "final_attempt_id")
    result_id: str = content_id("list-obligation-result")
    content_sha256: ContentHash = ""
    obligation_id: str = content_ref("list-obligation")
    terminal_state: ObligationTerminalState
    completed_at: AwareDatetime
    final_attempt_id: str | None = content_ref("fetch-attempt", default=None)
    reason_codes: SortedUniqueIds = Field(min_length=1)

    def _canonicalize(self) -> None:
        if self.terminal_state is ObligationTerminalState.SKIPPED_BY_POLICY:
            if self.final_attempt_id is not None:
                raise ValueError("a policy-skipped obligation cannot name a source attempt")
        elif self.final_attempt_id is None:
            raise ValueError("a non-skipped obligation must name its terminal source attempt")


class NormalizedObservation(IdentifiedFrozenModel):
    id_field = "observation_id"
    id_prefix = "normalized-observation"
    identity_fields = (
        "semantic_type",
        "semantic_version",
        "subject",
        "valid_from",
        "valid_to",
        "knowable_at",
        "source_vintage_id",
        "parser_version",
        "mapping_version",
        "normalized_payload_sha256",
    )
    observation_id: str = content_id("normalized-observation")
    content_sha256: ContentHash = ""
    semantic_type: StableId
    semantic_version: ImmutableId
    subject: SubjectRef
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    knowable_at: AwareDatetime
    source_vintage_id: str = content_ref("source-vintage")
    parser_version: ImmutableId
    mapping_version: ImmutableId
    normalized_payload_sha256: Sha256
    is_restatement: bool = False
    supersedes_observation_id: str | None = content_ref("normalized-observation", default=None)

    def _canonicalize(self) -> None:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")
        if self.is_restatement != (self.supersedes_observation_id is not None):
            raise ValueError("only a restatement may name, and every restatement must name, its predecessor")

    @model_validator(mode="after")
    def reject_self_supersession(self) -> Self:
        if self.supersedes_observation_id == self.observation_id:
            raise ValueError("a restatement cannot supersede itself")
        return self


__all__ = [
    "AssessmentFreshness",
    "CaptureCampaign",
    "CaptureRun",
    "CaptureSchedulePolicy",
    "CaptureWorkItem",
    "FetchAttempt",
    "FetchAttemptOutcome",
    "FetchAttemptResult",
    "ListObligation",
    "ListObligationResult",
    "NormalizedObservation",
    "ObligationTerminalState",
    "RetryPolicy",
    "SourceRequest",
    "SourceVintage",
]
