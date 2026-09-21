"""TOPT capture executor (Phase 3b, ADR A1 + #366/#171).

Drives each planned `CaptureWorkItem` through an injected `SourceFetchPort`, applies the
reason-code disposition (STOP / RETRY / TRACE_ONLY), and writes the append-only evidence
graph for every success. The real source adapters implement `SourceFetchPort` in later
slices; this module owns the loop, the error-code governance, and the evidence writes.

A run succeeds when every obligation reaches a terminal state with no STOP outstanding — not
only when every obligation is `available` (see #366).

A source may declare further origins able to serve a cell its primary could not (#862):
its port then also implements `FailoverFetchPort`. The executor asks for a failover only
once the primary is exhausted — every RETRY spent, or a TRACE_ONLY answer — never on a
STOP. A served failover resolves the obligation SUCCESS while its attempts keep the
primary's failure reasons, so the ledger never forgets why the primary did not serve.

A primary can also answer with something a further origin may serve better: a bar from a
session older than the one the cell asks for (#862, the 2026-09-15 KHC close). Its success
then names that shortfall (`FetchSuccess.failover_reason`); the executor asks `failover`
with it, and the primary's own success stands when no origin can serve.
Which origins exist, in which order, and what counts as the same datum stay the source's
business; this loop only knows that one was asked.
"""

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
# The token a failover-served obligation carries beside the primary's reason codes, and the
# payload key its served observation declares the serving origin under (#862). One name, so
# the ledger, the payload and every report read the same word.
SERVED_BY_FAILOVER = "served_by_failover"


def _require_digest(digest: str) -> None:
    if len(digest) != _HEX64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("fetch digests must be lowercase sha256 hex")


@dataclass(frozen=True)
class NormalizedRecord:
    """The normalized record a fetch asserts, with the parser identity behind it.

    The payload is JSON-safe and Decimal-free (base-10 strings for numerics, per
    init.md's no-binary-float rule); it is what lands in
    `staging.capture_observation_payloads` and what the materializer validates.
    """

    payload: Mapping[str, Any]
    parser_version: str
    mapping_version: str


@dataclass(frozen=True)
class RawResponse:
    """The vendor's response bytes, verbatim, plus where they belong in raw storage.

    Adapters used to hand over a digest and an object URI they had computed themselves,
    keeping the bytes. Nothing then wrote those bytes anywhere, so `raw.fetches` filled
    with pointers into buckets that do not exist — 924 captures and one stored object
    (#171). Carrying the bytes makes the landing possible and makes the digest
    underivable-by-hand: it is computed here, from what actually arrived.

    `source` and `record_id` are the VENDOR's identity for this response, not the capture
    run's. That is what lets the content-addressed store collapse an unchanged re-fetch
    onto the object already there; keying on the run version, as the synthesized URIs did,
    guarantees every tick writes a new copy of identical bytes.
    """

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
    """An independent second-origin assertion about the same cell (init.md rule 15).

    A cell reaches two independent origins by the owning adapter returning one, not
    by the executor knowing a second source exists. The sink persists it as its own
    source request / vintage / observation, so the fusion engine reconciles two
    real assertions rather than counting origins.
    """

    origin: str
    record: NormalizedRecord
    confidence: Decimal
    raw: RawResponse
    normalized_sha256: str
    # The second origin's own source time (its settled bar's date) — the sink
    # persists it as the corroborating observation's knowable_at and the
    # vintage's source_published_at (#530 slice 3).
    transaction_time: datetime

    def __post_init__(self) -> None:
        _require_digest(self.normalized_sha256)
        if canonical_sha256(dict(self.record.payload)) != self.normalized_sha256:
            raise ValueError("corroboration payload does not match its normalized digest")
        if not (Decimal(0) <= self.confidence <= Decimal(1)):
            raise ValueError("confidence must be in [0, 1]")
        if self.transaction_time.tzinfo is None or self.transaction_time.utcoffset() is None:
            raise ValueError("corroboration transaction_time must be timezone-aware")


