"""The two stored daily price series and the split events they show (#758, #1257).

`staging.market_prices_daily` holds an unadjusted series (`adjust = 'none'`) and a split-adjusted
series (`adjust = 'splits'`) for each symbol. Two readers need both: the backtest, to price
returns on unadjusted bars with explicit splits, and the history projector, to put an as-filed
share count on the share basis of the close it multiplies (#1257). Both read the series and derive
the split events here, so the two cannot disagree on what a split is.
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


__all__ = ["DailySeries", "load_daily_series", "split_events_of"]
