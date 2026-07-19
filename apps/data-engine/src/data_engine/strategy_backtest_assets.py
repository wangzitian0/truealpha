"""Dagster asset: materialize the Core Strategy on captured staging data (#395).

Unlike the fixture-sourced `core_strategy_replay` preview asset, this runs the real
product path -- it captures the strategy's provenance-neutral factor inputs into
`staging.strategy_backtest_inputs`, evaluates the single-source `strategy_evaluator`
over the `StrategyBacktestGateway`, and persists to `mart.strategy_*` with a
content-addressed PIT `snapshot_id`. There is no fixture-direct read in this path.
The persisted mart rows are what the consumption lane reads once App/MCP flip off the
strategy-run fixture (#362).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import _load_corpus
from data_engine.strategy_backtest_gateway import run_backtest_from_staging, seed_strategy_backtest_inputs
from data_engine.strategy_replay_repository import write_replay

STRATEGY_BACKTEST_MART_ASSET_NAME = "strategy_backtest_mart"


@dataclass(frozen=True)
class StrategyBacktestMartResource:
    """Explicit `executed_at` (never `datetime.now()`) so a re-materialization against
    the same captured snapshot reproduces the same `strategy_run_id`."""

    connection: Connection[Any]
    executed_at: datetime


@dg.asset(
    name=STRATEGY_BACKTEST_MART_ASSET_NAME,
    group_name="strategy_backtest",
    required_resource_keys={"strategy_backtest_mart_runner"},
    description=(
        "Capture the strategy's factor inputs into staging, run the single-source evaluator "
        "over the BacktestDataGateway, and persist to mart.strategy_* with PIT snapshot lineage (#395)."
    ),
)
def materialize_strategy_backtest_mart(context: AssetExecutionContext) -> dg.Output[str]:
    runner = cast(StrategyBacktestMartResource, context.resources.strategy_backtest_mart_runner)
    corpus = _load_corpus()
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))

    seed_strategy_backtest_inputs(runner.connection, corpus)
    decisions, snapshot_id = run_backtest_from_staging(runner.connection, corpus, definition)
    run_id, decision_ids = write_replay(
        runner.connection, decisions, definition, executed_at=runner.executed_at, snapshot_id=snapshot_id
    )

    context.log.info(
        f"Strategy backtest materialized from staging: run {run_id}, "
        f"{len(decision_ids)} decisions, snapshot {snapshot_id}"
    )
    return dg.Output(
        run_id,
        metadata={
            "strategy_run_id": run_id,
            "decision_count": len(decision_ids),
            "snapshot_id": snapshot_id,
        },
    )


STRATEGY_BACKTEST_ASSETS = (materialize_strategy_backtest_mart,)


def build_strategy_backtest_definitions(*, connection: Connection[Any], executed_at: datetime) -> dg.Definitions:
    return dg.Definitions(
        assets=list(STRATEGY_BACKTEST_ASSETS),
        resources={
            "strategy_backtest_mart_runner": cast(
                Any, StrategyBacktestMartResource(connection=connection, executed_at=executed_at)
            )
        },
    )
