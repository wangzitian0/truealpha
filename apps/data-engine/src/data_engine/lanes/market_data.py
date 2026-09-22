"""Daily multi-resolution market data refresh and PIT universe mask (#938 layer 1).

Wires `data_engine.datahub.market_prices` and `data_engine.datahub.universe_mask` into
the deployed composition root, following the #731 lane convention (a lane adds a job by
editing only its own module; `tools/reachability_ratchet.py` and
`test_dagster_defs.py::test_every_lane_module_is_registered` both fail a module under
this package that is not wired into some deployed job).

One op: ingest the TOPT universe's OHLCV bars for both resolutions the `bt` engine
consumes (1D for 3y drawdown/Sharpe, 1M for 10y CAGR), then recompute
`staging.universe_mask` for every cutoff each resolution's price table actually holds --
not just today's (#938 contract item 2: a fresh table has zero mask rows for ten years of
backfilled monthly history until this covers every date already ingested, not only the
newest one).

Every vendor call this op makes is attributed to this Dagster run in the external call
ledger (#729) and admitted by the rule-6 gate first (`gateway.run_scope` +
`gateway.capacity_scope`, #938 contract item 3): before this fix, `market_prices.py`
routed its Twelve Data calls through `gateway.urlopen` for the ledger row alone -- no
`capacity_scope` was ever bound around this op, so `record_call`'s `gate = _bound_gate.get()`
was always `None` and nothing throttled or budgeted the calls this lane made. That is the
same failure shape `sources/gateway.py`'s `BudgetExhausted` docstring calls "the August
freeze": a spent shared key that looks like a quiet vendor because nothing admitted or
refused the calls that spent it.

No lookahead: the mask is only ever computed from bars at or before its own cutoff date
(`universe_mask.evaluate_symbol_pit`), and a cutoff is always a real XNYS session
(`_daily_cutoff`/`_monthly_cutoff`) -- never a raw wall-clock date, which is what made
~20 of every 21 trading days in a month read the whole universe as `suspended` under 1M
before this fix (contract item 1).
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.sources import gateway

if TYPE_CHECKING:
    from data_engine.datahub.market_prices import TwelveDataClient

MARKET_DATA_REFRESH_JOB_NAME = "market_data_refresh_pipeline"
# Weekdays 21:15 UTC: after the XNYS 20:00 UTC close, ahead of `universe_refresh`'s
# Saturday 08:07 UTC run so that lane's weekly mask read always has the week's own
# prices already ingested.
MARKET_DATA_REFRESH_CRON = "15 21 * * 1-5"


def _daily_cutoff(as_of: date) -> date:
    """The most recent XNYS trading session at or before `as_of`.

    Never `as_of` itself: a run on a weekend or a market holiday has no daily bar for
    that raw date, and `evaluate_symbol_pit`'s exact-date match on 1D would misread the
    whole universe as `suspended` for the same reason contract item 1 documents for 1M.
    """
    from data_engine.datahub.market_prices import most_recent_xnys_session

    return most_recent_xnys_session(as_of)


def _monthly_cutoff(as_of: date) -> date:
    """The current month's own last XNYS session -- never `as_of` itself (#938 contract
    item 1). `parse_monthly_bars` already snaps every monthly bar to this date, so a raw
    `as_of` cutoff mismatches the bar it should match on ~20 of every 21 trading days and
    reads the whole universe as `suspended`."""
    from data_engine.datahub.market_prices import last_xnys_session_of_month

    return last_xnys_session_of_month(as_of.year, as_of.month)


def _distinct_trading_dates(connection: psycopg.Connection, table: str, symbols: Sequence[str]) -> list[date]:
    """Every date this resolution's price table already holds for `symbols` -- the
    backfill cutoff set (#938 contract item 2). Deriving cutoffs from the data actually
    ingested, rather than an independent calendar sweep, keeps every mask cutoff aligned
    to a bar that can actually answer it."""
    query = f"select distinct trading_date from {table} where symbol = any(%s) order by trading_date;"
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols),))
        return [row[0] for row in cur.fetchall()]


def _refresh_market_data(
    context: dg.OpExecutionContext,
    connection: psycopg.Connection,
    *,
    symbols: Sequence[str],
    as_of: date,
    client: "TwelveDataClient | None" = None,
) -> dict[str, Any]:
    """The op body, factored out so a test can inject a fake Twelve Data transport and a
    real (or fake) connection without going through `dg.OpExecutionContext` plumbing --
    the #731 lane convention `universe_refresh._refresh_universes` also follows."""
    from data_engine.datahub.market_prices import DEFAULT_TOPT_SYMBOLS, ingest_twelve_data_market_prices
    from data_engine.datahub.universe_mask import compute_and_persist_universe_mask_from_db

    symbols = tuple(symbols) or DEFAULT_TOPT_SYMBOLS

    summary = ingest_twelve_data_market_prices(
        symbols=symbols,
        client=client,
        connection=connection,
        as_of=as_of,
    )
    context.log.info(
        f"market_data: ingested {summary.daily_inserted} daily / {summary.monthly_inserted} monthly new-vintage "
        f"rows over {summary.total_calls} calls for {summary.total_symbols} symbols"
    )

    # Backfill: every cutoff either resolution's table already holds, plus this run's own
    # cutoff (covers a run whose fetch yielded nothing new, e.g. a holiday or an
    # unchanged re-fetch de-duplicated by `_latest_vintages`).
    daily_cutoffs = sorted(
        set(_distinct_trading_dates(connection, "staging.market_prices_daily", symbols)) | {_daily_cutoff(as_of)}
    )
    monthly_cutoffs = sorted(
        set(_distinct_trading_dates(connection, "staging.market_prices_monthly", symbols)) | {_monthly_cutoff(as_of)}
    )

    daily_mask = compute_and_persist_universe_mask_from_db(
        connection,
        symbols=symbols,
        cutoff_dates=daily_cutoffs,
        source_table="staging.market_prices_daily",
        resolution="1D",
    )
    monthly_mask = compute_and_persist_universe_mask_from_db(
        connection,
        symbols=symbols,
        cutoff_dates=monthly_cutoffs,
        source_table="staging.market_prices_monthly",
        resolution="1M",
    )
    context.log.info(
        f"universe_mask: recomputed {len(daily_mask)} daily (over {len(daily_cutoffs)} cutoffs) + "
        f"{len(monthly_mask)} monthly (over {len(monthly_cutoffs)} cutoffs) records"
    )
    return {
        "daily_inserted": summary.daily_inserted,
        "monthly_inserted": summary.monthly_inserted,
        "daily_mask_rows": len(daily_mask),
        "monthly_mask_rows": len(monthly_mask),
        "daily_cutoffs": daily_cutoffs,
        "monthly_cutoffs": monthly_cutoffs,
    }


@dg.op
def refresh_market_data_op(context: dg.OpExecutionContext) -> None:
    """Ingest OHLCV bars for the TOPT universe, then recompute the PIT universe mask over
    every cutoff each resolution's table holds."""
    from data_engine.datahub.market_prices import DEFAULT_TOPT_SYMBOLS

    as_of = datetime.now(UTC).date()
    with (
        gateway.run_scope(f"dagster:{context.run_id}"),
        gateway.capacity_scope(),
        psycopg.connect(settings.database_url) as connection,
    ):
        _refresh_market_data(context, connection, symbols=DEFAULT_TOPT_SYMBOLS, as_of=as_of)
        connection.commit()


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
