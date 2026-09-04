"""Deployable Dagster entrypoint for isolated Staging / Production (#27).

The infra2 deploy surface (`truealpha/truealpha/20.data_engine/compose.yaml`)
loads THIS module in both roles:

    dagster-webserver -m data_engine.dagster_defs     # loopback-only UI
    dagster-daemon    run -m data_engine.dagster_defs  # sole recurring-run authority

Since #731 the module owns no job of its own: `defs` is the merge of every lane
registered in `data_engine.lanes.LANE_MODULES` (capture ticks + schedules, the weekly
universe refresh, the manual-trigger sensor). A lane adds or retires a job in its own
module; nothing here lists a job by name, and `test_dagster_defs.py` asserts the root
is exactly the union of what the lanes declare. The names re-exported below keep the
operator commands and tests that address `data_engine.dagster_defs` working.

No fixture data is seeded anywhere in the deployed composition (#429 invariant I2);
the retired fixture-seeded canary lives in `fixture_canary_definitions()` — an
explicitly named, tests-only composition that is NOT part of the deployed `defs`.
"""

from datetime import datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.lanes import LANE_MODULES, lane_definitions
from data_engine.lanes.capture import (
    CANARY_JOB_NAME,
    QQQ_LIVE_JOB_NAME,
    TOPT_LIVE_CRON,
    TOPT_LIVE_JOB_NAME,
    ToptLiveTickConfig,
    live_topt_cron,
    run_topt_live_tick,
    topt_live_schedule,
)
from data_engine.lanes.triggers import pipeline_trigger_sensor
from data_engine.lanes.universe_refresh import UNIVERSE_REFRESH_JOB_NAME

__all__ = [
    "CANARY_JOB_NAME",
    "CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME",
    "LANE_MODULES",
    "QQQ_LIVE_JOB_NAME",
    "TOPT_LIVE_CRON",
    "TOPT_LIVE_JOB_NAME",
    "UNIVERSE_REFRESH_JOB_NAME",
    "ToptLiveTickConfig",
    "defs",
    "fixture_canary_definitions",
    "live_topt_cron",
    "pipeline_trigger_sensor",
    "run_topt_live_tick",
    "topt_live_schedule",
]

defs = dg.Definitions.merge(*lane_definitions().values())


# -- retired fixture canary (tests only; never deployed) -------------------------------

CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME = "core_strategy_fixture_canary"


def fixture_canary_definitions() -> dg.Definitions:
    """The retired golden-fixture canary, explicitly named as a fixture (#429 I2).

    Kept ONLY so tests can prove the fixture path still replays deterministically;
    it is deliberately excluded from the deployed `defs` above — the deployed job
    graph contains no fixture seeding.
    """
    import json

    from truealpha_contracts.strategy import LargeModelValueV0Definition

    from data_engine.core_strategy_replay import _load_corpus
    from data_engine.strategy_backtest_gateway import run_backtest_from_staging, seed_strategy_backtest_inputs
    from data_engine.strategy_replay_repository import write_replay

    class FixtureCanaryConfig(dg.Config):
        executed_at: str

    @dg.op
    def run_core_strategy_fixture_canary(context: dg.OpExecutionContext, config: FixtureCanaryConfig) -> str:
        executed_at = datetime.fromisoformat(config.executed_at)
        corpus = _load_corpus()
        definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
        with psycopg.connect(settings.database_url) as connection:
            seed_strategy_backtest_inputs(connection, corpus)
            decisions, snapshot_id = run_backtest_from_staging(connection, corpus, definition)
            run_id, decision_ids = write_replay(
                connection, decisions, definition, executed_at=executed_at, snapshot_id=snapshot_id
            )
            connection.commit()
        context.log.info(f"fixture canary: run {run_id}, {len(decision_ids)} decisions")
        return run_id

    @dg.job(name=CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME)
    def core_strategy_fixture_canary_job() -> None:
        run_core_strategy_fixture_canary()

    return dg.Definitions(jobs=[core_strategy_fixture_canary_job])
