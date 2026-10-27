"""The nightly backtest of the core strategy, ``large_model_value_v0`` (#758, Milestone M3).

One op runs ``datahub.backtest.run_core_strategy_backtest``. It evaluates the strategy with
``strategy_evaluator.evaluate_cutoff`` at each calendar quarter end that holds point-in-time
inputs, fills each target at the next session's unadjusted open with the definition's cost,
applies the split events once, and writes ``mart.backtest_runs``, ``mart.backtest_valuations``
and ``mart.backtest_trades``. It records the nightly verdict ``nightly_backtest``: green when a
run is written or already exists, pending when this environment holds no quarter-end inputs or
no bars yet, red on any failure.

The schedule runs in production and in staging, so staging evidence exists before production.
21:45 UTC on weekdays is after the strategy history projection (20:07 UTC, ``lanes.strategy_history``),
after the market data refresh of both environments (21:15 UTC; 15 minutes in production, 20 minutes in
staging, #1281) and before the staging TOPT tick (22:45 UTC, the staging cron of the TOPT universe record).

Manual replay: launch the job from the Dagster UI launchpad, or through GraphQL, with the run
config below. A rerun of one release on the same data writes nothing. A new release on the same
data writes a new run, because run_id binds the deployed code identity (GIT_COMMIT_SHA). In staging
and production a run without that identity fails before it writes.

    ops:
      run_nightly_backtest_op:
        config:
          executed_at: "2026-10-12T21:45:00+00:00"
"""

import json

import dagster as dg
import psycopg
from truealpha_contracts.strategy import load_strategy_definition as load_packaged_strategy_definition

from data_engine.config import settings
from data_engine.datahub.backtest import DEPLOYED_ENVIRONMENTS, BacktestPending, run_core_strategy_backtest
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_from_config, tick_of, verdict

BACKTEST_JOB_NAME = "nightly_backtest_pipeline"
# Weekdays 21:45 UTC: 98 minutes after strategy_history_projection_pipeline (20:07 UTC), 30 minutes
# after market_data_refresh_pipeline starts (21:15 UTC), 60 minutes before the staging TOPT tick (22:45 UTC).
# The refresh makes 60 calls at the per-minute share of its environment, so it ends at 21:30 UTC in
# production and at 21:35 UTC in staging (#1281). The backtest waits at least 5 more minutes.
BACKTEST_CRON = "45 21 * * 1-5"
#: The environments whose schedule runs by default. Elsewhere the job runs only by hand. The same
#: tuple decides where a run must carry a code identity (`datahub.backtest.deployed_code_identity`).
SCHEDULED_ENVIRONMENTS = DEPLOYED_ENVIRONMENTS

NIGHTLY_BACKTEST_VERDICT = "nightly_backtest"
NIGHTLY_VERDICTS: tuple[str, ...] = (NIGHTLY_BACKTEST_VERDICT,)


class NightlyBacktestConfig(dg.Config):
    """`executed_at` is the tick time (ISO 8601 with a time zone), never the wall clock.
    Optional `definition_resource`, `overrides_json`, `start_cutoff` and `end_cutoff` support
    parameterized and windowed backtests (#1413 B1).
    """

    executed_at: str
    definition_resource: str | None = None
    overrides_json: str | None = None
    start_cutoff: str | None = None
    end_cutoff: str | None = None


def _backtest_kwargs_from_config(config: NightlyBacktestConfig) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if config.definition_resource:
        kwargs["definition"] = load_packaged_strategy_definition(config.definition_resource)
    if config.overrides_json:
        kwargs["overrides"] = json.loads(config.overrides_json)
    if config.start_cutoff:
        kwargs["start_cutoff"] = tick_from_config(config.start_cutoff)
    if config.end_cutoff:
        kwargs["end_cutoff"] = tick_from_config(config.end_cutoff)
    return kwargs


def schedule_status(app_env: str) -> dg.DefaultScheduleStatus:
    """RUNNING in production (including the `prod` alias) and staging, STOPPED in any other environment."""
    return (
        dg.DefaultScheduleStatus.RUNNING
        if app_env.strip().lower() in SCHEDULED_ENVIRONMENTS
        else dg.DefaultScheduleStatus.STOPPED
    )


@dg.op
def run_nightly_backtest_op(context: dg.OpExecutionContext, config: NightlyBacktestConfig) -> None:
    """Backtest large_model_value_v0 over the quarter-end history and persist the run."""
    tick = tick_from_config(config.executed_at)
    extra_kwargs = _backtest_kwargs_from_config(config)
    with verdict(
        NIGHTLY_BACKTEST_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        with psycopg.connect(settings.database_url) as connection:
            try:
                result = run_core_strategy_backtest(connection, tick=tick, **extra_kwargs)  # type: ignore[arg-type]
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
    default_status=schedule_status(settings.app_env),
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
