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
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import (
    DEFAULT_TOPT_SYMBOLS,
    PriceBarRecord,
    TwelveDataApiError,
    TwelveDataClient,
    insert_market_prices_daily,
    last_xnys_session_of_month,
    xnys_session_close_utc,
)
from data_engine.datahub.production_topt.source_registrations import LEDGER_CAPACITIES, environment_share
from data_engine.datahub.universe_mask import UniverseMaskReason
from data_engine.lanes import market_data
from data_engine.lanes.market_data import _daily_cutoff, _monthly_cutoff, _refresh_market_data
from data_engine.quality import nightly_verdicts


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


def test_refresh_market_data_asks_twelve_data_for_the_canonical_ticker(connection) -> None:
    """#1060: Twelve Data lists Berkshire B as `BRK.B`. A `/` makes it read a currency pair and answer 404."""
    requested: list[dict[str, str]] = []

    def transport(url: str) -> tuple[int, bytes]:
        requested.append(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)))
        return 200, json.dumps({"values": []}).encode()

    client = TwelveDataClient(api_key="test", transport_fn=transport)
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    _refresh_market_data(dg.build_op_context(), connection, symbols=["BRK.B"], now=now, client=client)

    assert [(query["symbol"], query["interval"], query["adjust"]) for query in requested] == [
        ("BRK.B", "1day", "splits"),
        ("BRK.B", "1day", "none"),
        ("BRK.B", "1month", "splits"),
    ]


def test_refresh_market_data_fails_visibly_when_twelve_data_rejects_a_symbol(connection) -> None:
    """#1060: a symbol the vendor rejects must fail the run. The lane must not skip it."""

    def transport(url: str) -> tuple[int, bytes]:
        body = {"code": 404, "message": "symbol is missing or invalid", "status": "error"}
        return 404, json.dumps(body).encode()

    client = TwelveDataClient(api_key="test", transport_fn=transport)
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    with pytest.raises(TwelveDataApiError, match="404"):
        _refresh_market_data(dg.build_op_context(), connection, symbols=["NOSUCH"], now=now, client=client)


# --- #1131: split-adjusted and unadjusted daily bars ------------------------------------

SPLIT_CLOSE = "50"
UNADJUSTED_CLOSE = "500"
#: A Tuesday, after the 20:00 UTC close of its own session.
LANE_NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
LANE_SESSIONS = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)]


def _adjust_aware_client(requests: list[tuple[str, str, str]], sessions: list[date]) -> TwelveDataClient:
    """A fake vendor. A daily call answers one close per `adjust` value. Monthly calls answer nothing."""
    closes = {"splits": SPLIT_CLOSE, "none": UNADJUSTED_CLOSE}

    def transport(url: str) -> tuple[int, bytes]:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        requests.append((query["symbol"], query["interval"], query["adjust"]))
        values = []
        if query["interval"] == "1day":
            close = closes[query["adjust"]]
            values = [
                {
                    "datetime": d.isoformat(),
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": "1000",
                }
                for d in sessions
            ]
        return 200, json.dumps({"values": values}).encode()

    # The rate limiter never sleeps here: the test counts calls, it does not pace them.
    return TwelveDataClient(api_key="test", transport_fn=transport, sleep_fn=lambda _seconds: None)


def _write_newest_bars(connection, symbol: str, newest: dict[str, date]) -> None:
    """One bar per series, dated `newest[adjust]`."""
    for adjust, trading_date in newest.items():
        close = 50 if adjust == "splits" else 500
        insert_market_prices_daily(
            connection,
            [PriceBarRecord(symbol, trading_date, close, close, close, close, 1000, "twelvedata", "1D")],
            adjust=adjust,
        )


def test_refresh_market_data_ingests_both_adjust_values_with_one_fetch_each_per_symbol(connection) -> None:
    symbols = ["T1131LANEA", "T1131LANEB"]
    requests: list[tuple[str, str, str]] = []
    client = _adjust_aware_client(requests, LANE_SESSIONS)

    _refresh_market_data(dg.build_op_context(), connection, symbols=symbols, now=LANE_NOW, client=client)

    expected = Counter()
    for symbol in symbols:
        expected[(symbol, "1day", "splits")] = 1
        expected[(symbol, "1day", "none")] = 1
        expected[(symbol, "1month", "splits")] = 1
    assert Counter(requests) == expected, "each symbol needs one split-adjusted and one unadjusted daily fetch"

    for symbol in symbols:
        with connection.cursor() as cur:
            cur.execute(
                "select adjust, count(*), min(close), max(close), max(trading_date) "
                "from staging.market_prices_daily where symbol = %s group by adjust order by adjust",
                (symbol,),
            )
            rows = cur.fetchall()
        assert rows == [
            ("none", len(LANE_SESSIONS), 500, 500, LANE_SESSIONS[-1]),
            ("splits", len(LANE_SESSIONS), 50, 50, LANE_SESSIONS[-1]),
        ], rows


