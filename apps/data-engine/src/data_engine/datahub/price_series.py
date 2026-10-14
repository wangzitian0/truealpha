"""The two stored daily price series and the split events they show (#758, #1257).

`staging.market_prices_daily` holds an unadjusted series (`adjust = 'none'`) and a split-adjusted
series (`adjust = 'splits'`) for each symbol. Three readers need both. The backtest prices returns
on unadjusted bars with explicit splits. The history projector and the live strategy seed put an
as-filed share count on the share basis of the close it multiplies (#1257, #1262). All three
derive the split events here, so they cannot disagree on what a split is.

The series are keyed by symbol, and an issuer can have more than one listing. Alphabet is GOOG in
lineage, and the store holds GOOGL only (#1265). ``resolve_price_symbol`` is the one rule that
names the listing whose series price an issuer. The stored series decide it, not a list of names.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from factors.backtest.corporate_actions import SeriesBar, SplitDerivationError, SplitEvent, derive_split_events

from data_engine.datahub.market_prices import SPLIT_ADJUSTED, UNADJUSTED

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


# One stored bar is enough to hold a series. Each probe stops at the first bar of the symbol index.
_BOTH_SERIES_SQL = """
select candidate.symbol
from unnest(%s::text[]) as candidate (symbol)
where exists (select 1 from staging.market_prices_daily m where m.symbol = candidate.symbol and m.adjust = %s)
  and exists (select 1 from staging.market_prices_daily m where m.symbol = candidate.symbol and m.adjust = %s)
"""


def symbols_with_both_series(connection: psycopg.Connection[Any], symbols: Sequence[str]) -> frozenset[str]:
    """The symbols that hold a stored unadjusted series and a stored split-adjusted series."""
    rows = connection.execute(_BOTH_SERIES_SQL, (sorted(set(symbols)), UNADJUSTED, SPLIT_ADJUSTED)).fetchall()
    return frozenset(str(row[0]) for row in rows)


def resolve_price_symbol(ticker: str, listing_tickers: Iterable[str], priced: Collection[str]) -> str | None:
    """The listing ticker whose stored daily series price one issuer, or None (#1265).

    The candidates are the lineage ticker first, then the other listing tickers in sorted order.
    The first candidate in ``priced``, the symbols with both stored series, is the price symbol.
    None means that no listing of the issuer holds both series.
    """
    for candidate in (ticker, *sorted(set(listing_tickers) - {ticker})):
        if candidate in priced:
            return candidate
    return None


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


__all__ = [
    "DailySeries",
    "load_daily_series",
    "resolve_price_symbol",
    "split_events_by_symbol",
    "split_events_of",
    "symbols_with_both_series",
]
