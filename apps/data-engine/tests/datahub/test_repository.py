from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import PostgresCaptureControlRepository, expand_obligations
from data_engine.datahub.control_plane import AttemptLedger
from data_engine.datahub.medium_replay import _capture_run, _source_request, frozen_topt_list_version
from truealpha_contracts import canonical_sha256
from truealpha_contracts.capture_control import (
    CaptureCheckpoint,
    CaptureObligationWorkBinding,
    CheckpointPhase,
)
from truealpha_contracts.datahub import (
    CaptureWorkItem,
    FetchAttemptOutcome,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    SourceVintage,
)

CORPUS = Path(__file__).parents[1] / "fixtures" / "capture_control" / "corpus.v1.json"
STARTED_AT = datetime(2026, 4, 1, 1, tzinfo=UTC)


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
