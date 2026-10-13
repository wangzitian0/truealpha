"""The nightly backtest of the core strategy, ``large_model_value_v0`` (#758, Milestone M3).

One op runs ``datahub.backtest.run_core_strategy_backtest``. It evaluates the strategy with
``strategy_evaluator.evaluate_cutoff`` at each calendar quarter end that holds point-in-time
inputs, fills each target at the next session's unadjusted open with the definition's cost,
applies the split events once, and writes ``mart.backtest_runs``, ``mart.backtest_valuations``
and ``mart.backtest_trades``. It records the nightly verdict ``nightly_backtest``: green when a
run is written or already exists, pending when this environment holds no quarter-end inputs or
no bars yet, red on any failure.

The schedule runs in production and in staging, so staging evidence exists before production.
21:30 UTC on weekdays is after the production market data refresh (21:15 UTC) and before the
staging TOPT tick (22:45 UTC, ``lanes.capture.live_topt_cron``).

Manual replay: launch the job from the Dagster UI launchpad, or through GraphQL, with the run
config below. A rerun on the same data writes nothing.

    ops:
      run_nightly_backtest_op:
        config:
          executed_at: "2026-10-12T21:30:00+00:00"
"""

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.backtest import BacktestPending, run_core_strategy_backtest
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_from_config, tick_of, verdict

BACKTEST_JOB_NAME = "nightly_backtest_pipeline"
# Weekdays 21:30 UTC: 15 minutes after market_data_refresh_pipeline (21:15 UTC), 75 minutes
# before the staging TOPT tick (22:45 UTC).
BACKTEST_CRON = "30 21 * * 1-5"
#: The environments whose schedule runs by default. Elsewhere the job runs only by hand.
SCHEDULED_ENVIRONMENTS = ("production", "prod", "staging")

NIGHTLY_BACKTEST_VERDICT = "nightly_backtest"
NIGHTLY_VERDICTS: tuple[str, ...] = (NIGHTLY_BACKTEST_VERDICT,)


class NightlyBacktestConfig(dg.Config):
    """`executed_at` is the tick time (ISO 8601 with a time zone), never the wall clock."""

    executed_at: str


@dg.op
def run_nightly_backtest_op(context: dg.OpExecutionContext, config: NightlyBacktestConfig) -> None:
    """Backtest large_model_value_v0 over the quarter-end history and persist the run."""
    tick = tick_from_config(config.executed_at)
    with verdict(
        NIGHTLY_BACKTEST_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        with psycopg.connect(settings.database_url) as connection:
            try:
                result = run_core_strategy_backtest(connection, tick=tick)
            except BacktestPending as pending:
                connection.rollback()
                outcome.pending = True
                outcome.summary = f"pending: {pending}"
                context.log.info(f"nightly_backtest: {outcome.summary}")
                return
            connection.commit()
        outcome.summary = result.summary
        context.log.info(f"nightly_backtest: {result.summary}")
        context.add_output_metadata(
            {
                "run_id": result.run_id,
                "inserted": result.inserted,
                "strategy_key": result.strategy_key,
                "cutoffs": result.cutoffs,
                "trades": result.trades,
                "cagr": None if result.strategy.cagr is None else float(result.strategy.cagr),
                "benchmark_cagr": None if result.benchmark.cagr is None else float(result.benchmark.cagr),
                "max_drawdown": float(result.strategy.max_drawdown),
            }
        )


@dg.job(name=BACKTEST_JOB_NAME)
def nightly_backtest_pipeline_job() -> None:
    run_nightly_backtest_op()


@dg.schedule(
    job=nightly_backtest_pipeline_job,
    cron_schedule=BACKTEST_CRON,
    execution_timezone="UTC",
    default_status=(
        dg.DefaultScheduleStatus.RUNNING
        if settings.app_env.strip().lower() in SCHEDULED_ENVIRONMENTS
        else dg.DefaultScheduleStatus.STOPPED
    ),
)
def nightly_backtest_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=tick,
        tags={TICK_TAG: tick},
        run_config={"ops": {"run_nightly_backtest_op": {"config": {"executed_at": tick}}}},
    )


defs = dg.Definitions(
    jobs=[nightly_backtest_pipeline_job],
    schedules=[nightly_backtest_schedule],
)
