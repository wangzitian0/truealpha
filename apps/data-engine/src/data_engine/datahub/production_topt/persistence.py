"""Capture-control persistence for the TOPT executor path (#171 A1, ADR A1)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import Connection
from truealpha_contracts.capture_control import CaptureListObligation
from truealpha_contracts.common import canonical_sha256
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
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode
from truealpha_contracts.ports import RawObjectStore

from data_engine import raw_store
from data_engine.datahub.control_plane import AttemptLedger
from data_engine.datahub.production_topt.corroboration_audit import PERSIST, record_lost_corroboration
from data_engine.datahub.production_topt.executor import (
    SERVED_BY_FAILOVER,
    Corroboration,
    FetchSuccess,
    RawResponse,
)
from data_engine.datahub.production_topt.source_registrations import registration_for
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.datahub.resolve_coordinates import parse_alias

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
    ObligationReasonCode.DEFERRED_CAPACITY: FetchAttemptOutcome.UNAVAILABLE,
}
_TERMINAL_OUTCOMES: Mapping[ObligationTerminalState, FetchAttemptOutcome] = {
    ObligationTerminalState.SUCCESS: FetchAttemptOutcome.SUCCESS,
    ObligationTerminalState.UNCHANGED: FetchAttemptOutcome.UNCHANGED,
    ObligationTerminalState.UNAVAILABLE: FetchAttemptOutcome.UNAVAILABLE,
    ObligationTerminalState.SKIPPED_BY_POLICY: FetchAttemptOutcome.UNAVAILABLE,
    ObligationTerminalState.FAILED: FetchAttemptOutcome.FAILED,
}
_RETRY_SPACING = timedelta(seconds=2)


def build_source_request(
    obligation: CaptureListObligation,
    *,
    ordinal: int,
    source: str,
    origin: str | None = None,
) -> SourceRequest:
    """Build a content-addressed `SourceRequest` signed by the owning semantic registration."""
    coordinate: dict[str, Any] = {
        "ordinal": ordinal,
        "subject": obligation.subject.model_dump(mode="json"),
        "requirement": obligation.capture_requirement_id,
        "partition": obligation.partition,
    }
    if origin is not None:
        coordinate["origin"] = origin
    registration = registration_for(obligation.capture_requirement_id.removesuffix(":v1"))
    return SourceRequest(
        source_registry_entry_id=registration.entry_id,
        source_policy_id=registration.policy_id,
        request_fingerprint_version=f"{source}:v1",
        canonical_request_sha256=canonical_sha256(coordinate),
        subject_refs=(obligation.subject,),
        capture_requirement_ids=(obligation.capture_requirement_id,),
        partition=obligation.partition,
    )


@dataclass(frozen=True)
class CaptureTimeline:
    """Persistence timestamps for one capture run derived from its cutoff."""

    cutoff: datetime
    partition_start: datetime

    def __post_init__(self) -> None:
        for stamp in (self.cutoff, self.partition_start):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("capture timeline stamps must be timezone-aware")

    @classmethod
    def for_partition(cls, cutoff: datetime, partition: str) -> CaptureTimeline:
        return cls(
            cutoff=cutoff,
            partition_start=datetime.combine(date.fromisoformat(partition), datetime.min.time(), tzinfo=UTC),
        )

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


def _require_aware(stamp: datetime | None) -> datetime | None:
    if stamp is not None and (stamp.tzinfo is None or stamp.utcoffset() is None):
        raise ValueError("adapter transaction_time must be timezone-aware")
    return stamp


@dataclass(frozen=True)
class ObligationBinding:
    """Planned coordinates needed to persist one work item's outcome."""

    ordinal: int
    obligation: CaptureListObligation
    source_request: SourceRequest


