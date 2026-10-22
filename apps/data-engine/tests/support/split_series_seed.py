"""Test support: the stored daily series that a live tick reads its splits from (#1262).

The live strategy seed puts a filed share count on the share basis of the captured close. It
takes the splits from `staging.market_prices_daily`. For each symbol, the market-data lane stores
an unadjusted series (`adjust = 'none'`) and a split-adjusted series (`adjust = 'splits'`).
A tick whose symbols have no stored series makes each issuer unavailable, by design. So a test
that runs the strategy seed on a capture stores both series first, through the production writer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from data_engine.datahub.market_prices import (
    DEFAULT_TOPT_SYMBOLS,
    PriceBarRecord,
    insert_market_prices_daily,
    is_xnys_trading_day,
)
from data_engine.datahub.share_basis import SplitHistory

#: The (unadjusted close, split-adjusted close) of one symbol on one session. None stores no bar.
SeriesShape = Callable[[date], tuple[Decimal | None, Decimal | None]]


def sessions(first: date, last: date) -> list[date]:
    """The XNYS sessions from `first` to `last`, both included."""
    return [
        first + timedelta(days=n)
        for n in range((last - first).days + 1)
        if is_xnys_trading_day(first + timedelta(days=n))
    ]


def store_daily_series(connection: Any, symbol: str, shape: SeriesShape, *, first: date, last: date) -> None:
    """Both daily series of one symbol, one bar per session, each bar at its close."""
    bars: dict[str, list[PriceBarRecord]] = {"none": [], "splits": []}
    for day in sessions(first, last):
        for adjust, close in zip(("none", "splits"), shape(day), strict=True):
            if close is not None:
                bars[adjust].append(PriceBarRecord(symbol, day, close, close, close, close, Decimal(1000)))
    for adjust, records in bars.items():
        insert_market_prices_daily(connection, records, adjust=adjust)


def store_flat_daily_series(
    connection: Any, symbols: Sequence[str], *, first: date, last: date, close: Decimal = Decimal("40")
) -> None:
    """Both daily series of each symbol at one close: the series show no split."""
    for symbol in symbols:
        store_daily_series(connection, symbol, lambda _day: (close, close), first=first, last=last)


def store_flat_topt_series(connection: Any, *, last: date, first: date = date(2026, 1, 2)) -> None:
    """Flat series of every TOPT symbol in one open transaction, for a test that freezes a core snapshot.

    The member builder never keeps a filed count without series that prove the splits (#1277). A test
    that expects an available core result stores the series first. `last` is on or after the close session.
    """
    store_flat_daily_series(connection, DEFAULT_TOPT_SYMBOLS, first=first, last=last)


def store_topt_series(database_url: str, *, first: date = date(2025, 12, 1), last: date = date(2026, 9, 30)) -> None:
    """Flat series of every symbol the market-data lane stores, committed to one test database.

    The default span holds every settled session and every share-count date the tick tests use.
    """
    with psycopg.connect(database_url) as connection:
        store_flat_daily_series(connection, DEFAULT_TOPT_SYMBOLS, first=first, last=last)
        connection.commit()


def share_count_vintage(*, filed: date, own_date: date, accession: str = "0000100000-26-000001") -> dict[str, Any]:
    """The vintage entry of a filed share count, in the shape `sec_financial_adapter._vintage` writes."""
    return {
        "accession": accession,
        "document": None,
        "filed": filed.isoformat(),
        "form": "10-K",
        "fp": "FY",
        "fy": filed.year - 1,
        "period_end": own_date.isoformat(),
        "statement_form": "10-K",
    }


def flat_history(symbol: str = "TEST", *, close: Decimal = Decimal("40")) -> SplitHistory:
    """A split history that proves no split in any window a test uses (2000 to 2100).

    For a test that builds a snapshot member and asks nothing about shares. The member builder
    never keeps a filed count without a history, so such a test names its history.
    """
    return SplitHistory(symbol, (), date(2000, 1, 1), date(2100, 1, 1), close)
