from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _bar(symbol: str, d: date, close: float) -> PriceBarRecord:
    return PriceBarRecord(
        symbol=symbol,
        date=d,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
        source="twelvedata",
        resolution="1D",
    )


def test_load_prices_resolves_duplicate_vintages_to_the_latest(connection) -> None:
    """#1030: staging.market_prices_daily is append-only with no unique constraint on
    (symbol, trading_date) -- a re-fetch under a new adjust policy (split/dividend
    recompute) lands a second vintage row, never replaces the first
    (test_price_upsert_never_overwrites_prior_vintage in test_market_prices.py proves
    this for the write side). The old _load_prices had no `distinct on` and returned
    every vintage undifferentiated; VectorBT's pivot_to_vbt_matrices calls pandas'
    `.pivot()` (not `.pivot_table()`), which raises ValueError on a duplicate
    (date, symbol) pair -- this script could not run at all against a symbol with any
    real revision history. `distinct on (symbol, trading_date) ... order by ...,
    recorded_at desc` resolves to the latest known vintage deterministically, the same
    resolution market_prices.py's own `_latest_vintages` already uses on the write side.

    Reverse-verified: reverting the `distinct on`/`recorded_at desc` ordering in
    `_load_prices` back to the plain `order by trading_date asc` this replaces makes
    this test fail (two rows returned for one (symbol, date), or downstream pivot
    raises) -- confirmed by hand before landing this test, per this repo's rule 7."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from run_vectorbt_backtest import _load_prices

    symbol = "T1030DUPVINTAGE"
    trading_date = date(2026, 3, 10)
    inserted_first = insert_market_prices_daily(connection, [_bar(symbol, trading_date, 100)])
    inserted_second = insert_market_prices_daily(connection, [_bar(symbol, trading_date, 999)])
    assert (inserted_first, inserted_second) == (1, 1), "setup must land two distinct vintages"

    # The exact precondition that crashed the old query: two real rows for one (symbol,
    # trading_date), because insert_market_prices_daily never overwrites in place.
    with connection.cursor() as cur:
        cur.execute(
            "select count(*) from staging.market_prices_daily where symbol = %s and trading_date = %s",
            (symbol, trading_date),
        )
        (vintage_count,) = cur.fetchone()
    assert vintage_count == 2, f"test setup didn't produce two vintages: {vintage_count}"

    prices = _load_prices(connection, "staging.market_prices_daily", [symbol])
    rows = prices.filter(prices["symbol"] == symbol).to_dicts()

    assert len(rows) == 1, f"expected exactly one resolved row per (symbol, date), got {rows}"
    assert rows[0]["close"] == 999.0, f"expected the latest-known vintage (999), got {rows}"


def test_load_prices_query_uses_trading_date_alias() -> None:
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from run_vectorbt_backtest import _load_prices

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = []

    _load_prices(mock_conn, "staging.market_prices_daily", ["AAPL"])

    mock_cur.execute.assert_called_once()
    sql_executed = mock_cur.execute.call_args[0][0]

    # Must select trading_date as date instead of date
    assert "trading_date as date" in sql_executed
    assert "select symbol, date, close" not in sql_executed
