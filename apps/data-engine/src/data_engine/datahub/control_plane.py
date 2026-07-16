"""Deterministic list expansion and bounded attempt recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from truealpha_contracts import SubjectKind, SubjectRef, UniverseRef
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
    universe: UniverseRef,
    listings: tuple[str, ...],
    semantic_types: tuple[str, ...],
    partition: str,
) -> tuple[ListObligation, ...]:
    """Expand the exact list/type denominator without collapsing share classes."""
    if len(listings) != len(set(listings)) or len(semantic_types) != len(set(semantic_types)):
        raise ValueError("obligation inputs must not contain duplicates")
    obligations = (
        ListObligation(
            run_id=run_id,
            universe_ref=universe,
            subject=SubjectRef(kind=SubjectKind.LISTING, id=listing),
            capture_requirement_id=f"{semantic_type}:v1",
            partition=partition,
        )
        for listing in sorted(listings)
        for semantic_type in sorted(semantic_types)
    )
    return tuple(obligations)


@dataclass
class AttemptLedger:
    """Append-only in-memory E0 ledger; persistence is supplied by the repository layer later."""

    work_item_id: str
    maximum_attempts: int
    attempts: list[FetchAttempt] = field(default_factory=list)
    results: list[FetchAttemptResult] = field(default_factory=list)

    def start(self, *, started_at: datetime) -> FetchAttempt:
        if self.is_terminal:
            raise ValueError("attempt after terminal outcome")
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
