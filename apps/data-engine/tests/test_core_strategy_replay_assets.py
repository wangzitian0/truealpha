from __future__ import annotations

import dagster as dg
from data_engine.core_strategy_replay import Decision, run
from data_engine.core_strategy_replay_assets import (
    CORE_STRATEGY_REPLAY_ASSET_NAME,
    build_core_strategy_replay_definitions,
)


def _materialization(result: dg.ExecuteInProcessResult):
    return next(
        event.event_specific_data.materialization
        for event in result.all_events
        if event.is_step_materialization and event.step_key == CORE_STRATEGY_REPLAY_ASSET_NAME
    )


def test_asset_materializes_the_same_decisions_run_produces_directly() -> None:
    definitions = build_core_strategy_replay_definitions()
    dg.Definitions.validate_loadable(definitions)

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    assert result.success
    decisions, definition = result.output_for_node(CORE_STRATEGY_REPLAY_ASSET_NAME)
    expected_decisions, expected_definition = run()
    assert decisions == expected_decisions
    assert definition == expected_definition
    assert len(decisions) == 10
    assert all(isinstance(item, Decision) for item in decisions)


def test_asset_materialization_metadata_matches_the_replay() -> None:
    definitions = build_core_strategy_replay_definitions()
    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    metadata = _materialization(result).metadata
    assert metadata["decision_count"].value == 10
    assert metadata["selected_count"].value == 4
    assert metadata["strategy_id"].value == "large_model_value_v0"


def test_repeated_materialization_is_idempotent_same_data_version() -> None:
    definitions = build_core_strategy_replay_definitions()

    first = definitions.get_implicit_global_asset_job_def().execute_in_process()
    second = definitions.get_implicit_global_asset_job_def().execute_in_process()

    first_version = _materialization(first).tags["dagster/data_version"]
    second_version = _materialization(second).tags["dagster/data_version"]
    assert first_version == second_version
