import os
from datetime import UTC, datetime

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.core_strategy_replay_assets import (
    CORE_STRATEGY_REPLAY_MART_ASSET_NAME,
    build_core_strategy_replay_mart_definitions,
)

_EXECUTED_AT = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


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


def _materialization(result: dg.ExecuteInProcessResult):
    return next(
        event.event_specific_data.materialization
        for event in result.all_events
        if event.is_step_materialization and event.step_key == CORE_STRATEGY_REPLAY_MART_ASSET_NAME
    )


def test_dagster_composition_is_explicit_local_ci_only(connection) -> None:
    import data_engine.core_strategy_replay_assets as core_strategy_replay_assets

    definitions = build_core_strategy_replay_mart_definitions(connection=connection, executed_at=_EXECUTED_AT)
    dg.Definitions.validate_loadable(definitions)
    assert not definitions.schedules
    assert not definitions.sensors
    assert not hasattr(core_strategy_replay_assets, "defs")


def test_mart_asset_persists_all_ten_decisions_from_the_upstream_replay(connection) -> None:
    definitions = build_core_strategy_replay_mart_definitions(connection=connection, executed_at=_EXECUTED_AT)

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    assert result.success
    run_id = result.output_for_node(CORE_STRATEGY_REPLAY_MART_ASSET_NAME)
    assert run_id.startswith("strategy-run:")

    count = connection.execute(
        "select count(*) from mart.strategy_decisions where strategy_run_id = %s", (run_id,)
    ).fetchone()[0]
    assert count == 10


def test_mart_asset_metadata_reports_run_and_decision_count(connection) -> None:
    definitions = build_core_strategy_replay_mart_definitions(connection=connection, executed_at=_EXECUTED_AT)
    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    metadata = _materialization(result).metadata
    assert metadata["decision_count"].value == 10
    assert metadata["strategy_run_id"].value.startswith("strategy-run:")


def test_repeated_materialization_reuses_the_same_run_id(connection) -> None:
    definitions = build_core_strategy_replay_mart_definitions(connection=connection, executed_at=_EXECUTED_AT)

    first = definitions.get_implicit_global_asset_job_def().execute_in_process()
    second = definitions.get_implicit_global_asset_job_def().execute_in_process()

    first_run_id = first.output_for_node(CORE_STRATEGY_REPLAY_MART_ASSET_NAME)
    second_run_id = second.output_for_node(CORE_STRATEGY_REPLAY_MART_ASSET_NAME)
    assert first_run_id == second_run_id

    count = connection.execute(
        "select count(*) from mart.strategy_runs where strategy_run_id = %s", (first_run_id,)
    ).fetchone()[0]
    assert count == 1
