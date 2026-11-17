"""Nightly backtest lane for ``large_model_value_v0`` (#758, M3)."""

import json

import dagster as dg
import psycopg
from truealpha_contracts.strategy import load_strategy_definition as load_packaged_strategy_definition

from data_engine.config import settings
from data_engine.datahub.backtest import DEPLOYED_ENVIRONMENTS, BacktestPending, run_core_strategy_backtest
from data_engine.external_op import external_op
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_from_config, tick_of, verdict

BACKTEST_JOB_NAME = "nightly_backtest_pipeline"
BACKTEST_CRON = "45 21 * * 1-5"
SCHEDULED_ENVIRONMENTS = DEPLOYED_ENVIRONMENTS

NIGHTLY_BACKTEST_VERDICT = "nightly_backtest"
NIGHTLY_VERDICTS: tuple[str, ...] = (NIGHTLY_BACKTEST_VERDICT,)


class NightlyBacktestConfig(dg.Config):
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
    return (
        dg.DefaultScheduleStatus.RUNNING
        if app_env.strip().lower() in SCHEDULED_ENVIRONMENTS
        else dg.DefaultScheduleStatus.STOPPED
    )


@external_op
def run_nightly_backtest_op(context: dg.OpExecutionContext, config: NightlyBacktestConfig) -> None:
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


defs = dg.Definitions(jobs=[nightly_backtest_pipeline_job], schedules=[nightly_backtest_schedule])
