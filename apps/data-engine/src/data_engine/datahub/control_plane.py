"""Deterministic list expansion and bounded attempt recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from truealpha_contracts import SubjectKind
from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.datahub import (
    FetchAttempt,
    FetchAttemptOutcome,
    FetchAttemptResult,
    ListObligation,
)

_TERMINAL = {
    FetchAttemptOutcome.SUCCESS,
    FetchAttemptOutcome.UNCHANGED,
    FetchAttemptOutcome.UNAVAILABLE,
    FetchAttemptOutcome.FAILED,
}


def expand_obligations(
    *,
    run_id: str,
    list_version: CaptureListVersion,
    semantic_types: tuple[str, ...],
    partition: str,
) -> tuple[ListObligation, ...]:
    """Expand the exact list/type denominator without collapsing share classes."""
    if len(semantic_types) != len(set(semantic_types)):
        raise ValueError("obligation inputs must not contain duplicates")
    if any(member.kind is not SubjectKind.LISTING for member in list_version.members):
        raise ValueError("capture-control expansion requires listing members")
    ordered_semantic_types = tuple(sorted(semantic_types))
    obligations = (
        ListObligation(
            run_id=run_id,
            universe_ref=list_version.universe,
            subject=member,
            capture_requirement_id=f"{semantic_type}:v1",
            partition=partition,
        )
        for member in list_version.members
        for semantic_type in ordered_semantic_types
    )
    return tuple(obligations)


@dataclass
class AttemptLedger:
    """Append-only in-memory E0 ledger; persistence is supplied by the repository layer later."""

    work_item_id: str
    maximum_attempts: int
    attempts: list[FetchAttempt] = field(default_factory=list)
    results: list[FetchAttemptResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")

    def start(self, *, started_at: datetime) -> FetchAttempt:
        if self.is_terminal:
            raise ValueError("attempt after terminal outcome")
        if len(self.results) != len(self.attempts):
            raise ValueError("previous attempt has no result")
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
        source_vintage_id: str | None = None,
        reused_source_vintage_id: str | None = None,
    ) -> FetchAttemptResult:
        if not self.attempts or self.attempts[-1] != attempt:
            raise ValueError("attempt is not the current append-only attempt")
        if any(result.attempt_id == attempt.attempt_id for result in self.results):
            raise ValueError("attempt already has a result")
        if completed_at < attempt.started_at:
            raise ValueError("completed_at precedes started_at")
        result = FetchAttemptResult(
            attempt_id=attempt.attempt_id,
            completed_at=completed_at,
            outcome=outcome,
            reason_codes=(error_code or outcome.value,),
            source_vintage_id=source_vintage_id,
            reused_source_vintage_id=reused_source_vintage_id,
        )
        self.results.append(result)
        return result

    @property
    def is_terminal(self) -> bool:
        return bool(self.results and self.results[-1].outcome in _TERMINAL)
