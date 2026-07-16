"""Append-only PostgreSQL persistence and bounded TOPT read models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts import canonical_sha256
from truealpha_contracts.capture_control import (
    CaptureCheckpoint,
    CaptureListObligation,
    CaptureListVersion,
    CaptureObligationWorkBinding,
)
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
    """A content-addressed identity is already bound to different content."""


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

    def _check_existing(self, table: str, id_column: str, identity: str, content_sha256: str) -> bool:
        row = self._connection.execute(
            f"select content_sha256 from {table} where {id_column} = %s",  # noqa: S608 - identifiers are constants
            (identity,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"insert into {table} returned no row and no conflict")
        if row[0] != content_sha256:
            raise CaptureRepositoryConflictError(f"{identity} is already bound to different content")
        return False

    def _existing_matches(self, table: str, id_column: str, identity: str, content_sha256: str) -> bool:
        row = self._connection.execute(
            f"select content_sha256 from {table} where {id_column} = %s",  # noqa: S608 - identifiers are constants
            (identity,),
        ).fetchone()
        if row is None:
            return False
        if row[0] != content_sha256:
            raise CaptureRepositoryConflictError(f"{identity} is already bound to different content")
        return True

    def put_schedule_policy(self, policy: CaptureSchedulePolicy) -> bool:
        payload = self._payload(policy)
        inserted = self._connection.execute(
            """
            insert into raw.capture_schedule_policies (
                schedule_policy_id, content_sha256, policy_version, demanded_cadence,
                provider_availability_cadence, freshness_max_age, retry_policy, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (schedule_policy_id) do nothing returning schedule_policy_id
            """,
            (
                policy.schedule_policy_id,
                policy.content_sha256,
                policy.policy_version,
                policy.demanded_cadence,
                policy.provider_availability_cadence,
                policy.freshness_max_age,
                Jsonb(policy.retry.model_dump(mode="json")),
                Jsonb(payload),
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_schedule_policies", "schedule_policy_id", policy.schedule_policy_id, policy.content_sha256
            )
        )

    def put_campaign(self, campaign: CaptureCampaign) -> bool:
        payload = self._payload(campaign)
        inserted = self._connection.execute(
            """
            insert into raw.capture_campaigns (
                campaign_id, content_sha256, policy_id, environment, cutoff,
                cutoff_canonical, universe_refs
            ) values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (campaign_id) do nothing returning campaign_id
            """,
            (
                campaign.campaign_id,
                campaign.content_sha256,
                campaign.campaign_policy_id,
                campaign.environment.value,
                campaign.cutoff,
                payload["cutoff"],
                Jsonb(payload["universe_refs"]),
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_campaigns", "campaign_id", campaign.campaign_id, campaign.content_sha256
            )
        )

    def put_run(self, run: CaptureRun) -> bool:
        inserted = self._connection.execute(
            """
            insert into raw.capture_runs (
                run_id, campaign_id, run_sequence, schedule_policy_id,
                capture_scope_id, content_sha256
            ) values (%s, %s, %s, %s, %s, %s)
            on conflict (run_id) do nothing returning run_id
            """,
            (
                run.run_id,
                run.campaign_id,
                run.run_sequence,
                run.schedule_policy_id,
                run.capture_scope_id,
                run.content_sha256,
            ),
        ).fetchone()
        return True if inserted else self._check_existing("raw.capture_runs", "run_id", run.run_id, run.content_sha256)

    def put_list_version(self, version: CaptureListVersion) -> bool:
        payload = self._payload(version)
        inserted = self._connection.execute(
            """
            insert into raw.capture_list_versions (
                list_version_id, universe_id, universe_version, universe_sha256,
                effective_at, effective_at_canonical, member_count, members, content_sha256
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (list_version_id) do nothing returning list_version_id
            """,
            (
                version.list_version_id,
                version.universe.universe_id,
                version.universe.universe_version,
                version.universe.content_sha256,
                version.effective_at,
                payload["effective_at"],
                len(version.members),
                Jsonb(payload["members"]),
                version.content_sha256,
            ),
        ).fetchone()
        created = (
            True
            if inserted
            else self._check_existing(
                "raw.capture_list_versions", "list_version_id", version.list_version_id, version.content_sha256
            )
        )
        for ordinal, member in enumerate(version.members, start=1):
            self._connection.execute(
                """
                insert into raw.capture_list_version_members (
                    list_version_id, member_ordinal, subject_kind, subject_id
                ) values (%s, %s, %s, %s) on conflict do nothing
                """,
                (version.list_version_id, ordinal, member.kind.value, member.id),
            )
        return created

    def bind_campaign_list(self, campaign_id: str, list_version_id: str) -> None:
        self._connection.execute(
            """
            insert into raw.capture_campaign_list_versions (campaign_id, list_version_id)
            values (%s, %s) on conflict do nothing
            """,
            (campaign_id, list_version_id),
        )

    def put_obligation(self, campaign_id: str, obligation: CaptureListObligation) -> bool:
        inserted = self._connection.execute(
            """
            insert into raw.capture_obligations (
                obligation_id, campaign_id, run_id, list_version_id, subject_kind,
                subject_id, capture_requirement_id, partition_key, content_sha256
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (obligation_id) do nothing returning obligation_id
            """,
            (
                obligation.obligation_id,
                campaign_id,
                obligation.run_id,
                obligation.list_version_id,
                obligation.subject.kind.value,
                obligation.subject.id,
                obligation.capture_requirement_id,
                obligation.partition,
                obligation.content_sha256,
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_obligations", "obligation_id", obligation.obligation_id, obligation.content_sha256
            )
        )

    def put_source_request(self, request: SourceRequest) -> bool:
        payload = self._payload(request)
        inserted = self._connection.execute(
            """
            insert into raw.capture_source_requests (
                source_request_id, content_sha256, source_registry_entry_id, source_policy_id,
                request_fingerprint_version, canonical_request_sha256, subject_refs,
                capture_requirement_ids, partition_key, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (source_request_id) do nothing returning source_request_id
            """,
            (
                request.source_request_id,
                request.content_sha256,
                request.source_registry_entry_id,
                request.source_policy_id,
                request.request_fingerprint_version,
                request.canonical_request_sha256,
                Jsonb(payload["subject_refs"]),
                list(request.capture_requirement_ids),
                request.partition,
                Jsonb(payload),
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_source_requests", "source_request_id", request.source_request_id, request.content_sha256
            )
        )

    def put_work_item(self, item: CaptureWorkItem, retry: RetryPolicy) -> bool:
        retryable = [value.value for value in retry.retryable_outcomes]
        terminal = [value.value for value in retry.terminal_outcomes]
        envelope_sha256 = canonical_sha256(
            {
                "work_item_id": item.work_item_id,
                "content_sha256": item.content_sha256,
                "maximum_attempts": retry.max_attempts,
                "retryable_outcomes": retryable,
                "terminal_outcomes": terminal,
            }
        )
        inserted = self._connection.execute(
            """
            insert into raw.capture_work_items (
                work_item_id, campaign_id, source_request_id, schedule_policy_id,
                maximum_attempts, retryable_outcomes, terminal_outcomes,
                content_sha256, storage_envelope_sha256
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (work_item_id) do nothing returning work_item_id
            """,
            (
                item.work_item_id,
                item.campaign_id,
                item.source_request_id,
                item.schedule_policy_id,
                retry.max_attempts,
                retryable,
                terminal,
                item.content_sha256,
                envelope_sha256,
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing("raw.capture_work_items", "work_item_id", item.work_item_id, item.content_sha256)
        )

    def put_binding(self, binding: CaptureObligationWorkBinding) -> bool:
        inserted = self._connection.execute(
            """
            insert into raw.capture_obligation_work_bindings (
                binding_id, obligation_id, work_item_id, content_sha256
            ) values (%s, %s, %s, %s)
            on conflict (binding_id) do nothing returning binding_id
            """,
            (binding.binding_id, binding.obligation_id, binding.work_item_id, binding.content_sha256),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_obligation_work_bindings", "binding_id", binding.binding_id, binding.content_sha256
            )
        )

    def put_attempt(self, attempt: FetchAttempt) -> bool:
        if self._existing_matches("raw.capture_attempts", "attempt_id", attempt.attempt_id, attempt.content_sha256):
            return False
        payload = self._payload(attempt)
        inserted = self._connection.execute(
            """
            insert into raw.capture_attempts (
                attempt_id, work_item_id, attempt_number, started_at,
                started_at_canonical, content_sha256
            ) values (%s, %s, %s, %s, %s, %s)
            on conflict (attempt_id) do nothing returning attempt_id
            """,
            (
                attempt.attempt_id,
                attempt.work_item_id,
                attempt.attempt_number,
                attempt.started_at,
                payload["started_at"],
                attempt.content_sha256,
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing("raw.capture_attempts", "attempt_id", attempt.attempt_id, attempt.content_sha256)
        )

    def put_attempt_result(self, result: FetchAttemptResult) -> bool:
        payload = self._payload(result)
        inserted = self._connection.execute(
            """
            insert into raw.capture_attempt_results (
                attempt_result_id, attempt_id, completed_at, completed_at_canonical,
                outcome, status_code, reason_codes, source_vintage_id,
                reused_source_vintage_id, content_sha256
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (attempt_result_id) do nothing returning attempt_result_id
            """,
            (
                result.attempt_result_id,
                result.attempt_id,
                result.completed_at,
                payload["completed_at"],
                result.outcome.value,
                result.status_code,
                list(result.reason_codes),
                result.source_vintage_id,
                result.reused_source_vintage_id,
                result.content_sha256,
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_attempt_results", "attempt_result_id", result.attempt_result_id, result.content_sha256
            )
        )

    def put_checkpoint(self, checkpoint: CaptureCheckpoint) -> bool:
        if self._existing_matches(
            "raw.capture_checkpoints", "checkpoint_id", checkpoint.checkpoint_id, checkpoint.content_sha256
        ):
            return False
        payload = self._payload(checkpoint)
        inserted = self._connection.execute(
            """
            insert into raw.capture_checkpoints (
                checkpoint_id, run_id, sequence, phase, completed_obligation_ids,
                recorded_at, recorded_at_canonical, content_sha256
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (checkpoint_id) do nothing returning checkpoint_id
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.run_id,
                checkpoint.sequence,
                checkpoint.phase.value,
                list(checkpoint.completed_obligation_ids),
                checkpoint.recorded_at,
                payload["recorded_at"],
                checkpoint.content_sha256,
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_checkpoints", "checkpoint_id", checkpoint.checkpoint_id, checkpoint.content_sha256
            )
        )

    def put_source_vintage(self, vintage: SourceVintage, *, raw_fetch_id: int) -> bool:
        payload = self._payload(vintage)
        inserted = self._connection.execute(
            """
            insert into raw.capture_source_vintages (
                source_vintage_id, content_sha256, source_request_id, source_record_id,
                source_published_at, raw_object_id, raw_fetch_id, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (source_vintage_id) do nothing returning source_vintage_id
            """,
            (
                vintage.source_vintage_id,
                vintage.content_sha256,
                vintage.source_request_id,
                vintage.source_record_id,
                vintage.source_published_at,
                vintage.raw_object_id,
                raw_fetch_id,
                Jsonb(payload),
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_source_vintages", "source_vintage_id", vintage.source_vintage_id, vintage.content_sha256
            )
        )

    def put_observation(
        self,
        capture_obligation_id: str,
        observation: NormalizedObservation,
        *,
        confidence: Decimal,
        freshness_state: str = "unknown",
    ) -> bool:
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("observation confidence must be between zero and one")
        payload = self._payload(observation)
        inserted = self._connection.execute(
            """
            insert into staging.capture_normalized_observations (
                observation_id, content_sha256, capture_obligation_id, source_vintage_id,
                semantic_type, semantic_version, subject_kind, subject_id, valid_from,
                valid_to, knowable_at, parser_version, mapping_version,
                normalized_payload_sha256, is_restatement, supersedes_observation_id,
                confidence, freshness_state, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (observation_id) do nothing returning observation_id
            """,
            (
                observation.observation_id,
                observation.content_sha256,
                capture_obligation_id,
                observation.source_vintage_id,
                observation.semantic_type,
                observation.semantic_version,
                observation.subject.kind.value,
                observation.subject.id,
                observation.valid_from,
                observation.valid_to,
                observation.knowable_at,
                observation.parser_version,
                observation.mapping_version,
                observation.normalized_payload_sha256,
                observation.is_restatement,
                observation.supersedes_observation_id,
                confidence,
                freshness_state,
                Jsonb(payload),
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "staging.capture_normalized_observations",
                "observation_id",
                observation.observation_id,
                observation.content_sha256,
            )
        )

    def put_obligation_result(self, capture_obligation_id: str, result: ListObligationResult) -> bool:
        expected = self._connection.execute(
            """
            select 'list-obligation:' || raw.canonical_sha256(jsonb_build_object(
                'kind', 'list-obligation',
                'identity', jsonb_build_object(
                    'run_id', obligation.run_id,
                    'universe_ref', jsonb_build_object(
                        'universe_id', version.universe_id,
                        'universe_version', version.universe_version,
                        'content_sha256', version.universe_sha256
                    ),
                    'subject', jsonb_build_object(
                        'kind', obligation.subject_kind,
                        'id', obligation.subject_id
                    ),
                    'capture_requirement_id', obligation.capture_requirement_id,
                    'partition', obligation.partition_key
                )
            ))
            from raw.capture_obligations obligation
            join raw.capture_list_versions version using (list_version_id)
            where obligation.obligation_id = %s
            """,
            (capture_obligation_id,),
        ).fetchone()
        if expected is None:
            raise LookupError(f"capture obligation not found: {capture_obligation_id}")
        if expected[0] != result.obligation_id:
            raise ValueError("terminal result logical obligation does not match capture obligation")
        payload = self._payload(result)
        inserted = self._connection.execute(
            """
            insert into raw.capture_obligation_results (
                result_id, content_sha256, capture_obligation_id, logical_obligation_id,
                terminal_state, completed_at, final_attempt_id, reason_codes, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (result_id) do nothing returning result_id
            """,
            (
                result.result_id,
                result.content_sha256,
                capture_obligation_id,
                result.obligation_id,
                result.terminal_state.value,
                result.completed_at,
                result.final_attempt_id,
                list(result.reason_codes),
                Jsonb(payload),
            ),
        ).fetchone()
        return (
            True
            if inserted
            else self._check_existing(
                "raw.capture_obligation_results", "result_id", result.result_id, result.content_sha256
            )
        )

    def status(self, run_id: str) -> ToptCaptureStatus:
        row = self._connection.execute(
            """
            select run_id, campaign_id, environment, cutoff, universe_id,
                   universe_version, universe_sha256, obligation_count, terminal_count,
                   success_count, unchanged_count, unavailable_count, skipped_count,
                   failed_count, complete
            from mart.topt_capture_status where run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"capture run not found: {run_id}")
        return ToptCaptureStatus(*row)

    def meta_info(self, run_id: str, *, limit: int = 100, offset: int = 0) -> tuple[ToptCaptureMetaInfo, ...]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("meta_info pagination is outside the bounded range")
        rows = self._connection.execute(
            """
            select run_id, obligation_id, logical_obligation_id, subject_kind, subject_id,
                   capture_requirement_id, partition_key, work_item_id, source_request_id,
                   source_registry_entry_id, source_policy_id, request_fingerprint_version,
                   terminal_state, reason_codes, completed_at, attempt_count,
                   final_status_code, observation_id, semantic_version, parser_version,
                   mapping_version, confidence, freshness_state, knowable_at, recorded_at
            from mart.topt_capture_meta_info
            where run_id = %s order by obligation_id limit %s offset %s
            """,
            (run_id, limit, offset),
        ).fetchall()
        return tuple(
            ToptCaptureMetaInfo(
                run_id=row[0],
                obligation_id=row[1],
                logical_obligation_id=row[2],
                subject_kind=row[3],
                subject_id=row[4],
                capture_requirement_id=row[5],
                partition_key=row[6],
                work_item_id=row[7],
                source_request_id=row[8],
                source_registry_entry_id=row[9],
                source_policy_id=row[10],
                request_fingerprint_version=row[11],
                terminal_state=row[12],
                reason_codes=tuple(row[13]) if row[13] is not None else None,
                completed_at=row[14],
                attempt_count=row[15],
                final_status_code=row[16],
                observation_id=row[17],
                semantic_version=row[18],
                parser_version=row[19],
                mapping_version=row[20],
                confidence=row[21],
                freshness_state=row[22],
                knowable_at=row[23],
                recorded_at=row[24],
            )
            for row in rows
        )
