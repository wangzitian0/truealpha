"""The two stored daily price series and the split events they show (#758, #1257).

`staging.market_prices_daily` holds an unadjusted series (`adjust = 'none'`) and a split-adjusted
series (`adjust = 'splits'`) for each symbol. Three readers need both. The backtest prices returns
on unadjusted bars with explicit splits. The history projector and the live strategy seed put an
as-filed share count on the share basis of the close it multiplies (#1257, #1262). All three
derive the split events here, so they cannot disagree on what a split is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from factors.backtest.corporate_actions import SeriesBar, SplitDerivationError, SplitEvent, derive_split_events

DailySeries = dict[str, dict[date, tuple[Decimal | None, Decimal | None]]]

_SERIES_SQL = """
select distinct on (symbol, trading_date) symbol, trading_date, open, close
from staging.market_prices_daily
where symbol = any(%s) and adjust = %s
order by symbol, trading_date, recorded_at desc
"""


def load_daily_series(connection: psycopg.Connection[Any], symbols: Sequence[str], *, adjust: str) -> DailySeries:
    """The latest vintage of each daily bar of one series, as (open, close) per symbol and session."""
    series: DailySeries = {}
    for symbol, trading_date, open_, close in connection.execute(_SERIES_SQL, (list(symbols), adjust)).fetchall():
        series.setdefault(symbol, {})[trading_date] = (open_, close)
    return series


def split_events_of(symbol: str, unadjusted: DailySeries, split_adjusted: DailySeries) -> tuple[SplitEvent, ...]:
    """The splits of one symbol. Raise ``SplitDerivationError`` when the two series disagree."""
    raw = unadjusted.get(symbol, {})
    adjusted = split_adjusted.get(symbol, {})
    if raw and not adjusted:
        raise SplitDerivationError(
            f"{symbol}: unadjusted bars without split-adjusted bars; its split events cannot be derived"
        )
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
    """The splits of every symbol. Raise ONE ``SplitDerivationError`` that names every symbol that fails.

    A run that stops on the first bad symbol hides the others until the next run; this one lists them all.
    """
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
