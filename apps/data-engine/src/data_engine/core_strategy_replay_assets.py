"""Dagster asset wrapping the Core Strategy replay preview (#26).

Fixture-sourced today -- see `data_engine.core_strategy_replay`'s own
docstring for the exact scope boundary (preview evidence against #335's
golden corpus, not #26's full acceptance). This is nonetheless the
structural piece #26's remediation names ("a thin Dagster backtest asset
that... invokes the adapters, evaluates eligibility/rank, creates target
decisions"), independent of whether the underlying facts are real captured
data yet -- matching the pattern `mvp_probe.py` already established for
fixture-sourced assets in this repo (materialize now against a fixture,
swap the resource for a real repository later without changing the asset's
shape).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import CORPUS_SHA256, Decision, run
from data_engine.strategy_replay_repository import write_replay

CORE_STRATEGY_REPLAY_ASSET_NAME = "core_strategy_replay_preview"
CORE_STRATEGY_REPLAY_MART_ASSET_NAME = "core_strategy_replay_mart"


@dg.asset(
    name=CORE_STRATEGY_REPLAY_ASSET_NAME,
    group_name="core_strategy_replay",
    description=(
        "Preview replay of large_model_value_v0 against #335's golden fixture. "
        "Not #26's full acceptance evidence -- see the module docstring."
    ),
)
def materialize_core_strategy_replay_preview(
    context: AssetExecutionContext,
) -> dg.Output[tuple[list[Decision], LargeModelValueV0Definition]]:
    decisions, definition = run()
    content_sha256 = canonical_sha256([decision.to_json() for decision in decisions])
    selected_count = sum(1 for decision in decisions if decision.outcome == "selected")
    context.log.info(
        f"Core Strategy replay preview: {len(decisions)} decisions, {selected_count} selected, corpus {CORPUS_SHA256}"
    )
    return dg.Output(
        (decisions, definition),
        metadata={
            "decision_count": len(decisions),
            "selected_count": selected_count,
            "corpus_sha256": CORPUS_SHA256,
            "strategy_id": definition.strategy_id,
        },
        data_version=dg.DataVersion(content_sha256),
    )


@dataclass(frozen=True)
class CoreStrategyReplayMartResource:
    """Explicit `executed_at`, never `datetime.now()` -- a replay materialized
    twice against the same fixture must be able to reproduce the exact same
    `strategy_run_id` (see `strategy_replay_repository.write_replay`)."""

    connection: Connection[Any]
    executed_at: datetime


@dg.asset(
    name=CORE_STRATEGY_REPLAY_MART_ASSET_NAME,
    group_name="core_strategy_replay",
    deps=[CORE_STRATEGY_REPLAY_ASSET_NAME],
    ins={"replay": dg.AssetIn(CORE_STRATEGY_REPLAY_ASSET_NAME)},
    required_resource_keys={"core_strategy_replay_mart_runner"},
    description=(
        "Persist the replay preview's decisions into mart.strategy_runs/strategy_decisions "
        "(db/migrations/0027_core_strategy_replay_mart.sql). Still 'preview' claim_ceiling -- "
        "see strategy_replay_repository's own docstring for what this does and does not claim."
    ),
)
def materialize_core_strategy_replay_mart(
    context: AssetExecutionContext,
    replay: tuple[list[Decision], LargeModelValueV0Definition],
) -> dg.Output[str]:
    decisions, definition = replay
    runner = cast(CoreStrategyReplayMartResource, context.resources.core_strategy_replay_mart_runner)
    run_id, decision_ids = write_replay(runner.connection, decisions, definition, executed_at=runner.executed_at)
    context.log.info(f"Persisted {run_id} with {len(decision_ids)} decisions")
    return dg.Output(
        run_id,
        metadata={
            "strategy_run_id": run_id,
            "decision_count": len(decision_ids),
        },
    )


CORE_STRATEGY_REPLAY_ASSETS = (materialize_core_strategy_replay_preview,)
CORE_STRATEGY_REPLAY_MART_ASSETS = (materialize_core_strategy_replay_preview, materialize_core_strategy_replay_mart)


def build_core_strategy_replay_definitions() -> dg.Definitions:
    return dg.Definitions(assets=list(CORE_STRATEGY_REPLAY_ASSETS))


def build_core_strategy_replay_mart_definitions(
    *, connection: Connection[Any], executed_at: datetime
) -> dg.Definitions:
    return dg.Definitions(
        assets=list(CORE_STRATEGY_REPLAY_MART_ASSETS),
        resources={
            "core_strategy_replay_mart_runner": cast(
                Any, CoreStrategyReplayMartResource(connection=connection, executed_at=executed_at)
            )
        },
    )
