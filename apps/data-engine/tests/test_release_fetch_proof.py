"""Tests for release_fetch_proof quality check (#876 W7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import dagster as dg
import pytest
from data_engine.quality import release_fetch_proof
from data_engine.quality.release_fetch_proof import (
    BOOT_CANARY_TAG,
    OK,
    PENDING,
    RED,
    SCHEDULE_TAG,
    _as_utc,
    capture_run_of,
    evaluate,
    expected_origins,
    forced,
    origin_of,
)


def test_as_utc_normalizes_timestamps() -> None:
    now_aware = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    assert _as_utc(now_aware) == now_aware

    now_naive = datetime(2026, 9, 21, 12, 0, 0)
    assert _as_utc(now_naive) == now_aware

    epoch = now_aware.timestamp()
    assert _as_utc(epoch) == now_aware

    with pytest.raises(TypeError):
        _as_utc("2026-09-21")  # type: ignore[arg-type]


def test_origin_of_maps_registered_semantics() -> None:
    origin = origin_of("market-price", "production-topt-live-parser:v1")
    assert origin == "yahoo-chart:v1"

    unknown = origin_of("nonexistent_semantic", "nonexistent_parser")
    assert unknown is None


def test_expected_origins_raises_on_unregistered_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_fetch_proof, "ORIGIN_ENABLED", {})
    with pytest.raises(LookupError, match="declares no switch in ORIGIN_ENABLED"):
        expected_origins()


def test_forced_detects_force_fetch_config() -> None:
    run_normal = MagicMock(spec=dg.DagsterRun)
    run_normal.job_name = "topt_live_pipeline"
    run_normal.run_config = {}
    assert not forced(run_normal)

    run_forced = MagicMock(spec=dg.DagsterRun)
    run_forced.job_name = "topt_live_pipeline"
    run_forced.run_config = {"ops": {"run_topt_live_tick": {"config": {"force_fetch": True}}}}
    assert forced(run_forced)

    run_unknown_job = MagicMock(spec=dg.DagsterRun)
    run_unknown_job.job_name = "unknown_job"
    assert not forced(run_unknown_job)


def test_evaluate_pending_when_digest_is_local_or_ci() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    proof = evaluate(connection, instance, digest="local-dev")
    assert proof.state == PENDING
    assert proof.ok is None
    assert "no data-engine image digest" in proof.summary


def test_evaluate_pending_when_no_boot_canary() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    instance.get_run_records.return_value = []
    digest = "sha256:" + "a" * 64
    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == PENDING
    assert "no boot canary run" in proof.summary


def test_evaluate_pending_when_no_fetching_run_since_deployment() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now
    instance.get_run_records.side_effect = lambda filters, **kwargs: [canary_record] if filters.tags else []

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == PENDING
    assert "no scheduled or forced live-pipeline run since" in proof.summary


def test_evaluate_red_when_first_fetching_run_failed() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    failed_run = MagicMock()
    failed_run.job_name = "topt_live_pipeline"
    failed_run.run_id = "run-fail-12345"
    failed_run.tags = {SCHEDULE_TAG: "1"}
    failed_run.run_config = {}
    failed_run.status = dg.DagsterRunStatus.FAILURE

    failed_record = MagicMock()
    failed_record.create_timestamp = now + timedelta(minutes=1)
    failed_record.dagster_run = failed_run

    def mock_get_run_records(filters: dg.RunsFilter, **kwargs: Any) -> list:
        if filters.tags and BOOT_CANARY_TAG in filters.tags:
            return [canary_record]
        return [failed_record]

    instance.get_run_records.side_effect = mock_get_run_records

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert proof.ok is False
    assert "ended failure" in proof.summary


def test_evaluate_pending_when_first_fetching_run_is_in_progress() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    running_run = MagicMock()
    running_run.job_name = "topt_live_pipeline"
    running_run.run_id = "run-prog-12345"
    running_run.tags = {SCHEDULE_TAG: "1"}
    running_run.run_config = {}
    running_run.status = dg.DagsterRunStatus.STARTED

    running_record = MagicMock()
    running_record.create_timestamp = now + timedelta(minutes=1)
    running_record.dagster_run = running_run

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else [running_record]
    )

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == PENDING
    assert "is started" in proof.summary


def test_evaluate_red_when_capture_stamped_by_different_digest() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    other_digest = "sha256:" + "b" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    succ_run = MagicMock()
    succ_run.job_name = "topt_live_pipeline"
    succ_run.run_id = "run-succ-12345"
    succ_run.tags = {SCHEDULE_TAG: "1"}
    succ_run.run_config = {}
    succ_run.status = dg.DagsterRunStatus.SUCCESS

    succ_record = MagicMock()
    succ_record.create_timestamp = now + timedelta(minutes=1)
    succ_record.dagster_run = succ_run

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else [succ_record]
    )

    step_output = MagicMock()
    step_output.event_log_entry.dagster_event.step_output_data.metadata = {
        release_fetch_proof.CAPTURE_RUN_METADATA: "capture-run-xyz"
    }
    step_record = MagicMock()
    step_record.records = [step_output]
    instance.get_records_for_run.return_value = step_record

    # DB returns different image_digest
    connection.execute.return_value.fetchone.return_value = (other_digest,)

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert "its capture is stamped by" in proof.summary


def test_evaluate_ok_when_all_origins_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    succ_run = MagicMock()
    succ_run.job_name = "topt_live_pipeline"
    succ_run.run_id = "run-succ-12345"
    succ_run.tags = {SCHEDULE_TAG: "1"}
    succ_run.run_config = {}
    succ_run.status = dg.DagsterRunStatus.SUCCESS

    succ_record = MagicMock()
    succ_record.create_timestamp = now + timedelta(minutes=1)
    succ_record.dagster_run = succ_run

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else [succ_record]
    )

    step_output = MagicMock()
    step_output.event_log_entry.dagster_event.step_output_data.metadata = {
        release_fetch_proof.CAPTURE_RUN_METADATA: "capture-run-xyz"
    }
    step_record = MagicMock()
    step_record.records = [step_output]
    instance.get_records_for_run.return_value = step_record

    # DB returns same image_digest
    connection.execute.return_value.fetchone.return_value = (digest,)

    # Mock origins
    expected = frozenset({"sec:v1", "yahoo-chart:v1"})
    monkeypatch.setattr(release_fetch_proof, "expected_origins", lambda: expected)
    monkeypatch.setattr(
        release_fetch_proof, "fetched_by_origin", lambda conn, cap_id: {"sec:v1": 10, "yahoo-chart:v1": 20}
    )

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == OK
    assert proof.ok is True
    assert "fetched" in proof.summary


def test_evaluate_red_when_missing_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    succ_run = MagicMock()
    succ_run.job_name = "topt_live_pipeline"
    succ_run.run_id = "run-succ-12345"
    succ_run.tags = {SCHEDULE_TAG: "1"}
    succ_run.run_config = {}
    succ_run.status = dg.DagsterRunStatus.SUCCESS

    succ_record = MagicMock()
    succ_record.create_timestamp = now + timedelta(minutes=1)
    succ_record.dagster_run = succ_run

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else [succ_record]
    )

    step_output = MagicMock()
    step_output.event_log_entry.dagster_event.step_output_data.metadata = {
        release_fetch_proof.CAPTURE_RUN_METADATA: "capture-run-xyz"
    }
    step_record = MagicMock()
    step_record.records = [step_output]
    instance.get_records_for_run.return_value = step_record

    connection.execute.return_value.fetchone.return_value = (digest,)

    expected = frozenset({"sec:v1", "yahoo-chart:v1"})
    monkeypatch.setattr(release_fetch_proof, "expected_origins", lambda: expected)
    monkeypatch.setattr(release_fetch_proof, "fetched_by_origin", lambda conn, cap_id: {"sec:v1": 10})

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert proof.ok is False
    assert "fetched nothing from yahoo-chart:v1" in proof.summary


def test_evaluate_red_when_expected_origins_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    succ_run = MagicMock()
    succ_run.job_name = "topt_live_pipeline"
    succ_run.run_id = "run-succ-12345"
    succ_run.tags = {SCHEDULE_TAG: "1"}
    succ_run.run_config = {}
    succ_run.status = dg.DagsterRunStatus.SUCCESS

    succ_record = MagicMock()
    succ_record.create_timestamp = now + timedelta(minutes=1)
    succ_record.dagster_run = succ_run

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else [succ_record]
    )

    step_output = MagicMock()
    step_output.event_log_entry.dagster_event.step_output_data.metadata = {
        release_fetch_proof.CAPTURE_RUN_METADATA: "capture-run-xyz"
    }
    step_record = MagicMock()
    step_record.records = [step_output]
    instance.get_records_for_run.return_value = step_record

    connection.execute.return_value.fetchone.return_value = (digest,)

    monkeypatch.setattr(release_fetch_proof, "expected_origins", lambda: frozenset())
    monkeypatch.setattr(release_fetch_proof, "fetched_by_origin", lambda conn, cap_id: {})

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert proof.ok is False
    assert "no expected origins configured" in proof.summary


def test_evaluate_red_when_capture_run_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    succ_run = MagicMock()
    succ_run.job_name = "topt_live_pipeline"
    succ_run.run_id = "run-succ-12345"
    succ_run.tags = {SCHEDULE_TAG: "1"}
    succ_run.run_config = {}
    succ_run.status = dg.DagsterRunStatus.SUCCESS

    succ_record = MagicMock()
    succ_record.create_timestamp = now + timedelta(minutes=1)
    succ_record.dagster_run = succ_run

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else [succ_record]
    )

    monkeypatch.setattr(release_fetch_proof, "capture_run_of", lambda inst, run_id: None)

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert proof.ok is False
    assert "names no capture run" in proof.summary


def test_capture_run_of_handles_none_metadata() -> None:
    instance = MagicMock(spec=dg.DagsterInstance)
    step_output = MagicMock()
    step_output.event_log_entry.dagster_event.step_output_data.metadata = None
    step_record = MagicMock()
    step_record.records = [step_output]
    instance.get_records_for_run.return_value = step_record

    result = capture_run_of(instance, "run-123")
    assert result is None
