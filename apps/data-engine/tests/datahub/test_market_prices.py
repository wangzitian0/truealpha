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
from datetime import date, timedelta

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import (
    PriceBarRecord,
    insert_market_prices_daily,
    insert_market_prices_monthly,
    last_xnys_session_of_month,
    parse_monthly_bars,
)


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


@pytest.mark.parametrize(
    ("table", "insert_fn"),
    [
        ("staging.market_prices_daily", insert_market_prices_daily),
        ("staging.market_prices_monthly", insert_market_prices_monthly),
    ],
    ids=["daily", "monthly"],
)
@pytest.mark.parametrize("statement", ["update", "delete"])
def test_market_prices_rejects_in_place_mutation(connection, table, insert_fn, statement) -> None:
    """The append-only trigger is `BEFORE DELETE OR UPDATE` (#939 audit Low finding: the
    prior version of this test only ever sent UPDATE, and only against the daily table --
    DELETE and the monthly table were unguarded by any test). Both tables, both statements,
    must be rejected the same way."""
    symbol = f"T938FIVE-{table.split('.')[-1]}-{statement}"
    trading_date = date(2026, 1, 18)
    insert_fn(connection, [_bar(symbol, trading_date, 100)])

    sql = (
        f"update {table} set close = 1 where symbol = %s and trading_date = %s"
        if statement == "update"
        else f"delete from {table} where symbol = %s and trading_date = %s"
    )
    with pytest.raises(psycopg.errors.RaiseException):
        with connection.cursor() as cur:
            cur.execute(sql, (symbol, trading_date))
    connection.rollback()


def test_parse_monthly_bars_skips_the_in_progress_month() -> None:
    """#939 audit High finding: a monthly bar is asserted only once its month has
    CLOSED as of `as_of` -- wall-clock-independent by construction, since `as_of` is an
    explicit local value here, not `datetime.now()`, and this would fail identically no
    matter what day it is actually run.

    Twelve Data's `interval=1month` response, while the current month is still open,
    includes a running "month to date" row whose raw `datetime` is simply the latest
    available trading day (here, `as_of` itself) -- not a period close. The pre-fix
    behaviour snapped that date forward to the month's last XNYS session regardless,
    producing a `trading_date` (and therefore `transaction_time`) that has not happened
    yet: 2026-09-30 when `as_of` is 2026-09-22. `staging.market_prices_monthly`'s
    `check (recorded_at >= transaction_time)` then refuses the row -- correctly, since
    there genuinely is no September close to assert on the 22nd -- but because
    `lanes.market_data._refresh_market_data` writes the daily insert, the monthly
    insert, and the mask backfill on one uncommitted connection, that refusal used to
    roll back the whole op.
    """
    as_of = date(2026, 9, 22)
    closed_month_end = last_xnys_session_of_month(2026, 8)  # August already closed by Sep 22
    in_progress_raw_date = as_of - timedelta(days=2)  # a recent September trading day, unsnapped

    payload = {
        "values": [
            {
                "datetime": closed_month_end.isoformat(),
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1000",
            },
            {
                "datetime": in_progress_raw_date.isoformat(),
                "open": "110",
                "high": "111",
                "low": "109",
                "close": "110",
                "volume": "1100",
            },
        ]
    }

    bars = parse_monthly_bars("T939SIX", payload, as_of=as_of)

    dates = [b.date for b in bars]
    assert dates == [closed_month_end], (
        f"expected only the closed August bar, got {dates} -- the in-progress September "
        "row must be skipped, not snapped forward into a future trading_date"
    )
