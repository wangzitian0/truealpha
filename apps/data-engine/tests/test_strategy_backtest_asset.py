from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.strategy_backtest_assets import (
    STRATEGY_BACKTEST_MART_ASSET_NAME,
    build_strategy_backtest_definitions,
)

_EXECUTED_AT = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_asset_materializes_strategy_from_staging_to_mart(connection) -> None:
    # #395: the real product path -- capture -> gateway -> evaluator -> mart with
    # snapshot lineage, driven as a Dagster asset (no fixture-direct read).
    definitions = build_strategy_backtest_definitions(connection=connection, executed_at=_EXECUTED_AT)

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    assert result.success

    run_id = result.output_for_node(STRATEGY_BACKTEST_MART_ASSET_NAME)
    assert run_id.startswith("strategy-run:")

    snapshot_id = connection.execute(
        "select snapshot_id from mart.strategy_runs where strategy_run_id = %s", (run_id,)
    ).fetchone()
    assert snapshot_id is not None and snapshot_id[0].startswith("strategy-snapshot:")

    decision_count = connection.execute(
        "select count(*) from mart.strategy_decisions where strategy_run_id = %s", (run_id,)
    ).fetchone()
    assert decision_count == (10,)


def test_asset_composition_is_explicit_local_ci_only(connection) -> None:
    definitions = build_strategy_backtest_definitions(connection=connection, executed_at=_EXECUTED_AT)
    assert not definitions.schedules
    assert not definitions.sensors
