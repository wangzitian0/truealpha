"""DataHub capture control-plane and append-only observation contracts.

The contracts separate logical identity from append-only record content. A
logical ID is derived only from its declared unique-key grain, while
``content_sha256`` covers the complete record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import STABLE_ID_PATTERN, CaptureEnvironment
from truealpha_contracts.common import identify_by_grain as _freeze_identity
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import SubjectRef, UniverseRef

_SHA256 = r"^[0-9a-f]{64}$"
_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})


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
        if re.fullmatch(STABLE_ID_PATTERN, value) is None:
            raise ValueError(f"{field_name} must contain stable coordinates")
        if immutable:
            _reject_mutable_coordinate(value, field_name)
    return tuple(sorted(values))


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
    policy_version: str = Field(pattern=STABLE_ID_PATTERN)
    demanded_cadence: timedelta
    provider_availability_cadence: str = Field(pattern=STABLE_ID_PATTERN)
    freshness_max_age: timedelta
    semantic_freshness_max_age: Mapping[str, timedelta] = Field(default_factory=dict)
    retry: RetryPolicy

    @field_validator("policy_version", "provider_availability_cadence")
    @classmethod
    def validate_immutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        if self.demanded_cadence <= timedelta(0) or self.freshness_max_age <= timedelta(0):
            raise ValueError("cadence and freshness_max_age must be positive")
        if any(window <= timedelta(0) for window in self.semantic_freshness_max_age.values()):
            raise ValueError("semantic freshness windows must be positive")
        _freeze_identity(
            self,
            id_field="schedule_policy_id",
            prefix="schedule-policy",
            identity_fields=(
                "policy_version",
                "demanded_cadence",
                "provider_availability_cadence",
                "freshness_max_age",
                "semantic_freshness_max_age",
                "retry",
            ),
        )
        return self


class CaptureCampaign(BaseModel):
    """One deduplicated provider-work campaign over exact list versions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str = Field(default="", pattern=r"^(?:|capture-campaign:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    campaign_policy_id: str = Field(pattern=STABLE_ID_PATTERN)
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
    capture_requirement_id: str = Field(pattern=STABLE_ID_PATTERN)
    partition: str = Field(pattern=STABLE_ID_PATTERN)

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


class SourceRequest(BaseModel):
    """Pinned source request coordinates and its explicit obligation coverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_request_id: str = Field(default="", pattern=r"^(?:|source-request:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_policy_id: str = Field(pattern=STABLE_ID_PATTERN)
    request_fingerprint_version: str = Field(pattern=STABLE_ID_PATTERN)
    canonical_request_sha256: str = Field(pattern=_SHA256)
    subject_refs: tuple[SubjectRef, ...] = Field(min_length=1)
    capture_requirement_ids: tuple[str, ...] = Field(min_length=1)
    partition: str = Field(pattern=STABLE_ID_PATTERN)

    @field_validator("source_policy_id", "request_fingerprint_version")
    @classmethod
    def validate_immutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @field_validator("capture_requirement_ids")
    @classmethod
    def validate_requirements(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "capture_requirement_ids", allow_empty=False, immutable=True)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        subjects = tuple(sorted(self.subject_refs, key=lambda subject: (subject.kind.value, subject.id)))
        if len(subjects) != len(set(subjects)):
            raise ValueError("subject_refs must not contain duplicates")
        object.__setattr__(self, "subject_refs", subjects)
        _freeze_identity(
            self,
            id_field="source_request_id",
            prefix="source-request",
            identity_fields=(
                "source_registry_entry_id",
                "source_policy_id",
                "request_fingerprint_version",
                "canonical_request_sha256",
                "subject_refs",
                "capture_requirement_ids",
                "partition",
            ),
        )
        return self


class CaptureWorkItem(BaseModel):
    """One campaign-scoped provider request shared by compatible list runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_item_id: str = Field(default="", pattern=r"^(?:|capture-work-item:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    campaign_id: str = Field(pattern=r"^capture-campaign:[0-9a-f]{64}$")
    source_request_id: str = Field(pattern=r"^source-request:[0-9a-f]{64}$")
    schedule_policy_id: str = Field(pattern=r"^schedule-policy:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="work_item_id",
            prefix="capture-work-item",
            identity_fields=("campaign_id", "source_request_id", "schedule_policy_id"),
        )
        return self


class FetchAttempt(BaseModel):
    """Persisted dispatch intent before a source request can be issued."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str = Field(default="", pattern=r"^(?:|fetch-attempt:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    work_item_id: str = Field(pattern=r"^capture-work-item:[0-9a-f]{64}$")
    attempt_number: int = Field(ge=1)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "started_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="attempt_id",
            prefix="fetch-attempt",
            identity_fields=("work_item_id", "attempt_number"),
        )
        return self


class FetchAttemptResult(BaseModel):
    """One append-only result for a previously persisted dispatch intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_result_id: str = Field(default="", pattern=r"^(?:|fetch-attempt-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    attempt_id: str = Field(pattern=r"^fetch-attempt:[0-9a-f]{64}$")
    completed_at: datetime
    outcome: FetchAttemptOutcome
    status_code: int | None = Field(default=None, ge=100, le=599)
    source_vintage_id: str | None = Field(default=None, pattern=r"^source-vintage:[0-9a-f]{64}$")
    reused_source_vintage_id: str | None = Field(default=None, pattern=r"^source-vintage:[0-9a-f]{64}$")
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
        if self.outcome is FetchAttemptOutcome.SUCCESS:
            if self.source_vintage_id is None or self.reused_source_vintage_id is not None:
                raise ValueError("a successful attempt result must create exactly one source vintage")
        elif self.outcome is FetchAttemptOutcome.UNCHANGED:
            if self.reused_source_vintage_id is None or self.source_vintage_id is not None:
                raise ValueError("an unchanged attempt result must reuse exactly one source vintage")
        elif self.source_vintage_id is not None or self.reused_source_vintage_id is not None:
            raise ValueError("a non-content attempt result cannot name a source vintage")
        _freeze_identity(
            self,
            id_field="attempt_result_id",
            prefix="fetch-attempt-result",
            identity_fields=("attempt_id",),
        )
        return self


class SourceVintage(BaseModel):
    """A source-publication identity that preserves request and raw-byte lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_vintage_id: str = Field(default="", pattern=r"^(?:|source-vintage:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    source_request_id: str = Field(pattern=r"^source-request:[0-9a-f]{64}$")
    source_record_id: str = Field(pattern=STABLE_ID_PATTERN)
    source_published_at: datetime | None = None
    raw_object_id: str = Field(pattern=r"^raw-object:[0-9a-f]{64}$")

    @field_validator("source_published_at")
    @classmethod
    def validate_source_published_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value, "source_published_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        _freeze_identity(
            self,
            id_field="source_vintage_id",
            prefix="source-vintage",
            identity_fields=("source_request_id", "source_record_id", "source_published_at", "raw_object_id"),
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
    semantic_type: str = Field(pattern=STABLE_ID_PATTERN)
    semantic_version: str = Field(pattern=STABLE_ID_PATTERN)
    subject: SubjectRef
    valid_from: datetime
    valid_to: datetime | None = None
    knowable_at: datetime
    source_vintage_id: str = Field(pattern=r"^source-vintage:[0-9a-f]{64}$")
    parser_version: str = Field(pattern=STABLE_ID_PATTERN)
    mapping_version: str = Field(pattern=STABLE_ID_PATTERN)
    normalized_payload_sha256: str = Field(pattern=_SHA256)
    # #530: The next two fields stay declared and unused by the capture path.
    # `test_restatement_fields_stay_unwired.py` fails when a data-engine module writes them.
    is_restatement: bool = False
    supersedes_observation_id: str | None = Field(
        default=None,
        pattern=r"^normalized-observation:[0-9a-f]{64}$",
    )

    @field_validator("semantic_version", "parser_version", "mapping_version")
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
                "source_vintage_id",
                "parser_version",
                "mapping_version",
                "normalized_payload_sha256",
            ),
        )
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
