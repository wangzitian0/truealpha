"""TOPT capture executor and append-only evidence graph writes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem, ObligationTerminalState
from truealpha_contracts.evidence_graph import (
    BitemporalStamp,
    EvidenceEdge,
    EvidenceGraphWriter,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
)
from truealpha_contracts.models import DataSource
from truealpha_contracts.obligation_reason_codes import ObligationDisposition, ObligationReasonCode, disposition_for

_HEX64 = 64
SERVED_BY_FAILOVER = "served_by_failover"


def _require_digest(digest: str) -> None:
    if len(digest) != _HEX64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("fetch digests must be lowercase sha256 hex")


@dataclass(frozen=True)
class NormalizedRecord:
    payload: Mapping[str, Any]
    parser_version: str
    mapping_version: str


@dataclass(frozen=True)
class RawResponse:
    body: bytes
    source: DataSource
    record_id: str
    content_type: str = "application/json"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def byte_length(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class Corroboration:
    origin: str
    record: NormalizedRecord
    raw: RawResponse
    normalized_sha256: str
    transaction_time: datetime
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        _require_digest(self.normalized_sha256)
        if canonical_sha256(dict(self.record.payload)) != self.normalized_sha256:
            raise ValueError("corroboration payload does not match its normalized digest")
        if self.confidence is not None and not (Decimal(0) <= self.confidence <= Decimal(1)):
            raise ValueError("confidence must be in [0, 1]")
        if self.transaction_time.tzinfo is None or self.transaction_time.utcoffset() is None:
            raise ValueError("corroboration transaction_time must be timezone-aware")


@dataclass(frozen=True)
class FetchSuccess:
    raw: RawResponse
    normalized_sha256: str
    valid_from: date
    transaction_time: datetime
    confidence: Decimal | None = None
    record: NormalizedRecord | None = None
    corroborations: tuple[Corroboration, ...] = field(default=())
    served_by_failover: str | None = None
    failover_reason: ObligationReasonCode | None = None

    @property
    def raw_sha256(self) -> str:
        return self.raw.sha256

    def __post_init__(self) -> None:
        _require_digest(self.normalized_sha256)
        if self.confidence is not None and not (Decimal(0) <= self.confidence <= Decimal(1)):
            raise ValueError("confidence must be in [0, 1]")
        if self.record is not None and canonical_sha256(dict(self.record.payload)) != self.normalized_sha256:
            raise ValueError("normalized payload does not match its normalized digest")
        if self.served_by_failover is not None:
            if not self.served_by_failover:
                raise ValueError("a failover success must name the origin that served it")
            if self.record is None or self.record.payload.get(SERVED_BY_FAILOVER) != self.served_by_failover:
                raise ValueError("a failover success must declare its serving origin in its payload")
        if self.failover_reason is not None:
            if self.served_by_failover is not None:
                raise ValueError("a failover success cannot itself ask for a failover")
            if disposition_for(self.failover_reason) is ObligationDisposition.STOP:
                raise ValueError(f"{self.failover_reason.value} is a STOP reason, not a failover reason")


@dataclass(frozen=True)
class FetchFailure:
    reason_code: ObligationReasonCode


FetchOutcome = FetchSuccess | FetchFailure


class SourceFetchPort(Protocol):
    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome: ...


@runtime_checkable
class FailoverFetchPort(Protocol):
    def failover(self, work_item: CaptureWorkItem, primary_reason: ObligationReasonCode) -> FetchSuccess | None: ...


class ObligationSink(Protocol):
    def record_outcome(
        self,
        work_item: CaptureWorkItem,
        *,
        attempt_reasons: Sequence[ObligationReasonCode | None],
        terminal_state: ObligationTerminalState,
        success: FetchSuccess | None,
    ) -> None: ...


@dataclass(frozen=True)
class ObligationOutcome:
    work_item_id: str
    terminal_state: ObligationTerminalState
    reason_code: ObligationReasonCode | None
    attempts: int
    served_by_failover: str | None = None


@dataclass(frozen=True)
class ToptCaptureRunReport:
    run_id: str
    outcomes: tuple[ObligationOutcome, ...]
    halted: bool
    halt_reason: ObligationReasonCode | None

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def available(self) -> int:
        return sum(o.terminal_state is ObligationTerminalState.SUCCESS for o in self.outcomes)

    @property
    def unavailable(self) -> int:
        return sum(o.terminal_state is ObligationTerminalState.UNAVAILABLE for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.terminal_state is ObligationTerminalState.FAILED for o in self.outcomes)

    @property
    def served_by_failover(self) -> int:
        return sum(o.served_by_failover is not None for o in self.outcomes)

    @property
    def succeeded(self) -> bool:
        return not self.halted and self.total == len(self.outcomes)


class ToptCaptureExecutor:
    def __init__(
        self,
        writer: EvidenceGraphWriter,
        *,
        sink: ObligationSink | None = None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._writer = writer
        self._sink = sink
        self._max_attempts = max_attempts

    def run(
        self,
        run_id: str,
        work_items: Sequence[CaptureWorkItem],
        fetch: SourceFetchPort,
        *,
        cutoff: datetime,
        recorded_at: datetime,
    ) -> ToptCaptureRunReport:
        if not run_id.startswith("capture-run:"):
            raise ValueError("run_id must be a capture-run identity")
        run_ref = EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=run_id)
        run_stamp = BitemporalStamp(valid_from=cutoff.date(), transaction_time=cutoff, recorded_at=recorded_at)
        self._writer.append(
            [EvidenceNode(ref=run_ref, content_sha256=run_id.split(":", 1)[1], stamp=run_stamp)],
            [],
        )

        outcomes: list[ObligationOutcome] = []
        for item in work_items:
            outcome, halt = self._run_item(item, fetch, run_ref, recorded_at)
            outcomes.append(outcome)
            if halt:
                return ToptCaptureRunReport(
                    run_id=run_id,
                    outcomes=tuple(outcomes),
                    halted=True,
                    halt_reason=outcome.reason_code,
                )
        return ToptCaptureRunReport(run_id=run_id, outcomes=tuple(outcomes), halted=False, halt_reason=None)

    def _run_item(
        self,
        item: CaptureWorkItem,
        fetch: SourceFetchPort,
        run_ref: EvidenceNodeRef,
        recorded_at: datetime,
    ) -> tuple[ObligationOutcome, bool]:
        reasons: list[ObligationReasonCode | None] = []
        primary_reason: ObligationReasonCode | None = None
        while len(reasons) < self._max_attempts:
            result = fetch.fetch(item)
            if isinstance(result, FetchSuccess):
                if result.served_by_failover is not None:
                    raise ValueError(f"{item.work_item_id}: a failover success must come from `failover`, not `fetch`")
                if result.failover_reason is not None:
                    served = self._failover(item, fetch, result.failover_reason)
                    if served is not None:
                        reasons.append(result.failover_reason)
                        self._write_success(item, served, run_ref, recorded_at)
                        return self._terminal(item, reasons, ObligationTerminalState.SUCCESS, served), False
                reasons.append(None)
                self._write_success(item, result, run_ref, recorded_at)
                return self._terminal(item, reasons, ObligationTerminalState.SUCCESS, result), False
            reasons.append(result.reason_code)
            primary_reason = result.reason_code
            disposition = disposition_for(result.reason_code)
            if disposition is ObligationDisposition.STOP:
                return self._terminal(item, reasons, ObligationTerminalState.FAILED, None), True
            if disposition is ObligationDisposition.TRACE_ONLY:
                break
        assert primary_reason is not None
        served = self._failover(item, fetch, primary_reason)
        if served is not None:
            self._write_success(item, served, run_ref, recorded_at)
            return self._terminal(item, reasons, ObligationTerminalState.SUCCESS, served), False
        return self._terminal(item, reasons, ObligationTerminalState.UNAVAILABLE, None), False

    @staticmethod
    def _failover(
        item: CaptureWorkItem, fetch: SourceFetchPort, primary_reason: ObligationReasonCode
    ) -> FetchSuccess | None:
        if not isinstance(fetch, FailoverFetchPort):
            return None
        served = fetch.failover(item, primary_reason)
        if served is not None and served.served_by_failover is None:
            raise ValueError(f"{item.work_item_id}: a failover success must name the origin that served it")
        return served

    def _terminal(
        self,
        item: CaptureWorkItem,
        reasons: Sequence[ObligationReasonCode | None],
        terminal_state: ObligationTerminalState,
        success: FetchSuccess | None,
    ) -> ObligationOutcome:
        if self._sink is not None:
            self._sink.record_outcome(
                item,
                attempt_reasons=tuple(reasons),
                terminal_state=terminal_state,
                success=success,
            )
        return ObligationOutcome(
            item.work_item_id,
            terminal_state,
            reasons[-1],
            len(reasons),
            served_by_failover=None if success is None else success.served_by_failover,
        )

    def _write_success(
        self,
        _item: CaptureWorkItem,
        result: FetchSuccess,
        run_ref: EvidenceNodeRef,
        recorded_at: datetime,
    ) -> None:
        raw_ref = EvidenceNodeRef(kind=EvidenceNodeKind.RAW_FETCH, node_id=f"raw-fetch:{result.raw_sha256}")
        obs_ref = EvidenceNodeRef(
            kind=EvidenceNodeKind.NORMALIZED_OBSERVATION,
            node_id=f"normalized-observation:{result.normalized_sha256}",
        )
        stamp = BitemporalStamp(
            valid_from=result.valid_from,
            transaction_time=result.transaction_time,
            recorded_at=recorded_at,
        )
        self._writer.append(
            [
                EvidenceNode(ref=raw_ref, content_sha256=result.raw_sha256, stamp=stamp),
                EvidenceNode(ref=obs_ref, content_sha256=result.normalized_sha256, stamp=stamp),
            ],
            [
                EvidenceEdge(from_ref=obs_ref, to_ref=raw_ref, relation=EvidenceRelation.DERIVED_FROM, stamp=stamp),
                EvidenceEdge(from_ref=raw_ref, to_ref=run_ref, relation=EvidenceRelation.MEMBER_OF, stamp=stamp),
            ],
        )
