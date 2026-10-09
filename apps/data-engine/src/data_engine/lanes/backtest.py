"""Nightly backtest simulation and mart persistence lane (#758, Milestone M3).

This lane runs on weekdays at 21:30 UTC, 15 minutes after market data refresh.
It loads monthly and daily market prices and universe eligibility masks.
It compiles the factor panel and calculates Top-K Dropout target weights.
It simulates portfolio performance and writes backtest runs, valuations, and trades to mart tables.
It records a nightly verdict in mart.nightly_verdicts.
"""

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.backtest import run_backtest
from data_engine.datahub.market_prices import DEFAULT_TOPT_SYMBOLS, SPLIT_ADJUSTED
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_of, verdict

BACKTEST_JOB_NAME = "nightly_backtest_pipeline"
# Weekdays 21:30 UTC: 15 minutes after market_data_refresh_pipeline (21:15 UTC).
BACKTEST_CRON = "30 21 * * 1-5"

NIGHTLY_BACKTEST_VERDICT = "nightly_backtest"
NIGHTLY_VERDICTS: tuple[str, ...] = (NIGHTLY_BACKTEST_VERDICT,)


class NightlyBacktestConfig(dg.Config):
    """Configuration for nightly backtest execution."""

    strategy_key: str = "demo_topk_rank"
    strategy_version: str = "v1"
    universe_id: str = "topt"
    factor_column: str = "close"
    top_k: int = 5
    dropout_k: int = 2


@dg.op
def run_nightly_backtest_op(context: dg.OpExecutionContext, config: NightlyBacktestConfig) -> None:
    """Execute nightly portfolio backtest and persist results to mart tables."""
    with verdict(
        NIGHTLY_BACKTEST_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        with psycopg.connect(settings.database_url) as connection:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    select 1 from staging.market_prices_monthly
                    where symbol = any(%s) and adjust = %s
                    limit 1
                    """,
                    (list(DEFAULT_TOPT_SYMBOLS), SPLIT_ADJUSTED),
                )
                has_prices = cur.fetchone() is not None

            if not has_prices:
                outcome.pending = True
                outcome.summary = "pending: no monthly prices in staging.market_prices_monthly"
                context.log.info("nightly_backtest: no monthly prices in staging; marking pending")
                return

            result = run_backtest(
                connection,
                strategy_key=config.strategy_key,
                strategy_version=config.strategy_version,
                universe_id=config.universe_id,
                symbols=DEFAULT_TOPT_SYMBOLS,
                factor_column=config.factor_column,
                top_k=config.top_k,
                dropout_k=config.dropout_k,
            )
            connection.commit()

            if result.status != "succeeded":
                outcome.summary = f"failed: {result.error_message or 'backtest failed'}"
                raise dg.Failure(f"backtest simulation failed: {result.error_message}")

            summary_text = (
                f"succeeded: {result.run_id[:25]} cagr={result.cagr_monthly:.4f} "
                f"sharpe={result.sharpe_daily:.4f} max_dd={result.max_dd_daily:.4f}"
            )
            outcome.summary = summary_text
            context.log.info(f"nightly_backtest: {summary_text}")
            context.add_output_metadata(
                {
                    "run_id": result.run_id,
                    "strategy_key": result.strategy_key,
                    "cagr_monthly": float(result.cagr_monthly),
                    "sharpe_daily": float(result.sharpe_daily),
                    "max_dd_daily": float(result.max_dd_daily),
                    "vol_daily": float(result.vol_daily),
                    "turnover_monthly": float(result.turnover_monthly),
                    "status": result.status,
                    "trades_count": len(result.trades),
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
        if settings.app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    ),
)
def nightly_backtest_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=tick,
        tags={TICK_TAG: tick},
        run_config={"ops": {"run_nightly_backtest_op": {"config": {}}}},
    )


defs = dg.Definitions(
    jobs=[nightly_backtest_pipeline_job],
    schedules=[nightly_backtest_schedule],
)
