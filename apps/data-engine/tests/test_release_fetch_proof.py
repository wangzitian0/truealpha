"""Tests for release_fetch_proof quality check (#876 W7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
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
    fetched_by_origin,
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

    other_tz = timezone(timedelta(hours=8))
    now_other = datetime(2026, 9, 21, 20, 0, 0, tzinfo=other_tz)
    assert _as_utc(now_other) == now_aware

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

    proof = evaluate(connection, instance, digest=digest, now=now)
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

    proof = evaluate(connection, instance, digest=digest, now=now + timedelta(minutes=2))
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


def test_evaluate_red_when_pending_exceeds_max_window() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    boot_time = datetime.now(UTC) - timedelta(hours=27)

    canary_record = MagicMock()
    canary_record.create_timestamp = boot_time

    instance.get_run_records.side_effect = lambda filters, **kwargs: (
        [canary_record] if filters.tags and BOOT_CANARY_TAG in filters.tags else []
    )

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert proof.ok is False
    assert "proof timed out" in proof.summary
    assert "limit" in proof.summary


def test_evaluate_recovers_when_later_forced_run_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
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
    failed_record.create_timestamp = now + timedelta(minutes=10)
    failed_record.dagster_run = failed_run

    succ_run = MagicMock()
    succ_run.job_name = "topt_live_pipeline"
    succ_run.run_id = "run-succ-67890"
    succ_run.tags = {}
    succ_run.run_config = {"ops": {"run_topt_live_tick": {"config": {"force_fetch": True}}}}
    succ_run.status = dg.DagsterRunStatus.SUCCESS

    succ_record = MagicMock()
    succ_record.create_timestamp = now + timedelta(minutes=30)
    succ_record.dagster_run = succ_run

    def mock_get_run_records(filters: dg.RunsFilter, **kwargs: Any) -> list:
        if filters.tags and BOOT_CANARY_TAG in filters.tags:
            return [canary_record]
        if filters.job_name == "topt_live_pipeline":
            return [failed_record, succ_record]
        return []

    instance.get_run_records.side_effect = mock_get_run_records

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
    monkeypatch.setattr(
        release_fetch_proof, "fetched_by_origin", lambda conn, cap_id: {"sec:v1": 10, "yahoo-chart:v1": 20}
    )

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == OK
    assert proof.ok is True
    assert "fetched" in proof.summary


def test_fetched_by_origin_aggregates_counts_and_filters_none() -> None:
    class FakeCursor:
        def __init__(self, rows: list[tuple[str, str, int]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[str, str, int]]:
            return self._rows

    class FakeConnection:
        def __init__(self, rows: list[tuple[str, str, int]]) -> None:
            self._rows = rows

        def execute(self, query: str, params: tuple[Any, ...]) -> FakeCursor:
            return FakeCursor(self._rows)

    rows = [
        ("market-price", "production-topt-live-parser:v1", 10),
        ("market-price", "twelve-data-parser:v1", 20),
        ("unknown-semantic", "random-parser", 5),
    ]
    fake_conn = FakeConnection(rows)
    result = fetched_by_origin(fake_conn, "cap-1")  # type: ignore[arg-type]
    assert result == {"yahoo-chart:v1": 10, "twelve-data:v1": 20}


def test_evaluate_recovers_when_earlier_run_had_missing_origins_but_later_run_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    canary_record = MagicMock()
    canary_record.create_timestamp = now

    run1 = MagicMock()
    run1.job_name = "topt_live_pipeline"
    run1.run_id = "run-succ-11111"
    run1.tags = {SCHEDULE_TAG: "1"}
    run1.run_config = {}
    run1.status = dg.DagsterRunStatus.SUCCESS

    record1 = MagicMock()
    record1.create_timestamp = now + timedelta(minutes=10)
    record1.dagster_run = run1

    run2 = MagicMock()
    run2.job_name = "topt_live_pipeline"
    run2.run_id = "run-succ-22222"
    run2.tags = {SCHEDULE_TAG: "1"}
    run2.run_config = {}
    run2.status = dg.DagsterRunStatus.SUCCESS

    record2 = MagicMock()
    record2.create_timestamp = now + timedelta(minutes=30)
    record2.dagster_run = run2

    def mock_get_run_records(filters: dg.RunsFilter, **kwargs: Any) -> list:
        if filters.tags and BOOT_CANARY_TAG in filters.tags:
            return [canary_record]
        if filters.job_name == "topt_live_pipeline":
            return [record1, record2]
        return []

    instance.get_run_records.side_effect = mock_get_run_records

    monkeypatch.setattr(
        release_fetch_proof,
        "capture_run_of",
        lambda inst, run_id: "cap-1" if run_id == "run-succ-11111" else "cap-2",
    )

    connection.execute.return_value.fetchone.return_value = (digest,)

    expected = frozenset({"sec:v1", "yahoo-chart:v1"})
    monkeypatch.setattr(release_fetch_proof, "expected_origins", lambda: expected)

    def mock_fetched_by_origin(conn: Any, cap_id: str) -> dict[str, int]:
        if cap_id == "cap-1":
            return {"sec:v1": 10}
        return {"sec:v1": 10, "yahoo-chart:v1": 20}

    monkeypatch.setattr(release_fetch_proof, "fetched_by_origin", mock_fetched_by_origin)

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == OK
    assert proof.ok is True
    assert "fetched" in proof.summary


def test_evaluate_red_when_in_progress_run_exceeds_max_window() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    boot_time = datetime.now(UTC) - timedelta(hours=27)

    canary_record = MagicMock()
    canary_record.create_timestamp = boot_time

    in_progress_run = MagicMock()
    in_progress_run.job_name = "topt_live_pipeline"
    in_progress_run.run_id = "run-prog-12345"
    in_progress_run.tags = {SCHEDULE_TAG: "1"}
    in_progress_run.run_config = {}
    in_progress_run.status = dg.DagsterRunStatus.STARTED

    in_progress_record = MagicMock()
    in_progress_record.create_timestamp = boot_time + timedelta(minutes=10)
    in_progress_record.dagster_run = in_progress_run

    def mock_get_run_records(filters: dg.RunsFilter, **kwargs: Any) -> list:
        if filters.tags and BOOT_CANARY_TAG in filters.tags:
            return [canary_record]
        if filters.job_name == "topt_live_pipeline":
            return [in_progress_record]
        return []

    instance.get_run_records.side_effect = mock_get_run_records

    proof = evaluate(connection, instance, digest=digest)
    assert proof.state == RED
    assert proof.ok is False
    assert "in-progress run hung" in proof.summary
    assert "proof timed out" in proof.summary
    assert "limit" in proof.summary


def test_evaluate_pending_when_recent_in_progress_run_on_old_deployment() -> None:
    connection = MagicMock()
    instance = MagicMock(spec=dg.DagsterInstance)
    digest = "sha256:" + "a" * 64
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    boot_time = now - timedelta(hours=27)

    canary_record = MagicMock()
    canary_record.create_timestamp = boot_time

    in_progress_run = MagicMock()
    in_progress_run.job_name = "topt_live_pipeline"
    in_progress_run.run_id = "run-prog-recent"
    in_progress_run.tags = {SCHEDULE_TAG: "1"}
    in_progress_run.run_config = {}
    in_progress_run.status = dg.DagsterRunStatus.STARTED

    in_progress_record = MagicMock()
    in_progress_record.create_timestamp = now - timedelta(minutes=10)
    in_progress_record.dagster_run = in_progress_run

    def mock_get_run_records(filters: dg.RunsFilter, **kwargs: Any) -> list:
        if filters.tags and BOOT_CANARY_TAG in filters.tags:
            return [canary_record]
        if filters.job_name == "topt_live_pipeline":
            return [in_progress_record]
        return []

    instance.get_run_records.side_effect = mock_get_run_records

    proof = evaluate(connection, instance, digest=digest, now=now)
    assert proof.state == PENDING
    assert "is started" in proof.summary
