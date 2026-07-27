"""Capture-control persistence for the TOPT executor path (#171 A1, ADR A1).

The generic executor owns the loop and the reason-code governance; it must never
learn how a capture is stored (init.md rule 22). This module supplies the
`ObligationSink` it writes through: one terminally resolved obligation becomes an
attempt ledger, an immutable `raw.fetches` landing, a source vintage, a normalized
observation with its payload, and the terminal `raw.capture_obligation_results`
row — the exact tables `freeze_snapshot` → `materialize` → `mart.topt_*` reads.

Everything lands on the caller's connection, so the capture-control writes and the
executor's evidence-graph append commit as one transaction per run.

Stamps are derived from the run's `cutoff`, never the wall clock, so a retried tick
reproduces the same content-addressed rows and the conflict-tolerant inserts make
the replay idempotent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import Connection
from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.capture_control import CaptureListObligation
from truealpha_contracts.datahub import (
    CaptureWorkItem,
    FetchAttemptOutcome,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    RetryPolicy,
    SourceRequest,
    SourceVintage,
)

from data_engine.datahub.control_plane import AttemptLedger
from data_engine.datahub.production_topt.executor import Corroboration, FetchSuccess
from data_engine.datahub.repository import PostgresCaptureControlRepository

# How a classified reason code lands in the attempt ledger. The reason code stays
# the record of *why*; the outcome is what the retry policy classifies.
_ATTEMPT_OUTCOMES: Mapping[ObligationReasonCode, FetchAttemptOutcome] = {
    ObligationReasonCode.AUTH_FAILED: FetchAttemptOutcome.FAILED,
    ObligationReasonCode.CONTRACT_VIOLATION: FetchAttemptOutcome.FAILED,
    ObligationReasonCode.RELEASE_SCOPE_MISMATCH: FetchAttemptOutcome.FAILED,
    ObligationReasonCode.LOOK_AHEAD_VIOLATION: FetchAttemptOutcome.FAILED,
    ObligationReasonCode.CHECKSUM_MISMATCH: FetchAttemptOutcome.FAILED,
    ObligationReasonCode.TRANSIENT_NETWORK: FetchAttemptOutcome.TRANSPORT_ERROR,
    ObligationReasonCode.TIMEOUT: FetchAttemptOutcome.INTERRUPTED,
    ObligationReasonCode.RATE_LIMITED: FetchAttemptOutcome.RATE_LIMITED,
    ObligationReasonCode.SERVER_ERROR: FetchAttemptOutcome.SERVER_ERROR,
    ObligationReasonCode.NOT_YET_KNOWABLE: FetchAttemptOutcome.UNAVAILABLE,
    ObligationReasonCode.FIELD_UNAVAILABLE: FetchAttemptOutcome.UNAVAILABLE,
    ObligationReasonCode.LOW_CONFIDENCE: FetchAttemptOutcome.UNAVAILABLE,
}
_TERMINAL_OUTCOMES: Mapping[ObligationTerminalState, FetchAttemptOutcome] = {
    ObligationTerminalState.SUCCESS: FetchAttemptOutcome.SUCCESS,
    ObligationTerminalState.UNCHANGED: FetchAttemptOutcome.UNCHANGED,
    ObligationTerminalState.UNAVAILABLE: FetchAttemptOutcome.UNAVAILABLE,
    ObligationTerminalState.SKIPPED_BY_POLICY: FetchAttemptOutcome.UNAVAILABLE,
    ObligationTerminalState.FAILED: FetchAttemptOutcome.FAILED,
}
# Spacing between the recorded retries of one work item. Small enough that every
# permitted attempt still precedes the terminal completion stamp below.
_RETRY_SPACING = timedelta(seconds=2)


@dataclass(frozen=True)
class CaptureTimeline:
    """The run's persistence stamps, all derived from its cutoff.

    `partition_start` is the frozen universe partition the run asserts values for:
    observations anchor `valid_from` there with an open `valid_to`, so the
    materializer selects them for that partition at any cutoff. `knowable_at` is
    the capture's own knowable time — inside the schedule policy's freshness
    window, which is what makes a captured cell `fresh` at the mart.
    """

    cutoff: datetime
    partition_start: datetime

    def __post_init__(self) -> None:
        for stamp in (self.cutoff, self.partition_start):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("capture timeline stamps must be timezone-aware")

    @classmethod
    def for_partition(cls, cutoff: datetime, partition: str) -> CaptureTimeline:
        """Build the timeline for a run whose obligations carry `partition` (a date)."""
        start = datetime.combine(date.fromisoformat(partition), datetime.min.time(), tzinfo=UTC)
        return cls(cutoff=cutoff, partition_start=start)

    @property
    def source_published_at(self) -> datetime:
        return self.cutoff - timedelta(hours=2)

    @property
    def first_attempt_at(self) -> datetime:
        return self.cutoff - timedelta(hours=1)

    @property
    def terminal_attempt_at(self) -> datetime:
        return self.cutoff - timedelta(minutes=59)

    @property
    def knowable_at(self) -> datetime:
        return self.cutoff - timedelta(minutes=58)

    @property
    def completed_at(self) -> datetime:
        return self.cutoff - timedelta(minutes=57)


@dataclass(frozen=True)
class ObligationBinding:
    """The planned coordinates the sink needs to persist one work item's outcome."""

    ordinal: int
    obligation: CaptureListObligation
    source_request: SourceRequest


