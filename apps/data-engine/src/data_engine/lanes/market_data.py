"""Daily market data refresh (#938 layer 1)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import dagster as dg
import psycopg

from data_engine import db
from data_engine.config import settings
from data_engine.datahub.market_prices import (
    MAX_BAR_LAG_SESSIONS,
    SPLIT_ADJUSTED,
    UNADJUSTED,
    most_recent_xnys_session,
    oldest_session_within_lag,
    xnys_session_close_utc,
)
from data_engine.external_op import external_op
from data_engine.lanes.backtest import schedule_status
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_of, verdict
from data_engine.sources import gateway

if TYPE_CHECKING:
    from data_engine.datahub.market_prices import TwelveDataClient

MARKET_DATA_REFRESH_JOB_NAME = "market_data_refresh_pipeline"
MARKET_DATA_REFRESH_CRON = "15 21 * * 1-5"

MARKET_DATA_VERDICT = "market_data_freshness"
NIGHTLY_VERDICTS: tuple[str, ...] = (MARKET_DATA_VERDICT,)
FRESHNESS_SERIES: tuple[str, ...] = (UNADJUSTED, SPLIT_ADJUSTED)


def _daily_cutoff(now: datetime) -> date:
    today = now.date()
    candidate = most_recent_xnys_session(today)
    if candidate == today and now < xnys_session_close_utc(candidate):
        return most_recent_xnys_session(today - timedelta(days=1))
    return candidate


def _distinct_trading_dates(
    connection: psycopg.Connection, table: str, symbols: Sequence[str], *, adjust: str
) -> list[date]:
    query = f"select distinct trading_date from {table} where symbol = any(%s) and adjust = %s order by trading_date;"
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), adjust))
        return [row[0] for row in cur.fetchall()]


def _refresh_market_data(
    context: dg.OpExecutionContext,
    connection: psycopg.Connection,
    *,
    symbols: Sequence[str],
    now: datetime,
    client: "TwelveDataClient | None" = None,
) -> dict[str, Any]:
    from data_engine.datahub.market_prices import ingest_twelve_data_market_prices

    symbols = tuple(symbols)
    if not symbols:
        raise ValueError("market data: the universe record names no symbol to store")

    summary = ingest_twelve_data_market_prices(symbols=symbols, client=client, connection=connection, now=now)
    context.log.info(
        f"market_data: ingested {summary.daily_inserted} daily / {summary.unadjusted_daily_inserted} unadjusted "
        f"daily new-vintage rows over {summary.total_calls} calls"
    )

    daily_cutoffs = sorted(
        set(_distinct_trading_dates(connection, "staging.market_prices_daily", symbols, adjust=SPLIT_ADJUSTED))
        | {_daily_cutoff(now)}
    )
    return {
        "daily_inserted": summary.daily_inserted,
        "unadjusted_daily_inserted": summary.unadjusted_daily_inserted,
        "daily_cutoffs": daily_cutoffs,
    }


@dataclass(frozen=True)
class BarFreshness:
    ok: bool
    summary: str


def _newest_trading_date(connection: psycopg.Connection, symbols: Sequence[str], *, adjust: str) -> date | None:
    if not symbols:
        return None
    query = (
        "select symbol, max(trading_date) from staging.market_prices_daily "
        "where symbol = any(%s) and adjust = %s group by symbol;"
    )
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), adjust))
        rows = cur.fetchall()
    newest_by_symbol = {str(sym): dt for sym, dt in rows if dt is not None}
    if any(sym not in newest_by_symbol for sym in symbols):
        return None
    return min(newest_by_symbol[sym] for sym in symbols)


def judge_bar_freshness(connection: psycopg.Connection, symbols: Sequence[str], *, now: datetime) -> BarFreshness:
    latest_closed = _daily_cutoff(now)
    oldest_fresh = oldest_session_within_lag(latest_closed)
    problems: list[str] = []
    newest_by_series: list[str] = []
    for adjust in FRESHNESS_SERIES:
        newest = _newest_trading_date(connection, symbols, adjust=adjust)
        if newest is None:
            problems.append(f"{adjust}: no bar")
        elif newest < oldest_fresh:
            problems.append(
                f"{adjust}: newest bar {newest.isoformat()} is more than {MAX_BAR_LAG_SESSIONS} "
                f"session behind {latest_closed.isoformat()}"
            )
        else:
            newest_by_series.append(f"{adjust} {newest.isoformat()}")
    if problems:
        return BarFreshness(ok=False, summary="; ".join(problems))
    return BarFreshness(
        ok=True,
        summary=f"newest daily bars: {', '.join(newest_by_series)}; latest closed session {latest_closed.isoformat()}",
    )


@external_op
def refresh_market_data_op(context: dg.OpExecutionContext) -> None:
    from data_engine.datahub.market_prices import stored_symbols

    with verdict(
        MARKET_DATA_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        now = datetime.now(UTC)
        with gateway.capacity_scope(), db.connect() as connection:
            symbols = stored_symbols(connection, known_at=now)
            _refresh_market_data(context, connection, symbols=symbols, now=now)
            connection.commit()
            freshness = judge_bar_freshness(connection, symbols, now=now)
        outcome.summary = freshness.summary
        if not freshness.ok:
            outcome.failed = True
            context.log.warning("market data freshness (fact): %s", freshness.summary)


@dg.job(name=MARKET_DATA_REFRESH_JOB_NAME)
def market_data_refresh_pipeline_job() -> None:
    refresh_market_data_op()


@dg.schedule(
    job=market_data_refresh_pipeline_job,
    cron_schedule=MARKET_DATA_REFRESH_CRON,
    execution_timezone="UTC",
    default_status=schedule_status(settings.environment),
)
def market_data_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


defs = dg.Definitions(jobs=[market_data_refresh_pipeline_job], schedules=[market_data_refresh_schedule])
