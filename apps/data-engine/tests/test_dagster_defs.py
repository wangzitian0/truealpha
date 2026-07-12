from datetime import UTC, datetime
from pathlib import Path

import dagster as dg
import pytest
from data_engine import db
from data_engine.capture import runs
from data_engine.config import settings
from data_engine.dagster_defs import (
    SCHEDULE_NAME,
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
