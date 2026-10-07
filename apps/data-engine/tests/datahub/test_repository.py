from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import PostgresCaptureControlRepository, expand_obligations
from data_engine.datahub.control_plane import AttemptLedger, frozen_topt_universe, replay_retry_policy
from data_engine.datahub.production_topt.universe_corpus import frozen_topt_list_version
from truealpha_contracts.capture_control import (
    CaptureCheckpoint,
    CaptureObligationWorkBinding,
    CheckpointPhase,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    FetchAttemptOutcome,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    SourceRequest,
    SourceVintage,
)
from truealpha_contracts.universe import SubjectRef

CORPUS = Path(__file__).parents[1] / "fixtures" / "capture_control" / "corpus.v1.json"
STARTED_AT = datetime(2026, 4, 1, 1, tzinfo=UTC)


# The two helpers below lived in `medium_replay` until #1061. The values are unchanged,
# so every identity this file derives from them is unchanged.
def _capture_run(
    corpus: Mapping[str, Any], *, cutoff: datetime, sequence: int
) -> tuple[CaptureSchedulePolicy, CaptureCampaign, CaptureRun]:
    schedule_policy = CaptureSchedulePolicy(
        policy_version="d5-medium-replay:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="fixture-daily:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:d5-medium-v1",
        environment=CaptureEnvironment.LOCAL_DEV,
        cutoff=cutoff,
        universe_refs=(frozen_topt_universe(corpus),),
    )
    scope_id = f"capture-scope:{canonical_sha256({'corpus_id': corpus['corpus_id'], 'rung': 'E3'})}"
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=sequence,
        schedule_policy_id=schedule_policy.schedule_policy_id,
        capture_scope_id=scope_id,
    )
    return schedule_policy, campaign, run


def _source_request(
    *,
    member: SubjectRef,
    semantic_types: tuple[str, ...],
    partition: str,
) -> SourceRequest:
    requirement_ids = tuple(f"{semantic_type}:v1" for semantic_type in semantic_types)
    request_coordinate = {
        "member": member.model_dump(mode="json"),
        "requirements": requirement_ids,
        "partition": partition,
    }
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'd5-medium-fixture:v1', 'semantic_types': semantic_types})}",
        source_policy_id="source-policy:d5-medium-fixture-v1",
        request_fingerprint_version="d5-medium-request:v1",
        canonical_request_sha256=canonical_sha256(request_coordinate),
        subject_refs=(member,),
        capture_requirement_ids=requirement_ids,
        partition=partition,
    )


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _schedule_policy() -> CaptureSchedulePolicy:
    return CaptureSchedulePolicy(
        policy_version="d5-medium-replay:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="fixture-daily:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )


def _capture_run(
    corpus: dict[str, object], *, cutoff: datetime, sequence: int
) -> tuple[CaptureSchedulePolicy, CaptureCampaign, CaptureRun]:
    universe = frozen_topt_universe(corpus)
    schedule_policy = _schedule_policy()
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:d5-medium-v1",
        environment=CaptureEnvironment.LOCAL_DEV,
        cutoff=cutoff,
        universe_refs=(universe,),
    )
    scope_id = f"capture-scope:{canonical_sha256({'corpus_id': corpus['corpus_id'], 'rung': 'E3'})}"
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=sequence,
        schedule_policy_id=schedule_policy.schedule_policy_id,
        capture_scope_id=scope_id,
    )
    return schedule_policy, campaign, run


def _source_request(
    *,
    member: SubjectRef,
    semantic_types: tuple[str, ...],
    partition: str,
) -> SourceRequest:
    requirement_ids = tuple(f"{semantic_type}:v1" for semantic_type in semantic_types)
    request_coordinate = {
        "member": member.model_dump(mode="json"),
        "requirements": requirement_ids,
        "partition": partition,
    }
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'd5-medium-fixture:v1', 'semantic_types': semantic_types})}",
        source_policy_id="source-policy:d5-medium-fixture-v1",
        request_fingerprint_version="d5-medium-request:v1",
        canonical_request_sha256=canonical_sha256(request_coordinate),
        subject_refs=(member,),
        capture_requirement_ids=requirement_ids,
        partition=partition,
    )


