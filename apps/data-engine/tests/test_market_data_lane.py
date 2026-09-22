"""#938 layer 1 acceptance: the market data lane's monthly cutoff is the month's own
last XNYS session, not the raw wall-clock `as_of` date.

The draft #101 branch's op passed `cutoff_dates=[as_of]` straight through for BOTH
resolutions. `parse_monthly_bars` already snaps every monthly bar to the month's last
XNYS session, so on every day of the month except that one session, `as_of` itself
mismatched the bar `evaluate_symbol_pit` needed to see -- `has_current_bar` came back
False and the whole universe read `suspended` on ~20 of every 21 trading days.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import date

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import TwelveDataClient, last_xnys_session_of_month
from data_engine.datahub.universe_mask import UniverseMaskReason
from data_engine.lanes.market_data import _refresh_market_data


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


def _monthly_session_dates(end_year: int, end_month: int, count: int) -> list[date]:
    """`count` consecutive month-end XNYS sessions ending at (end_year, end_month)."""
    dates: list[date] = []
    y, m = end_year, end_month
    for _ in range(count):
        dates.append(last_xnys_session_of_month(y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(dates))


def _fake_client(monthly_dates: list[date]) -> TwelveDataClient:
    """A Twelve Data client whose transport never touches the network: '1month' requests
    get `monthly_dates` back (as the vendor's own unsnapped datetimes -- snapping is
    `parse_monthly_bars`'s job, not the fixture's), '1day' requests get nothing (this
    test only asserts on the 1M mask row)."""

    def transport(url: str) -> tuple[int, bytes]:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if query.get("interval") == "1month":
            values = [
                {"datetime": d.isoformat(), "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1000"}
                for d in monthly_dates
            ]
        else:
            values = []
        return 200, json.dumps({"values": values}).encode()

    return TwelveDataClient(api_key="test", transport_fn=transport)


def test_refresh_market_data_op_mid_month_writes_ok_not_suspended(connection) -> None:
    symbol = "T938MID"
    as_of = date(2026, 3, 15)  # mid-month: not itself an XNYS month-end session
    month_end = last_xnys_session_of_month(2026, 3)
    assert as_of != month_end, "fixture picked a date that IS the month-end session; pick another"

    monthly_dates = _monthly_session_dates(2026, 3, 13)  # >= default min_periods=12
    client = _fake_client(monthly_dates)

    context = dg.build_op_context()
    _refresh_market_data(context, connection, symbols=[symbol], as_of=as_of, client=client)

    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, month_end),
        )
        row = cur.fetchone()

    assert row is not None, (
        f"no 1M mask row at the month-end cutoff {month_end}; the lane must snap the "
        "cutoff to the month's last XNYS session, not use the raw as_of date"
    )
    eligible, reason_code = row
    assert (eligible, reason_code) == (True, UniverseMaskReason.OK), (
        f"expected the mid-month run to mark {symbol} eligible at {month_end}, got "
        f"eligible={eligible} reason_code={reason_code!r} -- a raw as_of cutoff mismatches "
        "the month-end-snapped bar and reads the universe as suspended"
    )

    # And the raw mid-month `as_of` date itself must NOT be the cutoff a row was written
    # under -- that would mean the bug is still there, just also writing the right one.
    with connection.cursor() as cur:
        cur.execute(
            "select 1 from staging.universe_mask where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, as_of),
        )
        assert cur.fetchone() is None, f"unexpected 1M mask row written at the raw as_of date {as_of}"