@dataclass(frozen=True)
class FetchSuccess:
    """A successful fetch: immutable raw bytes plus one normalized value.

    `record` carries the normalized payload for the capture-control sink; adapters
    that only feed the evidence graph may omit it. `corroborations` carry the same
    cell's independent second-origin assertions.
    """

    raw: RawResponse
    normalized_sha256: str
    confidence: Decimal
    valid_from: date
    transaction_time: datetime
    record: NormalizedRecord | None = None
    corroborations: tuple[Corroboration, ...] = field(default=())
    # Set only on a success a `FailoverFetchPort` returned: the origin that served the
    # cell the primary could not (#862). The record is that origin's own assertion — its
    # parser identity, its bytes — and its payload names the origin under the same key.
    served_by_failover: str | None = None
    # Set only by a `FailoverFetchPort`'s `fetch`: the primary served, but short of what
    # the cell asks for — a bar from an older session than the settled one (#862). The
    # executor then asks `failover` with this reason; this success stands if no origin
    # can serve, and is replaced (the attempt keeping this reason) if one can.
    failover_reason: ObligationReasonCode | None = None

    @property
    def raw_sha256(self) -> str:
        return self.raw.sha256

    def __post_init__(self) -> None:
        _require_digest(self.normalized_sha256)
        if not (Decimal(0) <= self.confidence <= Decimal(1)):
            raise ValueError("confidence must be in [0, 1]")
        if self.record is not None and canonical_sha256(dict(self.record.payload)) != self.normalized_sha256:
            raise ValueError("normalized payload does not match its normalized digest")
        if self.served_by_failover is not None:
            if not self.served_by_failover:
                raise ValueError("a failover success must name the origin that served it")
            if self.record is None or self.record.payload.get(SERVED_BY_FAILOVER) != self.served_by_failover:
                # The payload is what the snapshot binds and every report reads; a
                # substitution it does not declare is a silent one.
                raise ValueError("a failover success must declare its serving origin in its payload")
        if self.failover_reason is not None:
            if self.served_by_failover is not None:
                raise ValueError("a failover success cannot itself ask for a failover")
            if disposition_for(self.failover_reason) is ObligationDisposition.STOP:
                # A STOP is a broken run, never a gap another origin may fill.
                raise ValueError(f"{self.failover_reason.value} is a STOP reason, not a failover reason")


@dataclass(frozen=True)
class FetchFailure:
    """A failed fetch classified by a reason code."""

    reason_code: ObligationReasonCode


FetchOutcome = FetchSuccess | FetchFailure


class SourceFetchPort(Protocol):
    """Implemented by the real source adapters (SEC / Yahoo / release-derived / #70)."""

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome: ...


@runtime_checkable
class FailoverFetchPort(Protocol):
    """A `SourceFetchPort` whose source declares further origins for the same cell (#862).

    Asked once, after the primary could not serve (`primary_reason` is its last
    classified failure). Returns the next origin's success — marked with
    `served_by_failover` — or None when no origin holds the datum, in which case the
    obligation resolves exactly as it would have without a failover.
    """

    def failover(self, work_item: CaptureWorkItem, primary_reason: ObligationReasonCode) -> FetchSuccess | None: ...


class ObligationSink(Protocol):
    """Persists what a terminally resolved obligation produced.

    Injected so the generic executor never learns the storage shape (init.md rule
    22): it hands over the work item, its attempt history and the terminal state,
    and the sink decides which tables that becomes. A failover-served obligation arrives as
    SUCCESS with `success.served_by_failover` set and the primary's failure as its last
    attempt reason (#862).
    """

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
    # The origin that served the cell when the primary could not (#862); `reason_code`
    # is then the primary's last failure, not None.
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
        """Obligations the primary could not serve and a further origin did (#862)."""
        return sum(o.served_by_failover is not None for o in self.outcomes)

    @property
    def succeeded(self) -> bool:
        """True when the run terminally resolved every obligation with no STOP outstanding."""
        return not self.halted and self.total == len(self.outcomes)


class ToptCaptureExecutor:
    """Iterates work items, applies reason-code dispositions, writes the evidence graph."""

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
                    # The primary served short (#862): a further origin may serve the cell
                    # instead, and the attempt then keeps the primary's shortfall.
                    served = self._failover(item, fetch, result.failover_reason)
                    if served is not None:
                        reasons.append(result.failover_reason)
                        self._write_success(item, served, run_ref, recorded_at)
                        return self._terminal(item, reasons, ObligationTerminalState.SUCCESS, served), False
                    reasons.append(result.failover_reason)
                else:
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
            # RETRY: loop again until max_attempts, then resolve unavailable.
        # The primary could not serve: every retry spent, or a trace-only answer.
        assert primary_reason is not None
        served = self._failover(item, fetch, primary_reason)
        if served is not None:
            # The attempts keep the primary's failure; the served origin never erases it.
            self._write_success(item, served, run_ref, recorded_at)
            return self._terminal(item, reasons, ObligationTerminalState.SUCCESS, served), False
        return self._terminal(item, reasons, ObligationTerminalState.UNAVAILABLE, None), False

    @staticmethod
    def _failover(
        item: CaptureWorkItem, fetch: SourceFetchPort, primary_reason: ObligationReasonCode
    ) -> FetchSuccess | None:
        """The source's next origin for a cell its primary could not serve, if it declares one."""
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
        item: CaptureWorkItem,
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
        nodes = [
            EvidenceNode(ref=raw_ref, content_sha256=result.raw_sha256, stamp=stamp),
            EvidenceNode(ref=obs_ref, content_sha256=result.normalized_sha256, stamp=stamp),
        ]
        edges = [
            EvidenceEdge(from_ref=obs_ref, to_ref=raw_ref, relation=EvidenceRelation.DERIVED_FROM, stamp=stamp),
            EvidenceEdge(from_ref=raw_ref, to_ref=run_ref, relation=EvidenceRelation.MEMBER_OF, stamp=stamp),
        ]
        self._writer.append(nodes, edges)
