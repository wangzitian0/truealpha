"""Deterministic list expansion and bounded attempt recording."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from truealpha_contracts import SubjectKind, UniverseRef
from truealpha_contracts.capture_control import CaptureListObligation, CaptureListVersion
from truealpha_contracts.datahub import (
    FetchAttempt,
    FetchAttemptOutcome,
    FetchAttemptResult,
    ListObligation,
    RetryPolicy,
)


def replay_retry_policy(max_attempts: int) -> RetryPolicy:
    """Build the frozen D5 retry partition without activating a scheduler."""
    return RetryPolicy(
        max_attempts=max_attempts,
        retryable_outcomes=(
            FetchAttemptOutcome.INTERRUPTED,
            FetchAttemptOutcome.RATE_LIMITED,
            FetchAttemptOutcome.SERVER_ERROR,
            FetchAttemptOutcome.TRANSPORT_ERROR,
        ),
        terminal_outcomes=(
            FetchAttemptOutcome.FAILED,
            FetchAttemptOutcome.SUCCESS,
            FetchAttemptOutcome.UNAVAILABLE,
            FetchAttemptOutcome.UNCHANGED,
        ),
    )


def frozen_topt_universe(corpus: Mapping[str, Any]) -> UniverseRef:
    """Reconstruct the frozen TOPT universe coordinate used by every D5 rung."""
    denominator = corpus["topt_denominator"]
    return UniverseRef(
        universe_id=denominator["universe_id"],
        universe_version="topt-candidate-2026-03-31-v1",
        content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
    )


def expand_obligations(
    *,
    run_id: str,
    list_version: CaptureListVersion,
    semantic_types: tuple[str, ...],
    partition: str,
) -> tuple[CaptureListObligation, ...]:
    """Expand the exact list/type denominator without collapsing share classes."""
    if not semantic_types:
        raise ValueError("semantic_types must not be empty")
    if len(semantic_types) != len(set(semantic_types)):
        raise ValueError("obligation inputs must not contain duplicates")
    supported_kinds = {SubjectKind.LISTING, SubjectKind.SECURITY}
    if any(member.kind not in supported_kinds for member in list_version.members):
        raise ValueError("capture-control expansion requires listing or security members")
    ordered_semantic_types = tuple(sorted(semantic_types))
    obligations = (
        CaptureListObligation(
            list_version_id=list_version.list_version_id,
            obligation=ListObligation(
                run_id=run_id,
                universe_ref=list_version.universe,
                subject=member,
                capture_requirement_id=f"{semantic_type}:v1",
                partition=partition,
            ),
        )
        for member in list_version.members
        for semantic_type in ordered_semantic_types
    )
    return tuple(obligations)


@dataclass
class AttemptLedger:
    """Append-only in-memory E0 ledger; persistence is supplied by the repository layer later."""

    work_item_id: str
    retry_policy: RetryPolicy
    attempts: list[FetchAttempt] = field(default_factory=list)
    results: list[FetchAttemptResult] = field(default_factory=list)

    @property
    def maximum_attempts(self) -> int:
        return self.retry_policy.max_attempts

    def start(self, *, started_at: datetime) -> FetchAttempt:
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        if self.is_terminal:
            raise ValueError("attempt after terminal outcome")
        if len(self.results) != len(self.attempts):
            raise ValueError("previous attempt has no result")
        if self.results and started_at < self.results[-1].completed_at:
            raise ValueError("retry starts before previous result completion")
        if len(self.attempts) >= self.maximum_attempts:
            raise ValueError("maximum attempts exceeded")
        attempt = FetchAttempt(
            work_item_id=self.work_item_id,
            attempt_number=len(self.attempts) + 1,
            started_at=started_at,
        )
        self.attempts.append(attempt)
        return attempt

    def finish(
        self,
        *,
        attempt: FetchAttempt,
        completed_at: datetime,
        outcome: FetchAttemptOutcome,
        error_code: str | None = None,
        status_code: int | None = None,
        source_vintage_id: str | None = None,
        reused_source_vintage_id: str | None = None,
    ) -> FetchAttemptResult:
        if not self.attempts or self.attempts[-1] != attempt:
            raise ValueError("attempt is not the current append-only attempt")
        if any(result.attempt_id == attempt.attempt_id for result in self.results):
            raise ValueError("attempt already has a result")
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        if completed_at < attempt.started_at:
            raise ValueError("completed_at precedes started_at")
        if outcome is FetchAttemptOutcome.SUCCESS:
            if source_vintage_id is None or reused_source_vintage_id is not None:
                raise ValueError("a successful result must create exactly one source vintage")
        elif outcome is FetchAttemptOutcome.UNCHANGED:
            if reused_source_vintage_id is None or source_vintage_id is not None:
                raise ValueError("an unchanged result must reuse exactly one source vintage")
        elif source_vintage_id is not None or reused_source_vintage_id is not None:
            raise ValueError("a non-content result cannot name a source vintage")
        classified = set(self.retry_policy.retryable_outcomes) | set(self.retry_policy.terminal_outcomes)
        if outcome not in classified:
            raise ValueError("outcome is not classified by the retry policy")
        if attempt.attempt_number == self.maximum_attempts and outcome not in self.retry_policy.terminal_outcomes:
            raise ValueError("the final permitted attempt must have a terminal outcome")
        result = FetchAttemptResult(
            attempt_id=attempt.attempt_id,
            completed_at=completed_at,
            outcome=outcome,
            status_code=status_code,
            reason_codes=(error_code or outcome.value,),
            source_vintage_id=source_vintage_id,
            reused_source_vintage_id=reused_source_vintage_id,
        )
        self.results.append(result)
        return result

    @property
    def is_terminal(self) -> bool:
        return bool(self.results and self.results[-1].outcome in self.retry_policy.terminal_outcomes)
