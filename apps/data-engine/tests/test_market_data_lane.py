"""#938 layer 1 acceptance: the market data lane's monthly cutoff is the month's own
last XNYS session, not the raw wall-clock `as_of` date -- and (#939 audit) ingesting a
still-open month's in-progress bar must not crash the whole op.

The draft #101 branch's op passed `cutoff_dates=[as_of]` straight through for BOTH
resolutions. `parse_monthly_bars` already snaps every CLOSED monthly bar to the month's
last XNYS session, so on every day of the month except that one session, `as_of` itself
mismatched the bar `evaluate_symbol_pit` needed to see -- `has_current_bar` came back
False and the whole universe read `suspended` on ~20 of every 21 trading days.

The fixture below is deliberately realistic about what Twelve Data actually returns for
a month that has not closed yet: a "month to date" row dated on the latest available
trading day, not the month's end. The first version of this test instead fed
`_monthly_session_dates` a count that included the CURRENT month and pre-snapped every
entry, including that one, to its month-end -- which happens to be exactly the shape
`parse_monthly_bars` needs to see to trigger the #939 High finding, but the test never
exercised it, because pre-snapped fixture data never goes through the snapping logic
that produces a future date in the first place.

`as_of` below is `date.today()`, not a fixed calendar date, and that is deliberate, not
sloppy: the invariant this test guards -- `staging.market_prices_monthly`'s
`check (recorded_at >= transaction_time)` -- is checked by Postgres against its own
`clock_timestamp()`, which no Python-level `as_of` argument can influence. A fixed
historical `as_of` (the #939 audit's finding: this file's first version hardcoded
`date(2026, 3, 15)`) makes the derived `transaction_time` historical relative to the
REAL server clock too, so the CHECK never fires regardless of whether the fix is
present -- the assertion passes by accident, not by proof. Anchoring to the real
"today" is the only way to make "this month is still open" true from Postgres's own
point of view on every day this test actually runs.

`_refresh_market_data` takes `now: datetime` (an instant), not `as_of: date`, as of the
#939 follow-up fix: `parse_monthly_bars`'s closed-vs-still-open check needed
time-of-day precision a bare `date` cannot carry (see `market_prices.py`). The test
below derives `now` from its own `as_of` (any instant on that calendar day, since the
guard already keeps `as_of` off the one day where the time of day would matter);
`test_refresh_market_data_op_does_not_crash_on_the_months_own_close_day_before_close`
below constructs THAT exact boundary directly instead -- a synthetic `now` a few
minutes before a computed month-end's own close, independent of real wall-clock time
entirely, because a `date.today()`-based fixture can only ever land on that one day
about once every 21 runs, and #939's second-round (blind) audit found that the first
version of this file avoided it outright rather than ever exercising it.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, date, datetime, timedelta

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import TwelveDataClient, last_xnys_session_of_month, xnys_session_close_utc
from data_engine.datahub.universe_mask import UniverseMaskReason
from data_engine.lanes.market_data import _daily_cutoff, _monthly_cutoff, _refresh_market_data


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


def _prior_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def test_refresh_market_data_op_mid_month_writes_ok_not_suspended(connection) -> None:
    symbol = "T938MID"
    as_of = date.today()
    in_progress_month_end = last_xnys_session_of_month(as_of.year, as_of.month)
    if as_of == in_progress_month_end:
        # The ~1-in-21 day this test happens to run ON the current month's own last
        # session: "this month" is not "in progress" from Postgres's clock either (its
        # close has genuinely already happened by the time the op runs), so the bar this
        # test means to exercise as in-progress would be a real, closed bar instead.
        # Stepping back a day keeps the scenario -- and the whole suite -- deterministic
        # without ever depending on wall-clock luck for whether it proves anything.
        as_of -= timedelta(days=1)
    last_closed_year, last_closed_month = _prior_month(as_of.year, as_of.month)
    last_closed_month_end = last_xnys_session_of_month(last_closed_year, last_closed_month)
    assert as_of != last_closed_month_end, "fixture picked a date that IS the month-end session; pick another"

    # 12 consecutive CLOSED month-end sessions ending at LAST month (>= default
    # min_periods=12), PLUS one realistic in-progress row for THIS (real, still-open as
    # of the real wall clock) month: Twelve Data's own "month to date" datetime for a
    # still-open period, NOT pre-snapped to this month's (not-yet-real) month-end. This
    # is the #939 regression fixture.
    monthly_dates = _monthly_session_dates(last_closed_year, last_closed_month, 12) + [as_of]
    client = _fake_client(monthly_dates)

    context = dg.build_op_context()
    # Must not raise: pre-#939-fix, the in-progress row above snapped forward to
    # `in_progress_month_end` (a date that has not happened yet as of `as_of`),
    # `staging.market_prices_monthly`'s `check (recorded_at >= transaction_time)` raised
    # CheckViolation on it, and the whole op -- daily insert and mask backfill included
    # -- rolled back with it. Any instant on `as_of`'s own calendar day works here: this
    # test's `as_of` is guaranteed (by the guard above) to NOT be the current month's own
    # closing session, so the exact time of day is not what this test is about -- see
    # `test_refresh_market_data_op_does_not_crash_on_the_months_own_close_day_before_close`
    # below for that boundary.
    now = datetime(as_of.year, as_of.month, as_of.day, 12, 0, tzinfo=UTC)
    _refresh_market_data(context, connection, symbols=[symbol], now=now, client=client)

    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, last_closed_month_end),
        )
        row = cur.fetchone()

    assert row is not None, (
        f"no 1M mask row at the last CLOSED month's cutoff {last_closed_month_end}; the "
        "lane must snap the cutoff to a real month's last XNYS session, not use the raw "
        "as_of date"
    )
    eligible, reason_code = row
    assert (eligible, reason_code) == (True, UniverseMaskReason.OK), (
        f"expected the mid-month run to mark {symbol} eligible at {last_closed_month_end}, "
        f"got eligible={eligible} reason_code={reason_code!r} -- a raw as_of cutoff "
        "mismatches the month-end-snapped bar and reads the universe as suspended"
    )

    # The raw mid-month `as_of` date itself must NOT be the cutoff a row was written
    # under -- that would mean the bug is still there, just also writing the right one.
    with connection.cursor() as cur:
        cur.execute(
            "select 1 from staging.universe_mask where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, as_of),
        )
        assert cur.fetchone() is None, f"unexpected 1M mask row written at the raw as_of date {as_of}"

    # And the in-progress row for THIS month must never have been asserted as a real
    # bar: no row at this month's (not-yet-real) month-end, under either its raw or its
    # snapped date.
    with connection.cursor() as cur:
        cur.execute(
            "select trading_date from staging.market_prices_monthly where symbol = %s",
            (symbol,),
        )
        monthly_dates_written = {row[0] for row in cur.fetchall()}
    assert in_progress_month_end not in monthly_dates_written, (
        f"a monthly bar was written at {in_progress_month_end}, which has not happened yet "
        f"as of {as_of} -- the in-progress month's row must be skipped, not asserted"
    )

    # #939 third-round High 1: the defect wasn't eliminated by the follow-up fix, only
    # moved -- `_monthly_cutoff` used to return `in_progress_month_end` UNCONDITIONALLY,
    # so this exact cutoff got a `suspended` mask row for the whole universe on every
    # day of the month except the one it closes on (the same #932/#938 shape, just keyed
    # to a different cutoff). The prior version of this test never queried this table at
    # this cutoff at all -- checking only that the price table had no bar -- so it never
    # could have caught this. There must be NO row here: an absent row is fail-closed
    # ("no verdict yet"), a `suspended` row would be a false, asserted-with-confidence
    # verdict about a month that has not happened yet.
    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, in_progress_month_end),
        )
        in_progress_mask_row = cur.fetchone()
    assert in_progress_mask_row is None, (
        f"unexpected 1M mask row at the not-yet-real cutoff {in_progress_month_end}: "
        f"{in_progress_mask_row} -- the cutoff for a month that has not closed must not "
        "be asserted at all, whole-universe-suspended or otherwise"
    )


def test_refresh_market_data_op_does_not_crash_on_the_months_own_close_day_before_close(connection) -> None:
    """#939 follow-up High: a blind re-audit of the first fix (`snapped_date > as_of_date`,
    two `date`s) found the exact boundary the test above structurally could never hit --
    its `as_of = date.today()` guard steps back a day whenever today happens to BE the
    current month's own last session, so across 365 days a year it never once lands on
    that exact day. `now` here is fully synthetic, built from a computed month-end, not
    real time -- this reproduces the regression deterministically on any day this test
    is actually run, which is the point: "green today" must not be able to mean
    "the bug isn't there" when it could just as easily mean "the test structurally
    cannot land on the one day that would show it".
    """
    symbol = "T939BOUNDARY"
    month_end = last_xnys_session_of_month(2026, 9)  # 2026-09-30: a real XNYS session
    close_instant = xnys_session_close_utc(month_end)
    now = close_instant - timedelta(minutes=5)  # this month's own last session, before its close

    last_closed_year, last_closed_month = _prior_month(2026, 9)
    last_closed_month_end = last_xnys_session_of_month(last_closed_year, last_closed_month)

    # 12 closed month-end sessions, PLUS the boundary row itself: Twelve Data's "month
    # to date" datetime for THIS month, dated on the month's own last session -- the
    # exact case a `snapped_date > as_of_date` (date-only) comparison cannot tell apart
    # from an already-closed bar, because the two dates are EQUAL, not `>`.
    monthly_dates = _monthly_session_dates(last_closed_year, last_closed_month, 12) + [month_end]
    client = _fake_client(monthly_dates)

    context = dg.build_op_context()
    # Must not raise: pre-follow-up-fix, this row's transaction_time (month_end's own
    # 16:00 ET close) was still five minutes in the future relative to `now`, but
    # `snapped_date > as_of_date` compared `month_end > month_end` -- False -- and let
    # it through. staging.market_prices_monthly's CHECK then fired, and the whole op
    # (daily insert and mask backfill included) rolled back with it.
    _refresh_market_data(context, connection, symbols=[symbol], now=now, client=client)

    with connection.cursor() as cur:
        cur.execute(
            "select trading_date from staging.market_prices_monthly where symbol = %s",
            (symbol,),
        )
        monthly_dates_written = {row[0] for row in cur.fetchall()}
    assert month_end not in monthly_dates_written, (
        f"a monthly bar was written at {month_end}, {close_instant - now} before its own "
        "close -- the still-open boundary row must be skipped, not asserted"
    )

    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, last_closed_month_end),
        )
        row = cur.fetchone()
    assert row == (True, UniverseMaskReason.OK), (
        f"expected {symbol} eligible at the last CLOSED month {last_closed_month_end}, got {row}"
    )

    # #939 third-round High 1, at this exact deterministic boundary: no mask row at the
    # not-yet-real cutoff either. Pre-High-1-fix, `_monthly_cutoff` returned `month_end`
    # unconditionally and this row would read `suspended` for the whole universe.
    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1M'",
            (symbol, month_end),
        )
        in_progress_mask_row = cur.fetchone()
    assert in_progress_mask_row is None, (
        f"unexpected 1M mask row at the not-yet-real cutoff {month_end}: {in_progress_mask_row}"
    )


def test_monthly_cutoff_falls_back_to_the_prior_closed_month_before_this_months_close() -> None:
    """#939 third-round High 1, at the `_monthly_cutoff` unit level: unconditionally
    returning `last_xnys_session_of_month(now.year, now.month)` -- this month's own
    end -- is a FUTURE date on every instant before that session's own close. Fully
    synthetic `now`, independent of real wall-clock day."""
    month_end = last_xnys_session_of_month(2026, 9)
    close_instant = xnys_session_close_utc(month_end)
    now = close_instant - timedelta(minutes=5)

    last_closed_year, last_closed_month = _prior_month(2026, 9)
    expected = last_xnys_session_of_month(last_closed_year, last_closed_month)

    assert _monthly_cutoff(now) == expected


def test_monthly_cutoff_uses_this_month_right_after_its_close() -> None:
    month_end = last_xnys_session_of_month(2026, 9)
    close_instant = xnys_session_close_utc(month_end)
    now = close_instant + timedelta(minutes=5)

    assert _monthly_cutoff(now) == month_end


def test_daily_cutoff_falls_back_to_the_prior_closed_session_before_todays_close() -> None:
    """#939 third-round High 1's finding applied symmetrically to `_daily_cutoff`: once
    `parse_daily_bars` (third-round High 2) also declines to write a bar for a session
    that has not closed yet, a cutoff of TODAY before today's own close would reproduce
    the identical "cutoff points at a bar that was correctly never written" defect for
    1D that `_monthly_cutoff` had for 1M."""
    today = date(2026, 9, 22)  # a real XNYS trading day (Tuesday)
    close_instant = xnys_session_close_utc(today)
    now = close_instant - timedelta(minutes=5)

    assert _daily_cutoff(now) == date(2026, 9, 21)  # the prior trading day (Monday)


def test_daily_cutoff_uses_today_right_after_its_close() -> None:
    today = date(2026, 9, 22)
    close_instant = xnys_session_close_utc(today)
    now = close_instant + timedelta(minutes=5)

    assert _daily_cutoff(now) == today


def test_daily_cutoff_falls_back_across_a_weekend_before_mondays_close() -> None:
    """The fallback itself must land on a real trading day, not just "yesterday" --
    Monday's own close hasn't happened yet, and the weekend before it has no session at
    all, so the answer must be the prior Friday."""
    monday = date(2026, 9, 21)
    close_instant = xnys_session_close_utc(monday)
    now = close_instant - timedelta(minutes=5)

    assert _daily_cutoff(now) == date(2026, 9, 18)  # the prior Friday


def test_refresh_market_data_op_does_not_crash_on_todays_session_before_its_close(connection) -> None:
    """#939 third-round High 2: `parse_daily_bars` had NO closed-vs-still-open gate at
    all through two rounds of fixing the identical defect in the monthly parser, and no
    test -- red or green -- ever exercised it. `now` here is fully synthetic (a real
    trading day's own close, minus five minutes), independent of real wall-clock day.
    """
    symbol = "T939DAILYBOUNDARY"
    today = date(2026, 9, 22)
    close_instant = xnys_session_close_utc(today)
    now = close_instant - timedelta(minutes=5)  # today's own session, before its close
    prior_session = date(2026, 9, 21)

    # 12 consecutive closed trading-day bars (>= default min_periods=12) ending the day
    # before `today`, PLUS `today`'s own still-open "so far" row -- Twelve Data's actual
    # shape for `interval=1day` mid-session.
    daily_dates = [date(2026, 9, d) for d in range(4, 22) if date(2026, 9, d).weekday() < 5][-12:] + [today]

    def transport(url: str) -> tuple[int, bytes]:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if query.get("interval") == "1day":
            values = [
                {"datetime": d.isoformat(), "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1000"}
                for d in daily_dates
            ]
        else:
            values = []
        return 200, json.dumps({"values": values}).encode()

    client = TwelveDataClient(api_key="test", transport_fn=transport)
    context = dg.build_op_context()
    # Must not raise: pre-fix, `today`'s still-open row's transaction_time (today's own
    # 16:00 ET close) was still five minutes in the future relative to `now`, and
    # `parse_daily_bars` had no gate to skip it -- staging.market_prices_daily's CHECK
    # fired, and the whole op (monthly insert and both mask backfills included) rolled
    # back with it.
    _refresh_market_data(context, connection, symbols=[symbol], now=now, client=client)

    with connection.cursor() as cur:
        cur.execute(
            "select trading_date from staging.market_prices_daily where symbol = %s",
            (symbol,),
        )
        daily_dates_written = {row[0] for row in cur.fetchall()}
    assert today not in daily_dates_written, (
        f"a daily bar was written at {today}, {close_instant - now} before its own close "
        "-- the still-open boundary row must be skipped, not asserted"
    )

    # And no mask row at the not-yet-real cutoff `today` either (#939 High 1 applied to
    # 1D): `_daily_cutoff` must fall back to the prior CLOSED session.
    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1D'",
            (symbol, today),
        )
        today_mask_row = cur.fetchone()
    assert today_mask_row is None, f"unexpected 1D mask row at the not-yet-closed cutoff {today}: {today_mask_row}"

    with connection.cursor() as cur:
        cur.execute(
            "select eligible, reason_code from staging.universe_mask "
            "where symbol = %s and cutoff_date = %s and resolution = '1D'",
            (symbol, prior_session),
        )
        prior_mask_row = cur.fetchone()
    assert prior_mask_row == (True, UniverseMaskReason.OK), (
        f"expected {symbol} eligible at the last CLOSED session {prior_session}, got {prior_mask_row}"
    )
