import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import dagster as dg
import pytest
from data_engine import dagster_defs, db
from data_engine.capture import runs, source_results
from data_engine.capture.topt import build_topt_scope
from data_engine.config import settings
from data_engine.dagster_defs import (
    SCHEDULE_NAME,
    _emit_phase_failure_results,
    defs,
    topt_capture_scope,
    topt_staging_daily_schedule,
)


def test_dagster_definitions_load_with_exact_capture_graph():
    dg.Definitions.validate_loadable(defs)
    repository = defs.get_repository_def()
    assert {key.to_user_string() for key in repository.asset_graph.get_all_asset_keys()} == {
        "topt_capture_scope",
        "topt_identity",
        "topt_sec_financials",
        "topt_sec_filings",
        "topt_yahoo_prices",
        "topt_moomoo_domains",
        "topt_capture_manifest",
    }
    assert [schedule.name for schedule in repository.schedule_defs] == [SCHEDULE_NAME]


def test_real_source_schedule_skips_outside_staging():
    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 7, 12, 6, 30, tzinfo=UTC))
    execution = topt_staging_daily_schedule.evaluate_tick(context)
    assert execution.skip_message == "TOPT real-source schedule only runs with APP_ENV=staging"
    assert not execution.run_requests


def test_scope_asset_persists_exact_release_binding(monkeypatch):
    monkeypatch.setenv("TRUEALPHA_RELEASE_MANIFEST_ID", "release-manifest:" + "a" * 64)
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv("TRUEALPHA_CONFIGURATION_SHA256", "c" * 64)
    try:
        result = dg.materialize([topt_capture_scope])
    except Exception as error:
        pytest.fail(f"scope asset failed against the configured test database: {error}")
    assert result.success
    conn = db.connect()
    try:
        binding = runs.get(conn, result.run_id)
    finally:
        conn.close()
    assert binding is not None
    assert binding.release_manifest_id == "release-manifest:" + "a" * 64
    assert binding.image_digest == "sha256:" + "b" * 64
    assert binding.configuration_sha256 == "c" * 64


def test_phase_failure_evidence_is_idempotent_per_attempt_and_latest_attempt_wins():
    run_id = f"test-phase-failure:{uuid.uuid4().hex}"
    scope = build_topt_scope()
    conn = db.connect()
    try:
        first_ids = _emit_phase_failure_results(
            conn,
            run_id=run_id,
            scope=scope,
            phase_name="sec_financials",
            attempt=0,
            error=RuntimeError("injected SEC outage"),
        )
        retry_ids = _emit_phase_failure_results(
            conn,
            run_id=run_id,
            scope=scope,
            phase_name="sec_financials",
            attempt=1,
            error=RuntimeError("injected SEC outage"),
        )
        repeated_retry_ids = _emit_phase_failure_results(
            conn,
            run_id=run_id,
            scope=scope,
            phase_name="sec_financials",
            attempt=1,
            error=RuntimeError("injected SEC outage"),
        )

        financial_requirements = [
            requirement for requirement in scope.requirements if requirement.domain.value == "financial_facts"
        ]
        assert len(financial_requirements) == 20
        assert len(first_ids) == len(retry_ids) == 20
        assert retry_ids == repeated_retry_ids
        assert set(first_ids).isdisjoint(retry_ids)

        for requirement in financial_requirements:
            latest = source_results.for_cell(conn, run_id, requirement)
            assert len(latest) == 1
            result = latest[0][1]
            assert result.attempt == 1
            assert result.outcome is source_results.SourceResultOutcome.FAILED
            assert result.confidence == Decimal("0")
            assert result.detail == "RuntimeError: capture phase failed"
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize(
    ("retry_number", "raises"),
    [(0, True), (dagster_defs.MAX_RETRIES, False)],
)
def test_phase_error_retries_before_exhaustion_then_returns_failure_evidence(
    monkeypatch,
    retry_number,
    raises,
):
    class FakeConnection:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    connection = FakeConnection()
    logged_messages = []
    context = SimpleNamespace(
        run=SimpleNamespace(run_id="run:test"),
        retry_number=retry_number,
        log=SimpleNamespace(error=logged_messages.append),
    )
    emitted_attempts = []

    monkeypatch.setattr(dagster_defs.db, "connect", lambda: connection)
    monkeypatch.setattr(dagster_defs, "_bound_scope", lambda *args, **kwargs: (object(), object()))
    monkeypatch.setattr(
        dagster_defs,
        "_emit_phase_failure_results",
        lambda *args, **kwargs: emitted_attempts.append(kwargs["attempt"]) or (11, 12),
    )
    monkeypatch.setattr(dagster_defs, "_phase_output", lambda *args, **kwargs: kwargs)

    def failing_phase(*args):
        raise RuntimeError("injected outage")

    if raises:
        with pytest.raises(RuntimeError, match="injected outage"):
            dagster_defs._run_phase(
                context,
                {},
                phase_name="sec_financials",
                phase=failing_phase,
            )
    else:
        output = dagster_defs._run_phase(
            context,
            {},
            phase_name="sec_financials",
            phase=failing_phase,
        )
        assert output["result_ids"] == (11, 12)
        assert output["phase_name"] == "sec_financials"

    assert emitted_attempts == [retry_number]
    assert connection.commits == 1
    assert connection.rollbacks == (2 if raises else 1)
    assert connection.closed
    assert logged_messages == (
        []
        if raises
        else [
            "sec_financials exhausted 3 attempts with RuntimeError; persisted failure evidence will drive the manifest"
        ]
    )
    assert all("injected outage" not in message for message in logged_messages)


def test_instance_config_keeps_all_runtime_metadata_in_dagster_schema(monkeypatch, tmp_path):
    separator = "&" if "?" in settings.database_url else "?"
    monkeypatch.setenv(
        "DAGSTER_POSTGRES_URL",
        f"{settings.database_url}{separator}options=-csearch_path%3Ddagster",
    )
    monkeypatch.setenv("DAGSTER_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DAGSTER_COMPUTE_LOG_DIR", str(tmp_path / "compute-logs"))
    config_dir = Path(__file__).resolve().parents[1] / "dagster"
    instance = dg.DagsterInstance.from_config(str(config_dir))
    try:
        assert type(instance.run_storage).__name__ == "PostgresRunStorage"
        assert type(instance.event_log_storage).__name__ == "PostgresEventLogStorage"
        assert type(instance.schedule_storage).__name__ == "PostgresScheduleStorage"
        conn = db.connect()
        try:
            dagster_table_count = conn.execute(
                "select count(*) from information_schema.tables where table_schema = 'dagster'"
            ).fetchone()[0]
            leaked_public_tables = conn.execute(
                """
                select table_name from information_schema.tables
                where table_schema = 'public'
                  and table_name = any(%s)
                """,
                (["runs", "event_logs", "jobs", "instigators", "job_ticks"],),
            ).fetchall()
        finally:
            conn.close()
    finally:
        instance.dispose()
    assert dagster_table_count > 0
    assert leaked_public_tables == []
