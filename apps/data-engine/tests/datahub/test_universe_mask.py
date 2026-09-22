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
from datetime import date, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily, insert_market_prices_monthly
from data_engine.datahub.universe_mask import (
    UniverseMaskReason,
    compute_and_persist_universe_mask_from_db,
    evaluate_symbol_pit,
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


def test_extract_bar_date_normalizes_a_datetime_valued_bar_date() -> None:
    """#939 review Medium: `_extract_bar_date` returned a `datetime` unchanged whenever
    a bar's date-bearing attribute happened to hold one, because `isinstance(d, date)`
    is also true for `datetime` (a `date` subclass). `evaluate_symbol_pit` then compares
    that value against a plain `date` cutoff (`<= cutoff_date`), which raises
    `TypeError: can't compare datetime.datetime to datetime.date` -- nothing in this
    module's public contract (`PriceBarRecord`'s `date` field is untyped at runtime, and
    the module docstring advertises "bar object, dataclass, or dictionary" generically)
    rules out a caller handing it a `datetime`-valued bar."""
    symbol = "T938DTBAR"
    cutoff = date(2026, 3, 31)
    bar = PriceBarRecord(
        symbol=symbol,
        date=datetime(2026, 3, 31, 9, 30),  # a datetime, not a date -- the trigger shape
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1000,
        source="twelvedata",
        resolution="1M",
    )

    record = evaluate_symbol_pit(
        symbol=symbol,
        cutoff_date=cutoff,
        bars=[bar],
        min_periods=1,
        resolution="1M",
    )

    assert record.eligible is True
    assert record.reason_code == UniverseMaskReason.OK


def test_1m_eligibility_requires_the_exact_snapped_month_end_bar() -> None:
    """#939 review Medium: the 1M `has_current_bar` check accepted ANY bar in the same
    calendar year/month as `cutoff_date`, not just the actual snapped month-end XNYS
    session bar. A symbol suspended exactly on the month-end session (bar missing at the
    snapped cutoff) but still trading earlier in the same month was misread as eligible
    -- the opposite of contract item 1's fail-closed intent for a missing cutoff bar."""
    symbol = "T938MEND"
    cutoff = date(2026, 3, 31)  # the actual last XNYS session of March 2026
    early_march_bar = PriceBarRecord(
        symbol=symbol,
        date=date(2026, 3, 2),  # trades earlier in March, but NOT on the month-end session
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1000,
        source="twelvedata",
        resolution="1M",
    )

    record = evaluate_symbol_pit(
        symbol=symbol,
        cutoff_date=cutoff,
        bars=[early_march_bar],
        min_periods=1,
        resolution="1M",
    )

    assert record.eligible is False, (
        "a bar earlier in the month must not stand in for the missing month-end session bar"
    )
    assert record.reason_code == UniverseMaskReason.SUSPENDED
