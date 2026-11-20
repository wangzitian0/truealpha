"""Daily price series and split-event derivation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from factors.backtest.corporate_actions import SeriesBar, SplitDerivationError, SplitEvent, derive_split_events

DailySeries = dict[str, dict[date, tuple[Decimal | None, Decimal | None]]]

_SERIES_SQL = (
    "select distinct on (symbol, trading_date) symbol, trading_date, open, close "
    "from staging.market_prices_daily where symbol = any(%s) and adjust = %s "
    "order by symbol, trading_date, (source = 'twelvedata') desc, recorded_at desc"
)
_SERIES_BY_SOURCE_SQL = (
    "select distinct on (symbol, trading_date) symbol, trading_date, open, close "
    "from staging.market_prices_daily where symbol = any(%s) and adjust = %s and source = %s "
    "order by symbol, trading_date, recorded_at desc"
)


def load_daily_series(
    connection: psycopg.Connection[Any], symbols: Sequence[str], *, adjust: str, source: str | None = None
) -> DailySeries:
    series: DailySeries = {}
    rows = (
        connection.execute(_SERIES_SQL, (list(symbols), adjust)).fetchall()
        if source is None
        else connection.execute(_SERIES_BY_SOURCE_SQL, (list(symbols), adjust, source)).fetchall()
    )
    for symbol, trading_date, open_, close in rows:
        series.setdefault(symbol, {})[trading_date] = (open_, close)
    return series


def split_events_of(symbol: str, unadjusted: DailySeries, split_adjusted: DailySeries) -> tuple[SplitEvent, ...]:
    raw, adjusted = unadjusted.get(symbol, {}), split_adjusted.get(symbol, {})
    if raw and not adjusted:
        raise SplitDerivationError(f"{symbol}: unadjusted bars without split-adjusted bars; splits cannot be derived")
    return derive_split_events(
        symbol,
        [
            SeriesBar(
                session=day,
                unadjusted_open=raw.get(day, (None, None))[0],
                unadjusted_close=raw.get(day, (None, None))[1],
                split_adjusted_close=adjusted.get(day, (None, None))[1],
            )
            for day in sorted(set(raw) | set(adjusted))
        ],
    )


def split_events_by_symbol(
    symbols: Sequence[str], unadjusted: DailySeries, split_adjusted: DailySeries
) -> dict[str, tuple[SplitEvent, ...]]:
    events: dict[str, tuple[SplitEvent, ...]] = {}
    failures: list[str] = []
    for symbol in sorted(set(symbols)):
        try:
            events[symbol] = split_events_of(symbol, unadjusted, split_adjusted)
        except SplitDerivationError as error:
            failures.append(str(error))
    if failures:
        raise SplitDerivationError(f"{len(failures)} symbols have no derivable split events: " + "; ".join(failures))
    return events


__all__ = ["DailySeries", "load_daily_series", "split_events_by_symbol", "split_events_of"]