def test_refresh_market_data_takes_its_mask_cutoffs_from_the_split_adjusted_series(connection) -> None:
    """A date that only the unadjusted series holds is no cutoff of the split-adjusted mask."""
    symbol = "T1131CUTOFFS"
    split_dates = [date(2026, 9, 30), date(2026, 10, 1)]
    unadjusted_only = date(2026, 10, 2)
    _write_newest_bars(connection, symbol, {"splits": split_dates[0]})
    _write_newest_bars(connection, symbol, {"splits": split_dates[1]})
    _write_newest_bars(connection, symbol, {"none": unadjusted_only})
    client = _adjust_aware_client([], [])

    result = _refresh_market_data(dg.build_op_context(), connection, symbols=[symbol], now=LANE_NOW, client=client)

    assert result["daily_cutoffs"] == [*split_dates, _daily_cutoff(LANE_NOW)]
    assert unadjusted_only not in result["daily_cutoffs"]


def test_the_lane_fits_the_twelve_data_production_share(connection) -> None:
    """Rule 6 ratchet. The measured calls of one lane run, plus the busiest measured capture day, fit the daily share.

    152 is production's busiest capture day of 2026-09-08..15, as recorded beside
    `TWELVE_DATA_ENVIRONMENT_SHARES` in `source_registrations.py`.
    """
    measured_busiest_capture_day = 152
    requests: list[tuple[str, str, str]] = []
    client = _adjust_aware_client(requests, [])

    _refresh_market_data(dg.build_op_context(), connection, symbols=DEFAULT_TOPT_SYMBOLS, now=LANE_NOW, client=client)

    twelve = LEDGER_CAPACITIES["twelvedata"]
    assert twelve.daily_budget is not None
    production_budget = environment_share(twelve.daily_budget, twelve.environment_shares, "production")
    assert production_budget is not None
    assert len(requests) + measured_busiest_capture_day <= production_budget, (
        f"the lane makes {len(requests)} Twelve Data calls a run; production's daily share is {production_budget}"
    )


# --- #1131: the freshness verdict --------------------------------------------------------


def _both(trading_date: date) -> dict[str, date]:
    return {"splits": trading_date, "none": trading_date}


MONDAY_RUN = datetime(2026, 10, 5, 21, 15, tzinfo=UTC)  # the lane's cron tick, a Monday
FRIDAY = date(2026, 10, 2)


def test_freshness_is_green_when_the_newest_bar_is_the_latest_closed_session(connection) -> None:
    symbol = "T1131FRESH0"
    _write_newest_bars(connection, symbol, _both(date(2026, 10, 5)))

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=MONDAY_RUN)

    assert verdict.ok is True, verdict.summary


def test_freshness_is_green_over_a_normal_weekend_gap(connection) -> None:
    """Monday's tick runs before the vendor lists Monday's bar. Friday is one session behind and still on time."""
    symbol = "T1131WEEKEND"
    _write_newest_bars(connection, symbol, _both(FRIDAY))

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=MONDAY_RUN)

    assert verdict.ok is True, verdict.summary


def test_freshness_is_green_on_the_weekend_itself(connection) -> None:
    symbol = "T1131SUNDAY"
    _write_newest_bars(connection, symbol, _both(FRIDAY))

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=datetime(2026, 10, 4, 12, 0, tzinfo=UTC))

    assert verdict.ok is True, verdict.summary


def test_freshness_is_green_over_a_holiday_weekend(connection) -> None:
    """Labor Day is Monday 2026-09-07. Tuesday's tick finds Friday's bar. Monday was no session."""
    symbol = "T1131HOLIDAY"
    _write_newest_bars(connection, symbol, _both(date(2026, 9, 4)))

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=datetime(2026, 9, 8, 21, 15, tzinfo=UTC))

    assert verdict.ok is True, verdict.summary


def test_freshness_is_red_when_the_newest_bar_is_older_than_the_limit(connection) -> None:
    """Tuesday's tick with Friday's bar as the newest: Monday and Tuesday are missing, two sessions."""
    symbol = "T1131STALE"
    _write_newest_bars(connection, symbol, _both(FRIDAY))

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=datetime(2026, 10, 6, 21, 15, tzinfo=UTC))

    assert verdict.ok is False
    assert market_data.MAX_BAR_LAG_SESSIONS == 1
    assert "newest bar 2026-10-02 is more than 1 session behind 2026-10-06" in verdict.summary, verdict.summary


def test_freshness_is_red_when_one_series_is_stale(connection) -> None:
    """The unadjusted fetch can fail alone. The split-adjusted bars stay fresh and hide it."""
    symbol = "T1131ONESERIES"
    _write_newest_bars(connection, symbol, {"splits": date(2026, 10, 5), "none": date(2026, 9, 29)})

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=MONDAY_RUN)

    assert verdict.ok is False
    assert "none" in verdict.summary and "splits" not in verdict.summary, verdict.summary


