"""Append-only PostgreSQL persistence and bounded TOPT read models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from truealpha_contracts.capture_control import (
    CaptureListObligation,
    CaptureListVersion,
    CaptureObligationWorkBinding,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    FetchAttempt,
    FetchAttemptResult,
    ListObligationResult,
    NormalizedObservation,
    RetryPolicy,
    SourceRequest,
    SourceVintage,
)


class CaptureRepositoryConflictError(RuntimeError):
    """Content-addressed identity is already bound to different content."""


def persist_run_report(
    connection: Connection[Any],
    report: Mapping[str, Any],
    *,
    table: str,
    report_id_prefix: str,
    columns: tuple[str, ...],
    exclude_from_hash: tuple[str, ...] = (),
) -> str:
    """Insert one content-addressed run report row."""
    digest = canonical_sha256({key: value for key, value in report.items() if key not in exclude_from_hash})
    report_id = f"{report_id_prefix}:{digest}"
    insert_columns = ("report_id", "content_sha256", *columns, "payload")
    statement = sql.SQL(
        "insert into {table} ({columns}) values ({placeholders}) on conflict (report_id) do nothing"
    ).format(
        table=sql.Identifier(*table.split(".")),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
        placeholders=sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns),
    )
    connection.execute(statement, (report_id, digest, *(report[column] for column in columns), Jsonb(dict(report))))
    return report_id


@dataclass(frozen=True)
class ToptCaptureStatus:
    run_id: str
    campaign_id: str
    environment: str
    cutoff: datetime
    universe_id: str
    universe_version: str
    universe_sha256: str
    obligation_count: int
    terminal_count: int
    success_count: int
    unchanged_count: int
    unavailable_count: int
    skipped_count: int
    failed_count: int
    complete: bool


@dataclass(frozen=True)
class ToptCaptureMetaInfo:
    run_id: str
    obligation_id: str
    logical_obligation_id: str | None
    subject_kind: str
    subject_id: str
    capture_requirement_id: str
    partition_key: str
    work_item_id: str | None
    source_request_id: str | None
    source_registry_entry_id: str | None
    source_policy_id: str | None
    request_fingerprint_version: str | None
    terminal_state: str | None
    reason_codes: tuple[str, ...] | None
    completed_at: datetime | None
    attempt_count: int
    final_status_code: int | None
    observation_id: str | None
    semantic_version: str | None
    parser_version: str | None
    mapping_version: str | None
    confidence: Decimal | None
    freshness_state: str | None
    knowable_at: datetime | None
    recorded_at: datetime | None


class PostgresCaptureControlRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    @staticmethod
    def _payload(value: Any) -> dict[str, Any]:
        return value.model_dump(mode="json", exclude_computed_fields=True)

    def _insert_idempotent(
        self, table: str, id_column: str, identity: str, sha: str, stmt: str, params: tuple[Any, ...]
    ) -> bool:
        if self._connection.execute(stmt, params).fetchone():
            return True
        existing = self._connection.execute(
            f"select content_sha256 from {table} where {id_column} = %s",  # noqa: S608
            (identity,),
        ).fetchone()
        if existing is None:
            raise RuntimeError(f"insert into {table} returned no row and no conflict")
        if existing[0] != sha:
            raise CaptureRepositoryConflictError(f"{identity} is already bound to different content")
        return False

    def _put(self, table: str, id_column: str, obj: Any, attrs: tuple[str, ...] = (), **extra: Any) -> bool:
        identity, sha = getattr(obj, id_column), obj.content_sha256
        row = {id_column: identity, "content_sha256": sha, **{k: getattr(obj, k) for k in attrs}, **extra}
        cols = ", ".join(row)
        placeholders = ", ".join(("%s",) * len(row))
        stmt = f"insert into {table} ({cols}) values ({placeholders}) on conflict ({id_column}) do nothing returning {id_column}"  # noqa: S608
        return self._insert_idempotent(table, id_column, identity, sha, stmt, tuple(row.values()))

    def put_schedule_policy(self, policy: CaptureSchedulePolicy) -> bool:
        return self._put(
            "raw.capture_schedule_policies",
            "schedule_policy_id",
            policy,
            ("policy_version", "demanded_cadence", "provider_availability_cadence", "freshness_max_age"),
            semantic_freshness_max_age=Jsonb(
                {k: f"{v.total_seconds()} seconds" for k, v in policy.semantic_freshness_max_age.items()}
            ),
            retry_policy=Jsonb(policy.retry.model_dump(mode="json")),
            payload=Jsonb(self._payload(policy)),
        )

    def put_campaign(self, campaign: CaptureCampaign) -> bool:
        payload = self._payload(campaign)
        return self._put(
            "raw.capture_campaigns",
            "campaign_id",
            campaign,
            ("cutoff",),
            policy_id=campaign.campaign_policy_id,
            environment=campaign.environment.value,
            cutoff_canonical=payload["cutoff"],
            universe_refs=Jsonb(payload["universe_refs"]),
        )

    def put_run(self, run: CaptureRun) -> bool:
        return self._put(
            "raw.capture_runs",
            "run_id",
            run,
            ("campaign_id", "run_sequence", "schedule_policy_id", "capture_scope_id"),
        )

    def put_list_version(self, version: CaptureListVersion) -> bool:
        payload = self._payload(version)
        created = self._put(
            "raw.capture_list_versions",
            "list_version_id",
            version,
            ("effective_at",),
            universe_id=version.universe.universe_id,
            universe_version=version.universe.universe_version,
            universe_sha256=version.universe.content_sha256,
            effective_at_canonical=payload["effective_at"],
            member_count=len(version.members),
            members=Jsonb(payload["members"]),
        )
        if version.members:
            self._connection.cursor().executemany(
                "insert into raw.capture_list_version_members (list_version_id, member_ordinal, subject_kind, subject_id) "
                "values (%s, %s, %s, %s) on conflict do nothing",
                [(version.list_version_id, i, m.kind.value, m.id) for i, m in enumerate(version.members, start=1)],
            )
        return created

    def bind_campaign_list(self, campaign_id: str, list_version_id: str) -> None:
        self._connection.execute(
            "insert into raw.capture_campaign_list_versions (campaign_id, list_version_id) values (%s, %s) on conflict do nothing",
            (campaign_id, list_version_id),
        )

    def put_obligation(self, campaign_id: str, obligation: CaptureListObligation) -> bool:
        return self._put(
            "raw.capture_obligations",
            "obligation_id",
            obligation,
            ("run_id", "list_version_id", "capture_requirement_id"),
            campaign_id=campaign_id,
            subject_kind=obligation.subject.kind.value,
            subject_id=obligation.subject.id,
            partition_key=obligation.partition,
        )

    def put_source_request(self, request: SourceRequest) -> bool:
        payload = self._payload(request)
        return self._put(
            "raw.capture_source_requests",
            "source_request_id",
            request,
            (
                "source_registry_entry_id",
                "source_policy_id",
                "request_fingerprint_version",
                "canonical_request_sha256",
            ),
            subject_refs=Jsonb(payload["subject_refs"]),
            capture_requirement_ids=list(request.capture_requirement_ids),
            partition_key=request.partition,
            payload=Jsonb(payload),
        )

    def put_work_item(self, item: CaptureWorkItem, retry: RetryPolicy) -> bool:
        retryable = [v.value for v in retry.retryable_outcomes]
        terminal = [v.value for v in retry.terminal_outcomes]
        envelope_sha256 = canonical_sha256(
            {
                "work_item_id": item.work_item_id,
                "content_sha256": item.content_sha256,
                "maximum_attempts": retry.max_attempts,
                "retryable_outcomes": retryable,
                "terminal_outcomes": terminal,
            }
        )
        return self._put(
            "raw.capture_work_items",
            "work_item_id",
            item,
            ("campaign_id", "source_request_id", "schedule_policy_id"),
            maximum_attempts=retry.max_attempts,
            retryable_outcomes=retryable,
            terminal_outcomes=terminal,
            storage_envelope_sha256=envelope_sha256,
        )

    def put_binding(self, binding: CaptureObligationWorkBinding) -> bool:
        return self._put(
            "raw.capture_obligation_work_bindings", "binding_id", binding, ("obligation_id", "work_item_id")
        )

    def put_attempt(self, attempt: FetchAttempt) -> bool:
        return self._put(
            "raw.capture_attempts",
            "attempt_id",
            attempt,
            ("work_item_id", "attempt_number", "started_at"),
            started_at_canonical=self._payload(attempt)["started_at"],
        )

    def put_attempt_result(self, result: FetchAttemptResult) -> bool:
        return self._put(
            "raw.capture_attempt_results",
            "attempt_result_id",
            result,
            ("attempt_id", "completed_at", "status_code", "source_vintage_id", "reused_source_vintage_id"),
            completed_at_canonical=self._payload(result)["completed_at"],
            outcome=result.outcome.value,
            reason_codes=list(result.reason_codes),
        )

    def put_source_vintage(self, vintage: SourceVintage, *, raw_fetch_id: int) -> bool:
        return self._put(
            "raw.capture_source_vintages",
            "source_vintage_id",
            vintage,
            ("source_request_id", "source_record_id", "source_published_at", "raw_object_id"),
            raw_fetch_id=raw_fetch_id,
            payload=Jsonb(self._payload(vintage)),
        )

    def put_observation(
        self,
        capture_obligation_id: str,
        observation: NormalizedObservation,
        *,
        normalized_payload: dict[str, Any],
        confidence: Decimal | None = None,
        freshness_state: str = "unknown",
    ) -> bool:
        if confidence is not None and not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("observation confidence must be between zero and one")
        if canonical_sha256(normalized_payload) != observation.normalized_payload_sha256:
            raise ValueError("normalized payload does not match the observation hash")
        created = self._put(
            "staging.capture_normalized_observations",
            "observation_id",
            observation,
            (
                "source_vintage_id",
                "semantic_type",
                "semantic_version",
                "valid_from",
                "valid_to",
                "knowable_at",
                "parser_version",
                "mapping_version",
                "normalized_payload_sha256",
                "is_restatement",
                "supersedes_observation_id",
            ),
            capture_obligation_id=capture_obligation_id,
            subject_kind=observation.subject.kind.value,
            subject_id=observation.subject.id,
            confidence=confidence,
            freshness_state=freshness_state,
            payload=Jsonb(self._payload(observation)),
        )
        self.bind_observation(capture_obligation_id, observation.observation_id)
        self._put_observation_payload(observation, normalized_payload)
        return created

    def _put_observation_payload(self, observation: NormalizedObservation, normalized_payload: dict[str, Any]) -> bool:
        payload_sha256 = observation.normalized_payload_sha256
        inserted = self._connection.execute(
            "insert into staging.capture_observation_payloads (observation_id, normalized_payload_sha256, normalized_payload) "
            "values (%s, %s, %s) on conflict (observation_id) do nothing returning observation_id",
            (observation.observation_id, payload_sha256, Jsonb(normalized_payload)),
        ).fetchone()
        if inserted:
            return True
        row = self._connection.execute(
            "select normalized_payload_sha256, normalized_payload from staging.capture_observation_payloads where observation_id = %s",
            (observation.observation_id,),
        ).fetchone()
        if row is None or row[0] != payload_sha256 or row[1] != normalized_payload:
            raise CaptureRepositoryConflictError(
                f"{observation.observation_id} is already bound to a different normalized payload"
            )
        return False

    def bind_observation(self, capture_obligation_id: str, observation_id: str) -> None:
        self._connection.execute(
            "insert into staging.capture_observation_obligations (capture_obligation_id, observation_id) values (%s, %s) on conflict do nothing",
            (capture_obligation_id, observation_id),
        )

    def put_obligation_result(self, capture_obligation_id: str, result: ListObligationResult) -> bool:
        expected = self._connection.execute(
            "select 'list-obligation:' || raw.canonical_sha256(jsonb_build_object("
            "'kind', 'list-obligation', 'identity', jsonb_build_object('run_id', obligation.run_id, "
            "'universe_ref', jsonb_build_object('universe_id', version.universe_id, 'universe_version', "
            "version.universe_version, 'content_sha256', version.universe_sha256), 'subject', jsonb_build_object("
            "'kind', obligation.subject_kind, 'id', obligation.subject_id), 'capture_requirement_id', "
            "obligation.capture_requirement_id, 'partition', obligation.partition_key))) "
            "from raw.capture_obligations obligation join raw.capture_list_versions version using (list_version_id) "
            "where obligation.obligation_id = %s",
            (capture_obligation_id,),
        ).fetchone()
        if expected is None:
            raise LookupError(f"capture obligation not found: {capture_obligation_id}")
        if expected[0] != result.obligation_id:
            raise ValueError("terminal result logical obligation does not match capture obligation")
        return self._put(
            "raw.capture_obligation_results",
            "result_id",
            result,
            ("completed_at", "final_attempt_id"),
            capture_obligation_id=capture_obligation_id,
            logical_obligation_id=result.obligation_id,
            terminal_state=result.terminal_state.value,
            reason_codes=list(result.reason_codes),
            payload=Jsonb(self._payload(result)),
        )

    def status(self, run_id: str) -> ToptCaptureStatus:
        row = self._connection.execute(
            "select run_id, campaign_id, environment, cutoff, universe_id, universe_version, universe_sha256, "
            "obligation_count, terminal_count, success_count, unchanged_count, unavailable_count, skipped_count, "
            "failed_count, complete from mart.topt_capture_status where run_id = %s",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"capture run not found: {run_id}")
        return ToptCaptureStatus(*row)

    def meta_info(self, run_id: str, *, limit: int = 100, offset: int = 0) -> tuple[ToptCaptureMetaInfo, ...]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("meta_info pagination is outside the bounded range")
        rows = self._connection.execute(
            "select run_id, obligation_id, logical_obligation_id, subject_kind, subject_id, capture_requirement_id, "
            "partition_key, work_item_id, source_request_id, source_registry_entry_id, source_policy_id, "
            "request_fingerprint_version, terminal_state, reason_codes, completed_at, attempt_count, final_status_code, "
            "observation_id, semantic_version, parser_version, mapping_version, confidence, freshness_state, "
            "knowable_at, recorded_at from mart.topt_capture_meta_info where run_id = %s order by obligation_id limit %s offset %s",
            (run_id, limit, offset),
        ).fetchall()
        return tuple(
            ToptCaptureMetaInfo(
                *(list(row[:13]) + [tuple(row[13]) if row[13] is not None else None] + list(row[14:]))  # type: ignore[arg-type]
            )
            for row in rows
        )