class PostgresCaptureControlSink:
    """`ObligationSink` persisting capture-control and observation rows on the caller's connection."""

    def __init__(
        self,
        connection: Connection[Any],
        bindings: Mapping[str, ObligationBinding],
        *,
        source_label: str,
        timeline: CaptureTimeline,
        retry: RetryPolicy,
        object_store: RawObjectStore | None = None,
        freshness_windows: Mapping[str, timedelta] | None = None,
        default_freshness_max_age: timedelta = timedelta(days=2),
        coordinates: Mapping[str, tuple[str, str, str, str]] | None = None,
        raw_coordinates: Mapping[str, tuple[str, str, str, str]] | None = None,
    ) -> None:
        self._connection = connection
        self._repository = PostgresCaptureControlRepository(connection)
        self._bindings = dict(bindings)
        self._source_label = source_label
        self._timeline = timeline
        self._retry = retry
        self._freshness_windows = dict(freshness_windows or {})
        self._default_freshness_max_age = default_freshness_max_age
        self._object_store = object_store
        self._coordinates = dict(coordinates) if coordinates else None
        self._raw_coordinates = dict(raw_coordinates) if raw_coordinates else None

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
        if len(attempt_reasons) > self._retry.max_attempts:
            raise ValueError(
                f"{work_item.work_item_id} ran {len(attempt_reasons)} attempts, "
                f"beyond the {self._retry.max_attempts} its retry policy permits"
            )
        if success is not None and success.record is None:
            raise ValueError(f"{work_item.work_item_id} succeeded without a normalized record to persist")
        if success is not None:
            served_by_failover = success.served_by_failover is not None
            if terminal_state is not ObligationTerminalState.SUCCESS:
                raise ValueError(f"{work_item.work_item_id} handed over a success under {terminal_state.value}")
            if served_by_failover != (attempt_reasons[-1] is not None):
                raise ValueError(
                    f"{work_item.work_item_id}: a failover success must follow a primary failure, "
                    "and a primary success must end its attempts cleanly"
                )

        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=self._retry)
        vintage_id = (
            self._persist_content(binding, success) if success is not None and success.record is not None else None
        )
        final_attempt_id = self._replay_attempts(
            ledger,
            attempt_reasons,
            terminal_state=terminal_state,
            source_vintage_id=vintage_id,
        )
        if success is not None and success.record is not None:
            self._persist_observation(binding, success, source_vintage_id=vintage_id)
            for corroboration in success.corroborations:
                self._persist_corroboration_or_record_loss(binding, corroboration)

        codes = {reason.value for reason in attempt_reasons if reason is not None}
        if success is not None and success.served_by_failover is not None:
            codes.add(SERVED_BY_FAILOVER)
        reasons = tuple(sorted(codes))
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

    def _replay_attempts(
        self,
        ledger: AttemptLedger,
        attempt_reasons: Sequence[ObligationReasonCode | None],
        *,
        terminal_state: ObligationTerminalState,
        source_vintage_id: str | None,
    ) -> str:
        last_attempt_id = ""
        for index, reason in enumerate(attempt_reasons):
            terminal = index == len(attempt_reasons) - 1
            started_at = self._timeline.first_attempt_at + index * _RETRY_SPACING
            attempt = ledger.start(started_at=started_at)
            if terminal:
                outcome = _TERMINAL_OUTCOMES[terminal_state]
                completed_at = self._timeline.terminal_attempt_at
            else:
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

    def _persist_content(self, binding: ObligationBinding, success: FetchSuccess) -> str:
        return self._put_vintage(
            source_request_id=binding.source_request.source_request_id,
            raw=success.raw,
            source_published_at=success.transaction_time,
        ).source_vintage_id

    def _put_vintage(
        self, *, source_request_id: str, raw: RawResponse, source_published_at: datetime | None = None
    ) -> SourceVintage:
        ingested_at = datetime.now(UTC)
        raw_fetch_id = raw_store.insert_fetch(
            self._connection,
            source=raw.source,
            source_record_id=raw.record_id,
            body=raw.body,
            content_type=raw.content_type,
            fetched_at=ingested_at,
            recorded_at=ingested_at,
            store=self._object_store,
        )
        vintage = SourceVintage(
            source_request_id=source_request_id,
            source_record_id=raw.record_id,
            source_published_at=_require_aware(source_published_at) or self._timeline.source_published_at,
            raw_object_id=f"raw-object:{raw.sha256}",
        )
        self._repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
        return vintage

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
            knowable_at=success.transaction_time,
            valid_from=datetime.combine(success.valid_from, datetime.min.time(), tzinfo=UTC),
        )

    def _persist_corroboration_or_record_loss(self, binding: ObligationBinding, corroboration: Corroboration) -> None:
        try:
            with self._connection.transaction():
                self._persist_corroboration(binding, corroboration)
        except Exception as error:  # noqa: BLE001
            record_lost_corroboration(corroboration.origin, PERSIST, binding.obligation.subject.id, error)

    def _persist_corroboration(self, binding: ObligationBinding, corroboration: Corroboration) -> None:
        source = f"{corroboration.origin}-{self._source_label}"
        request = self._corroborating_request(binding, origin=corroboration.origin, source=source)
        self._repository.put_source_request(request)
        vintage = self._put_vintage(
            source_request_id=request.source_request_id,
            raw=corroboration.raw,
            source_published_at=corroboration.transaction_time,
        )
        self._put_observation(
            binding,
            payload=dict(corroboration.record.payload),
            parser_version=corroboration.record.parser_version,
            mapping_version=corroboration.record.mapping_version,
            confidence=corroboration.confidence,
            source_vintage_id=vintage.source_vintage_id,
            knowable_at=corroboration.transaction_time,
            valid_from=corroboration.transaction_time,
        )

    def _corroborating_request(self, binding: ObligationBinding, *, origin: str, source: str) -> SourceRequest:
        return build_source_request(binding.obligation, ordinal=binding.ordinal, source=source, origin=origin)

    def _put_observation(
        self,
        binding: ObligationBinding,
        *,
        payload: dict[str, Any],
        parser_version: str,
        mapping_version: str,
        confidence: Decimal | None = None,
        source_vintage_id: str,
        knowable_at: datetime,
        valid_from: datetime,
    ) -> None:
        obligation = binding.obligation
        semantic_type = obligation.capture_requirement_id.removesuffix(":v1")
        if knowable_at.tzinfo is None or knowable_at.utcoffset() is None:
            raise ValueError("adapter transaction_time must be timezone-aware")
        observation = NormalizedObservation(
            semantic_type=semantic_type,
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=valid_from,
            valid_to=None,
            knowable_at=knowable_at,
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
            freshness_state=(
                "fresh"
                if self._timeline.cutoff - knowable_at
                <= self._freshness_windows.get(semantic_type, self._default_freshness_max_age)
                else "stale"
            ),
        )
        self._put_capture_entity_refs(observation.observation_id, binding=binding, knowable_at=knowable_at)

    def _put_capture_entity_refs(
        self,
        observation_id: str,
        *,
        binding: ObligationBinding,
        knowable_at: datetime,
    ) -> None:
        if not self._raw_coordinates or not self._coordinates:
            return
        subject_id = binding.obligation.subject.id
        raw_trio = self._raw_coordinates.get(subject_id)
        resolved_trio = self._coordinates.get(subject_id)
        if not raw_trio or not resolved_trio:
            return

        for role, entity_id_str, raw_id in (
            ("issuer", resolved_trio[0], raw_trio[0]),
            ("instrument", resolved_trio[1], raw_trio[1]),
            ("listing", resolved_trio[2], raw_trio[2]),
        ):
            try:
                entity_uuid = uuid.UUID(entity_id_str)
            except (ValueError, TypeError):
                continue
            scheme, value = parse_alias(raw_id, role)
            with self._connection.transaction():
                self._connection.execute(
                    """
                    insert into staging.capture_entity_refs (
                        observation_id, role, entity_id, scheme, value, known_at
                    ) values (%s, %s, %s, %s, %s, %s)
                    on conflict (observation_id, role) do nothing
                    """,
                    (observation_id, role, entity_uuid, scheme, value, knowable_at),
                )
