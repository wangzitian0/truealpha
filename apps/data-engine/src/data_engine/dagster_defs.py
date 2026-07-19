"""Deployable Dagster entrypoint for isolated Staging / Production (#27).

The infra2 deploy surface (`truealpha/truealpha/20.data_engine/compose.yaml`)
loads THIS module in both roles:

    dagster-webserver -m data_engine.dagster_defs     # loopback-only UI
    dagster-daemon    run -m data_engine.dagster_defs  # sole recurring-run authority

Unlike the hermetic Local/CI asset modules (`core_strategy_replay_assets`,
`strategy_backtest_assets`, ...), which deliberately expose no schedule or
sensor, this is the ONE composition that carries a schedule: the compose's
daemon is the single durable recurring-run authority the acceptance criteria
require, and schedule state persists in the `dagster` Postgres schema.

Hermeticity is preserved two ways:

* No database work happens at import. `dagster -m` imports this module to build
  the `Definitions`; the connection is opened lazily inside the op, from the
  environment's DATABASE_URL, only when a run executes. CI collection and the
  webserver's definition load touch no database.
* `executed_at` is the schedule's own tick time, never `datetime.now()`. Two
  consecutive scheduled cycles carry distinct tick times -> distinct
  `strategy_run_id`s (two-cycle proof); retrying the same tick carries the same
  tick time -> the same `run_key` and the same content-addressed run id
  (idempotent retry). This is exactly the reproducibility #27 asks for.

The scheduled job runs the real #395 data path: capture the strategy's factor
inputs into `staging.strategy_backtest_inputs`, evaluate the single-source
`strategy_evaluator` over the `StrategyBacktestGateway`, and persist to
`mart.strategy_*` with a content-addressed PIT snapshot. The full
raw->staging capture DAG (#58 manifest, real-source sweeps) is the remaining
asset work under #27; this entrypoint is the deployable Core-strategy run
authority it plugs into.
"""

import json
from datetime import datetime

import dagster as dg
import psycopg
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.config import settings
from data_engine.core_strategy_replay import _load_corpus
from data_engine.strategy_backtest_gateway import run_backtest_from_staging, seed_strategy_backtest_inputs
from data_engine.strategy_replay_repository import write_replay

CORE_STRATEGY_CANARY_JOB_NAME = "core_strategy_canary"
# Daily at 06:00 UTC. A bounded canary cadence -- #27 proves two consecutive
# scheduled cycles, not natural-refresh soak (#51) or continuous operation (#67).
CORE_STRATEGY_CANARY_CRON = "0 6 * * *"


class CoreStrategyCanaryConfig(dg.Config):
    """`executed_at` is injected by the schedule from its tick time (ISO 8601),
    never read from the wall clock inside the run -- see the module docstring."""

    executed_at: str


@dg.op
def run_core_strategy_canary(context: dg.OpExecutionContext, config: CoreStrategyCanaryConfig) -> str:
    executed_at = datetime.fromisoformat(config.executed_at)
    corpus = _load_corpus()
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))

    # Lazy, run-time connection from the environment (DATABASE_URL). autocommit
    # is off; we commit once the full replay has landed so a mid-run failure
    # leaves no partial run -- the retry re-runs the whole tick idempotently.
    with psycopg.connect(settings.database_url) as connection:
        seed_strategy_backtest_inputs(connection, corpus)
        decisions, snapshot_id = run_backtest_from_staging(connection, corpus, definition)
        run_id, decision_ids = write_replay(
            connection, decisions, definition, executed_at=executed_at, snapshot_id=snapshot_id
        )
        connection.commit()

    context.log.info(
        f"Core Strategy canary: run {run_id}, {len(decision_ids)} decisions, "
        f"snapshot {snapshot_id}, executed_at {executed_at.isoformat()}"
    )
    context.add_output_metadata(
        {"strategy_run_id": run_id, "decision_count": len(decision_ids), "snapshot_id": snapshot_id}
    )
    return run_id


@dg.job(name=CORE_STRATEGY_CANARY_JOB_NAME)
def core_strategy_canary_job() -> None:
    run_core_strategy_canary()


@dg.schedule(
    job=core_strategy_canary_job,
    cron_schedule=CORE_STRATEGY_CANARY_CRON,
    execution_timezone="UTC",
    # Start paused: enabling the schedule in a live environment is a deliberate
    # operator action, not a side effect of a deploy.
    default_status=dg.DefaultScheduleStatus.STOPPED,
)
def core_strategy_canary_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    executed_at = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        # run_key == the tick time: the daemon dedupes a re-evaluated tick to a
        # single run, so an identical partition retry is idempotent.
        run_key=executed_at,
        run_config=dg.RunConfig(ops={"run_core_strategy_canary": CoreStrategyCanaryConfig(executed_at=executed_at)}),
    )


defs = dg.Definitions(
    jobs=[core_strategy_canary_job],
    schedules=[core_strategy_canary_schedule],
)
