"""#938 layer 1 acceptance: `staging.market_prices_daily`/`_monthly` are append-only.

The draft #101 branch's insert functions used `on conflict (symbol, date) do update`:
a re-ingested date silently replaced the prior row's OHLCV values in place, which is
exactly what this repository's PIT red line forbids ("Never overwrite a point-in-time
record. Restatements insert new rows..."). It also erases the very audit trail a
backtest's `no-lookahead` property depends on -- there is no way to ask "what did we
know about this date before the value changed."
"""

from __future__ import annotations

import os
from datetime import date

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


def _bar(symbol: str, d: date, close) -> PriceBarRecord:
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


def test_price_upsert_never_overwrites_prior_vintage(connection) -> None:
    """The defect this pins (#938 contract item 1): a second ingest for the same
    (symbol, trading_date) with a DIFFERENT close must add a new vintage row and leave
    the first row's close untouched -- never update it in place."""
    symbol = "T938TWO"
    trading_date = date(2026, 1, 15)

    inserted_first = insert_market_prices_daily(connection, [_bar(symbol, trading_date, 100)])
    inserted_second = insert_market_prices_daily(connection, [_bar(symbol, trading_date, 999)])

    assert inserted_first == 1
    assert inserted_second == 1

    with connection.cursor() as cur:
        cur.execute(
            "select close from staging.market_prices_daily "
            "where symbol = %s and trading_date = %s order by recorded_at asc",
            (symbol, trading_date),
        )
        closes = [row[0] for row in cur.fetchall()]

    assert len(closes) == 2, f"expected both vintages to persist as separate rows, got {closes}"
    assert closes[0] == 100, f"the first vintage's close was overwritten in place: {closes}"
    assert closes[1] == 999


def test_price_upsert_is_a_no_op_for_an_unchanged_revisit(connection) -> None:
    """Append-only means every GENUINE change gets its own row -- not that every
    re-fetch of unchanged history piles up an identical row (this pipeline refetches its
    full lookback window on every scheduled run)."""
    symbol = "T938THREE"
    trading_date = date(2026, 1, 16)

    insert_market_prices_daily(connection, [_bar(symbol, trading_date, 100)])
    second = insert_market_prices_daily(connection, [_bar(symbol, trading_date, 100)])

    assert second == 0

    with connection.cursor() as cur:
        cur.execute(
            "select count(*) from staging.market_prices_daily where symbol = %s and trading_date = %s",
            (symbol, trading_date),
        )
        (count,) = cur.fetchone()

    assert count == 1


def test_market_prices_daily_rejects_in_place_mutation(connection) -> None:
    """The append-only trigger is the backstop even if a future caller reaches for
    `UPDATE`/`DELETE` directly instead of going through `insert_market_prices_daily`."""
    symbol = "T938FOUR"
    trading_date = date(2026, 1, 17)
    insert_market_prices_daily(connection, [_bar(symbol, trading_date, 100)])

    with pytest.raises(psycopg.errors.RaiseException):
        with connection.cursor() as cur:
            cur.execute(
                "update staging.market_prices_daily set close = 1 where symbol = %s and trading_date = %s",
                (symbol, trading_date),
            )
    connection.rollback()