def test_freshness_is_red_when_a_series_has_no_bar_at_all(connection) -> None:
    symbol = "T1131NOUNADJ"
    _write_newest_bars(connection, symbol, {"splits": date(2026, 10, 5)})

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=MONDAY_RUN)

    assert verdict.ok is False
    assert "none: no bar" in verdict.summary, verdict.summary


def test_freshness_reads_each_series_by_its_own_adjust_value(connection) -> None:
    """A fresh bar of the other series must not vouch for this one."""
    symbol = "T1131OTHERSERIES"
    _write_newest_bars(connection, symbol, {"splits": date(2026, 10, 5)})
    with connection.cursor() as cur:
        cur.execute("select count(*) from staging.market_prices_daily where symbol = %s and adjust = 'none'", (symbol,))
        assert cur.fetchone() == (0,)

    verdict = market_data.judge_bar_freshness(connection, [symbol], now=MONDAY_RUN)

    assert verdict.ok is False


# --- #1131: the op records the verdict ---------------------------------------------------

TICK = "2026-10-05T21:15:00+00:00"


class _Connection:
    """A database connection that counts commits. The op reads no table through it here."""

    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def written(monkeypatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **kwargs: rows.append({"check": name, **kwargs}))
    return rows


@pytest.fixture
def fake_connection(monkeypatch) -> _Connection:
    fake = _Connection()
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: fake)
    return fake


def _run_job() -> dg.ExecuteInProcessResult:
    return market_data.market_data_refresh_pipeline_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK}, raise_on_error=False
    )


def test_the_lane_declares_the_verdict_it_records() -> None:
    assert market_data.NIGHTLY_VERDICTS == ("market_data_freshness",)
    assert nightly_verdicts.is_valid_name("market_data_freshness")


def test_a_fresh_run_records_a_green_verdict_after_the_commit(monkeypatch, written, fake_connection) -> None:
    commits_when_judged: list[int] = []
    monkeypatch.setattr(market_data, "_refresh_market_data", lambda *_a, **_k: {})

    def judge(_connection: object, _symbols: object, *, now: datetime) -> market_data.BarFreshness:
        commits_when_judged.append(fake_connection.commits)
        return market_data.BarFreshness(ok=True, summary="newest bars 2026-10-05 (none, splits)")

    monkeypatch.setattr(market_data, "judge_bar_freshness", judge)

    assert _run_job().success
    ((row),) = written
    assert (row["check"], row["ok"], row["summary"]) == (
        "market_data_freshness",
        True,
        "newest bars 2026-10-05 (none, splits)",
    )
    assert row["ran_at"] == datetime.fromisoformat(TICK)
    assert commits_when_judged == [1], "the ingest must be committed before it is judged"


def test_a_stale_run_records_a_red_verdict_and_fails_the_run_but_keeps_the_ingest(
    monkeypatch, written, fake_connection
) -> None:
    monkeypatch.setattr(market_data, "_refresh_market_data", lambda *_a, **_k: {})
    monkeypatch.setattr(
        market_data,
        "judge_bar_freshness",
        lambda *_a, **_k: market_data.BarFreshness(ok=False, summary="none: newest bar 2026-10-02 is stale"),
    )

    result = _run_job()

    assert not result.success
    ((row),) = written
    assert (row["check"], row["ok"]) == ("market_data_freshness", False)
    assert row["summary"] == "failed: none: newest bar 2026-10-02 is stale"
    assert fake_connection.commits == 1, "a stale verdict must not roll back the bars this run ingested"


def test_a_crashing_ingest_records_a_red_verdict(monkeypatch, written, fake_connection) -> None:
    """The 2026-09-24..10-05 shape: the lane failed 8 times and no verdict row said so."""

    def crash(*_a: object, **_k: object) -> None:
        raise TwelveDataApiError("Twelve Data error 401: invalid key")

    monkeypatch.setattr(market_data, "_refresh_market_data", crash)

    result = _run_job()

    assert not result.success
    ((row),) = written
    assert (row["check"], row["ok"]) == ("market_data_freshness", False)
    assert row["summary"].startswith("failed: TwelveDataApiError")
    assert "invalid key" not in row["summary"], "an exception text is not published"


def test_the_schedule_stamps_its_tick_so_the_verdict_is_dated_by_it() -> None:
    tick = datetime(2026, 10, 5, 21, 15, tzinfo=UTC)

    result = market_data.market_data_refresh_schedule.evaluate_tick(
        dg.build_schedule_context(scheduled_execution_time=tick)
    )

    ((request),) = result.run_requests
    assert request.tags[nightly_verdicts.TICK_TAG] == tick.isoformat()