class PostgresCaptureControlSink:
    """`ObligationSink` writing the capture-control tables the mart reads.

    Constructed from the same plan the executor's routing table was built from, so
    a work item the plan never declared fails closed rather than inventing rows.
    """

    def __init__(
        self,
        connection: Connection[Any],
        bindings: Mapping[str, ObligationBinding],
        *,
        source_label: str,
        timeline: CaptureTimeline,
        retry: RetryPolicy,
    ) -> None:
        self._connection = connection
        self._repository = PostgresCaptureControlRepository(connection)
        self._bindings = dict(bindings)
        self._source_label = source_label
        self._timeline = timeline
        self._retry = retry

    def record_outcome(
        self,
        work_item: CaptureWorkItem,
        *,
        attempt_reasons: Sequence[ObligationReasonCode | None],
        terminal_state: ObligationTerminalState,
        success: FetchSuccess | None,
    ) -> None:
        binding = self._bindings.get(work_item.work_item_id)
        if binding is None:
            raise LookupError(f"work item is not part of the persisted plan: {work_item.work_item_id}")
        if not attempt_reasons:
            raise ValueError("a terminal obligation must record at least one attempt")

        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=self._retry)
        vintage_id: str | None = None
        if success is not None and success.record is not None:
            vintage_id = self._persist_content(binding, success)

        final_attempt_id = self._replay_attempts(
            ledger,
            attempt_reasons,
            terminal_state=terminal_state,
            source_vintage_id=vintage_id,
        )
        if success is not None and success.record is not None:
            self._persist_observation(binding, success, source_vintage_id=vintage_id)
            for corroboration in success.corroborations:
                self._persist_corroboration(binding, corroboration)

        reasons = tuple(sorted({reason.value for reason in attempt_reasons if reason is not None}))
        self._repository.put_obligation_result(
            binding.obligation.obligation_id,
            ListObligationResult(
                obligation_id=binding.obligation.obligation.obligation_id,
                terminal_state=terminal_state,
                completed_at=self._timeline.completed_at,
                final_attempt_id=final_attempt_id,
                reason_codes=reasons or (terminal_state.value,),
            ),
        )

    # -- attempts ---------------------------------------------------------------------

    def _replay_attempts(
        self,
        ledger: AttemptLedger,
        attempt_reasons: Sequence[ObligationReasonCode | None],
        *,
        terminal_state: ObligationTerminalState,
        source_vintage_id: str | None,
    ) -> str:
        """Persist one attempt per executed fetch; the last one carries the terminal outcome."""
        last_attempt_id = ""
        for index, reason in enumerate(attempt_reasons):
            terminal = index == len(attempt_reasons) - 1
            started_at = self._timeline.first_attempt_at + index * _RETRY_SPACING
            attempt = ledger.start(started_at=started_at)
            if terminal:
                outcome = _TERMINAL_OUTCOMES[terminal_state]
                completed_at = self._timeline.terminal_attempt_at
            else:
                # A non-terminal attempt is a bounded retry by construction: the
                # executor only loops on RETRY dispositions.
                outcome = _ATTEMPT_OUTCOMES[reason] if reason is not None else FetchAttemptOutcome.SUCCESS
                completed_at = started_at + _RETRY_SPACING / 2
            result = ledger.finish(
                attempt=attempt,
                completed_at=completed_at,
                outcome=outcome,
                error_code=None if reason is None else reason.value,
                status_code=200 if outcome is FetchAttemptOutcome.SUCCESS else None,
                source_vintage_id=source_vintage_id if terminal and outcome is FetchAttemptOutcome.SUCCESS else None,
            )
            self._repository.put_attempt(attempt)
            self._repository.put_attempt_result(result)
            last_attempt_id = attempt.attempt_id
        return last_attempt_id

    # -- content ----------------------------------------------------------------------

    def _persist_content(self, binding: ObligationBinding, success: FetchSuccess) -> str:
        """Land the immutable raw bytes and the source vintage; returns the vintage id."""
        vintage = self._put_vintage(
            source_request_id=binding.source_request.source_request_id,
            ordinal=binding.ordinal,
            source=self._source_label,
            raw_sha256=success.raw_sha256,
            byte_length=success.raw_byte_length,
        )
        return vintage.source_vintage_id

    def _put_vintage(
        self,
        *,
        source_request_id: str,
        ordinal: int,
        source: str,
        raw_sha256: str,
        byte_length: int,
    ) -> SourceVintage:
        record_id = f"{source}:{ordinal}"
        raw_fetch_id = self._insert_fetch(
            source=source, record_id=record_id, sha256=raw_sha256, byte_length=byte_length
        )
        vintage = SourceVintage(
            source_request_id=source_request_id,
            source_record_id=record_id,
            source_published_at=self._timeline.source_published_at,
            raw_object_id=f"raw-object:{raw_sha256}",
        )
        self._repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
        return vintage

    def _insert_fetch(self, *, source: str, record_id: str, sha256: str, byte_length: int) -> int:
        """Idempotent `raw.fetches` landing on the table's (source, source_record_id,
        payload_sha256) unique key: a retried tick with unchanged bytes reuses the
        existing row; changed source bytes land a NEW append-only vintage row."""
        fetched_at = self._timeline.source_published_at
        row = self._connection.execute(
            "insert into raw.fetches (source, source_record_id, payload_sha256, object_uri, content_type, "
            "byte_length, fetched_at, recorded_at, metadata) "
            "values (%s, %s, %s, %s, 'application/json', %s, %s, %s, '{}'::jsonb) "
            "on conflict (source, source_record_id, payload_sha256) do nothing returning id",
            (source, record_id, sha256, f"s3://{source}/{sha256}", byte_length, fetched_at, fetched_at),
        ).fetchone()
        if row is not None:
            return int(row[0])
        existing = self._connection.execute(
            "select id from raw.fetches where source = %s and source_record_id = %s and payload_sha256 = %s",
            (source, record_id, sha256),
        ).fetchone()
        if existing is None:  # pragma: no cover - the conflicting row exists by definition
            raise RuntimeError(f"raw.fetches landing for {record_id} neither inserted nor found")
        return int(existing[0])

    def _persist_observation(
        self,
        binding: ObligationBinding,
        success: FetchSuccess,
        *,
        source_vintage_id: str | None,
    ) -> None:
        assert success.record is not None and source_vintage_id is not None
        self._put_observation(
            binding,
            payload=dict(success.record.payload),
            parser_version=success.record.parser_version,
            mapping_version=success.record.mapping_version,
            confidence=success.confidence,
            source_vintage_id=source_vintage_id,
        )

    def _persist_corroboration(self, binding: ObligationBinding, corroboration: Corroboration) -> None:
        """Persist a second origin as its own request/vintage/observation for the same cell.

        The fusion engine then reconciles two real assertions (#343); the materializer
        ignores it, because a snapshot binds only the terminal attempt's vintage.
        """
        source = f"{corroboration.origin}-{self._source_label}"
        request = self._corroborating_request(binding, origin=corroboration.origin, source=source)
        self._repository.put_source_request(request)
        vintage = self._put_vintage(
            source_request_id=request.source_request_id,
            ordinal=binding.ordinal,
            source=source,
            raw_sha256=corroboration.raw_sha256,
            byte_length=corroboration.raw_byte_length,
        )
        self._put_observation(
            binding,
            payload=dict(corroboration.record.payload),
            parser_version=corroboration.record.parser_version,
            mapping_version=corroboration.record.mapping_version,
            confidence=corroboration.confidence,
            source_vintage_id=vintage.source_vintage_id,
        )

    def _corroborating_request(self, binding: ObligationBinding, *, origin: str, source: str) -> SourceRequest:
        obligation = binding.obligation
        coordinate: dict[str, Any] = {
            "ordinal": binding.ordinal,
            "subject": obligation.subject.model_dump(mode="json"),
            "requirement": obligation.capture_requirement_id,
            "partition": obligation.partition,
            "origin": origin,
        }
        return SourceRequest(
            source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': source})}",
            source_policy_id=f"source-policy:{source}",
            request_fingerprint_version=f"{source}:v1",
            canonical_request_sha256=canonical_sha256(coordinate),
            subject_refs=(obligation.subject,),
            capture_requirement_ids=(obligation.capture_requirement_id,),
            partition=obligation.partition,
        )

    def _put_observation(
        self,
        binding: ObligationBinding,
        *,
        payload: dict[str, Any],
        parser_version: str,
        mapping_version: str,
        confidence: Decimal,
        source_vintage_id: str,
    ) -> None:
        obligation = binding.obligation
        semantic_type = obligation.capture_requirement_id.removesuffix(":v1")
        observation = NormalizedObservation(
            semantic_type=semantic_type,
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=self._timeline.partition_start,
            valid_to=None,
            knowable_at=self._timeline.knowable_at,
            source_vintage_id=source_vintage_id,
            parser_version=parser_version,
            mapping_version=mapping_version,
            normalized_payload_sha256=canonical_sha256(payload),
        )
        self._repository.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload=payload,
            confidence=confidence,
            freshness_state="fresh",
        )
