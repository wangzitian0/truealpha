"""#938 layer 1 acceptance: `staging.universe_mask` keyed by (symbol, cutoff_date,
resolution).

The draft #101 branch's primary key was (symbol, cutoff_date): the lane computes 1D and
1M mask rows for the SAME `as_of` cutoff, so the second write silently overwrote the
first's row and one resolution's eligibility verdict was simply gone. This module's own
`evaluate_symbol_pit`/`compute_universe_mask` logic (reused verbatim from the draft) is
correct; the defect was that `UniverseMaskRecord` dropped the `resolution` it was
evaluated with before persistence ever saw it.
"""

from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily, insert_market_prices_monthly
from data_engine.datahub.universe_mask import (
    UniverseMaskReason,
    compute_and_persist_universe_mask_from_db,
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


def _bar(symbol: str, d: date, resolution: str) -> PriceBarRecord:
    return PriceBarRecord(
        symbol=symbol,
        date=d,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1000,
        source="twelvedata",
        resolution=resolution,
    )


def test_1d_and_1m_masks_do_not_collide(connection) -> None:
    """The defect this pins (#938 contract item 2): writing a 1D mask row and a 1M mask
    row for the SAME (symbol, cutoff_date) must leave both rows behind, not have the
    second overwrite the first."""
    symbol = "T938ONE"
    cutoff = date(2026, 3, 31)
    daily_dates = [date(2026, 3, d) for d in range(2, 32) if date(2026, 3, d).weekday() < 5][-5:]
    monthly_dates = [date(2025, m, 28) for m in range(1, 13)] + [cutoff]

    insert_market_prices_daily(connection, [_bar(symbol, d, "1D") for d in daily_dates])
    insert_market_prices_monthly(connection, [_bar(symbol, d, "1M") for d in monthly_dates])

    compute_and_persist_universe_mask_from_db(
        connection,
        symbols=[symbol],
        cutoff_dates=[cutoff],
        source_table="staging.market_prices_daily",
        resolution="1D",
        min_periods=1,
    )
    compute_and_persist_universe_mask_from_db(
        connection,
        symbols=[symbol],
        cutoff_dates=[cutoff],
        source_table="staging.market_prices_monthly",
        resolution="1M",
        min_periods=1,
    )

    with connection.cursor() as cur:
        cur.execute(
            "select resolution, eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s order by resolution",
            (symbol, cutoff),
        )
        rows = cur.fetchall()

    assert {r[0] for r in rows} == {"1D", "1M"}, (
        f"expected one row per resolution for the same cutoff, got {rows} "
        "-- the second write overwrote the first (missing `resolution` in the primary key)"
    )
    assert all(r[1] is True and r[2] == UniverseMaskReason.OK for r in rows), rows