def test_repository_persists_and_reads_terminal_capture_chain(connection) -> None:
    corpus = json.loads(CORPUS.read_text())
    list_version = frozen_topt_list_version(corpus)
    policy, campaign, run = _capture_run(corpus, cutoff=STARTED_AT, sequence=1)
    obligation = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=("market-price",),
        partition="2026-03-31",
    )[0]
    request = _source_request(
        member=obligation.subject,
        semantic_types=("market-price",),
        partition=obligation.partition,
    )
    work_item = CaptureWorkItem(
        campaign_id=campaign.campaign_id,
        source_request_id=request.source_request_id,
        schedule_policy_id=policy.schedule_policy_id,
    )
    binding = CaptureObligationWorkBinding(
        obligation_id=obligation.obligation_id,
        work_item_id=work_item.work_item_id,
    )
    raw_sha256 = canonical_sha256({"close": "175.20"})
    vintage = SourceVintage(
        source_request_id=request.source_request_id,
        source_record_id="yahoo-chart:NVDA:2026-03-31",
        source_published_at=STARTED_AT,
        raw_object_id=f"raw-object:{raw_sha256}",
    )
    ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=policy.retry)
    attempt = ledger.start(started_at=STARTED_AT)
    attempt_result = ledger.finish(
        attempt=attempt,
        completed_at=STARTED_AT + timedelta(seconds=1),
        outcome=FetchAttemptOutcome.SUCCESS,
        status_code=200,
        source_vintage_id=vintage.source_vintage_id,
    )
    observation = NormalizedObservation(
        semantic_type="market-price",
        semantic_version="market-price:v1",
        subject=obligation.subject,
        valid_from=STARTED_AT - timedelta(days=1),
        knowable_at=STARTED_AT,
        source_vintage_id=vintage.source_vintage_id,
        parser_version="yahoo-chart-parser:v1",
        mapping_version="topt-listing-map:v1",
        normalized_payload_sha256=canonical_sha256({"close": "175.20"}),
    )
    obligation_result = ListObligationResult(
        obligation_id=obligation.obligation.obligation_id,
        terminal_state=ObligationTerminalState.SUCCESS,
        completed_at=STARTED_AT + timedelta(seconds=2),
        final_attempt_id=attempt.attempt_id,
        reason_codes=("success",),
    )
    checkpoint = CaptureCheckpoint(
        run_id=run.run_id,
        sequence=1,
        phase=CheckpointPhase.MANIFEST_PERSISTED,
        completed_obligation_ids=(obligation.obligation_id,),
        recorded_at=STARTED_AT + timedelta(seconds=3),
    )
    repository = PostgresCaptureControlRepository(connection)

    raw_fetch_id = connection.execute(
        """
        insert into raw.fetches (
            source, source_record_id, payload_sha256, object_uri, content_type,
            byte_length, fetched_at, recorded_at, metadata
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
        returning id
        """,
        (
            "yahoo",
            vintage.source_record_id,
            raw_sha256,
            f"s3://test/{raw_sha256}",
            "application/json",
            18,
            STARTED_AT,
            STARTED_AT,
        ),
    ).fetchone()[0]

    assert repository.put_schedule_policy(policy)
    assert repository.put_campaign(campaign)
    assert repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    assert repository.put_run(run)
    assert repository.put_obligation(campaign.campaign_id, obligation)
    assert repository.put_source_request(request)
    assert repository.put_work_item(work_item, policy.retry)
    assert repository.put_binding(binding)
    assert repository.put_attempt(attempt)
    assert repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
    assert repository.put_attempt_result(attempt_result)
    assert repository.put_observation(
        obligation.obligation_id,
        observation,
        normalized_payload={"close": "175.20"},
        confidence=Decimal("0.95"),
        freshness_state="fresh",
    )
    assert repository.put_obligation_result(obligation.obligation_id, obligation_result)
    assert repository.put_checkpoint(checkpoint)

    status = repository.status(run.run_id)
    assert (status.obligation_count, status.terminal_count, status.success_count) == (1, 1, 1)
    assert status.complete
    assert status.environment == "local_dev"

    meta_info = repository.meta_info(run.run_id)
    assert len(meta_info) == 1
    assert meta_info[0].logical_obligation_id == obligation.obligation.obligation_id
    assert meta_info[0].attempt_count == 1
    assert meta_info[0].final_status_code == 200
    assert meta_info[0].observation_id == observation.observation_id
    assert meta_info[0].reason_codes == ("success",)
    assert meta_info[0].confidence == Decimal("0.95")

    assert repository.put_schedule_policy(policy) is False
    assert repository.put_campaign(campaign) is False
    assert repository.put_list_version(list_version) is False
    assert repository.put_run(run) is False
    assert repository.put_obligation(campaign.campaign_id, obligation) is False
    assert repository.put_source_request(request) is False
    assert repository.put_work_item(work_item, policy.retry) is False
    assert repository.put_binding(binding) is False
    assert repository.put_attempt(attempt) is False
    assert repository.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id) is False
    assert repository.put_attempt_result(attempt_result) is False
    assert (
        repository.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload={"close": "175.20"},
            confidence=Decimal("0.95"),
        )
        is False
    )
    with pytest.raises(ValueError, match="does not match the observation hash"):
        repository.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload={"close": "0"},
            confidence=Decimal("0.95"),
        )
    assert repository.put_obligation_result(obligation.obligation_id, obligation_result) is False
    assert repository.put_checkpoint(checkpoint) is False

    mismatched_result = ListObligationResult(
        obligation_id="list-obligation:" + "0" * 64,
        terminal_state=ObligationTerminalState.SUCCESS,
        completed_at=STARTED_AT + timedelta(seconds=2),
        final_attempt_id=attempt.attempt_id,
        reason_codes=("success",),
    )
    with pytest.raises(ValueError, match="logical obligation does not match"):
        repository.put_obligation_result(obligation.obligation_id, mismatched_result)


def test_repository_reads_are_bounded_and_capture_rows_are_append_only(connection) -> None:
    repository = PostgresCaptureControlRepository(connection)

    with pytest.raises(ValueError, match="bounded range"):
        repository.meta_info("capture-run:" + "0" * 64, limit=501)
    with pytest.raises(ValueError, match="bounded range"):
        repository.meta_info("capture-run:" + "0" * 64, offset=-1)
    with pytest.raises(LookupError, match="capture run not found"):
        repository.status("capture-run:" + "0" * 64)

    corpus = json.loads(CORPUS.read_text())
    policy, _, _ = _capture_run(corpus, cutoff=STARTED_AT, sequence=1)
    repository.put_schedule_policy(policy)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "update raw.capture_schedule_policies set policy_version = %s where schedule_policy_id = %s",
            ("mutated:v1", policy.schedule_policy_id),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "delete from raw.capture_schedule_policies where schedule_policy_id = %s",
            (policy.schedule_policy_id,),
        )
