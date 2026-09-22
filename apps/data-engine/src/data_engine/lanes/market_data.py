"""Daily multi-resolution market data refresh and PIT universe mask (#101).

Wires `data_engine.datahub.market_prices` and `data_engine.datahub.universe_mask`
into the deployed composition root, following the #731 lane convention (a lane
adds a job by editing only its own module; `tools/reachability_ratchet.py` and
`test_dagster_defs.py::test_every_lane_module_is_registered` both fail a module
under this package that is not wired into some deployed job).

One op: ingest the TOPT universe's OHLCV bars for both resolutions the `bt`
engine consumes (1D for 3y drawdown/Sharpe, 1M for 10y CAGR — see
`libs/factors/src/factors/backtest/engine.py`), then recompute
`staging.universe_mask` for today's cutoff from what was just ingested, for
each resolution. No lookahead: the mask is only ever computed from bars at or
before its own cutoff date (`universe_mask.evaluate_symbol_pit`).
"""

from datetime import UTC, datetime

import dagster as dg
import psycopg

from data_engine.config import settings

MARKET_DATA_REFRESH_JOB_NAME = "market_data_refresh_pipeline"
# Weekdays 21:15 UTC: after the XNYS 20:00 UTC close, ahead of `universe_refresh`'s
# Saturday 08:07 UTC run so that lane's weekly mask read always has the week's own
# prices already ingested.
MARKET_DATA_REFRESH_CRON = "15 21 * * 1-5"


@dg.op
def refresh_market_data_op(context: dg.OpExecutionContext) -> None:
    """Ingest OHLCV bars for the TOPT universe, then recompute the PIT universe mask."""
    from data_engine.datahub.market_prices import DEFAULT_TOPT_SYMBOLS, ingest_twelve_data_market_prices
    from data_engine.datahub.universe_mask import compute_and_persist_universe_mask_from_db

    as_of = datetime.now(UTC).date()
    with psycopg.connect(settings.database_url) as connection:
        summary = ingest_twelve_data_market_prices(
            symbols=DEFAULT_TOPT_SYMBOLS,
            connection=connection,
            as_of=as_of,
        )
        connection.commit()
        context.log.info(
            f"market_data: ingested {summary.daily_inserted} daily / {summary.monthly_inserted} monthly rows "
            f"over {summary.total_calls} calls for {summary.total_symbols} symbols"
        )

        daily_mask = compute_and_persist_universe_mask_from_db(
            connection,
            symbols=DEFAULT_TOPT_SYMBOLS,
            cutoff_dates=[as_of],
            source_table="staging.market_prices_daily",
            resolution="1D",
        )
        monthly_mask = compute_and_persist_universe_mask_from_db(
            connection,
            symbols=DEFAULT_TOPT_SYMBOLS,
            cutoff_dates=[as_of],
            source_table="staging.market_prices_monthly",
            resolution="1M",
        )
        connection.commit()
        context.log.info(
            f"universe_mask: recomputed {len(daily_mask)} daily + {len(monthly_mask)} monthly records for {as_of}"
        )


@dg.job(name=MARKET_DATA_REFRESH_JOB_NAME)
def market_data_refresh_pipeline_job() -> None:
    refresh_market_data_op()


@dg.schedule(
    job=market_data_refresh_pipeline_job,
    cron_schedule=MARKET_DATA_REFRESH_CRON,
    execution_timezone="UTC",
    # Production-only, like the other scheduled lanes; staging/dev exercise this
    # via an operator script when needed.
    default_status=(
        dg.DefaultScheduleStatus.RUNNING
        if settings.app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    ),
)
def market_data_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())


defs = dg.Definitions(jobs=[market_data_refresh_pipeline_job], schedules=[market_data_refresh_schedule])
