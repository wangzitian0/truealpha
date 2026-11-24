"""#938, #939, #1131: market data daily refresh and cutoff tests."""

from __future__ import annotations

import importlib
import json
import math
import os
import sys
import urllib.parse
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import (
    MAX_BAR_LAG_SESSIONS,
    PriceBarRecord,
    TwelveDataApiError,
    TwelveDataClient,
    insert_market_prices_daily,
    oldest_session_within_lag,
    xnys_session_close_utc,
)
from data_engine.lanes import backtest as backtest_lane
from data_engine.lanes import market_data
from data_engine.lanes.market_data import _daily_cutoff, _refresh_market_data
from data_engine.quality import nightly_verdicts
from data_engine.sources import gateway

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from topt_symbols import TOPT_SYMBOLS  # noqa: E402


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


def test_daily_cutoff_falls_back_to_the_prior_closed_session_before_todays_close() -> None:
    """`_daily_cutoff` falls back to prior closed session when today's close has not happened."""
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
    monday = date(2026, 9, 21)
    close_instant = xnys_session_close_utc(monday)
    now = close_instant - timedelta(minutes=5)

    assert _daily_cutoff(now) == date(2026, 9, 18)  # the prior Friday


def test_refresh_market_data_op_does_not_crash_on_todays_session_before_its_close(connection) -> None:
    """`parse_daily_bars` skips in-progress bars before today's session close."""
    symbol = "T939DAILYBOUNDARY"
    today = date(2026, 9, 22)
    close_instant = xnys_session_close_utc(today)
    now = close_instant - timedelta(minutes=5)
    prior_session = date(2026, 9, 21)

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
    result = _refresh_market_data(context, connection, symbols=[symbol], now=now, client=client)

    with connection.cursor() as cur:
        cur.execute(
            "select trading_date from staging.market_prices_daily where symbol = %s",
            (symbol,),
        )
        daily_dates_written = {row[0] for row in cur.fetchall()}
    assert today not in daily_dates_written
    assert prior_session in daily_dates_written
    assert result["daily_cutoffs"] == sorted(set(daily_dates[:-1]))
    assert today not in result["daily_cutoffs"]


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
    """A fake vendor. A daily call answers one close per `adjust` value."""
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


def test_refresh_market_data_takes_its_cutoffs_from_the_split_adjusted_series(connection) -> None:
    """A date that only the unadjusted series holds is no cutoff of the split-adjusted series."""
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

    152 is production's busiest capture day of 2026-09-08..15. The daily share is the one that the gate
    enforces: `gateway.CAPACITIES["twelvedata"].in_environment("production")`.
    """
    measured_busiest_capture_day = 152
    requests: list[tuple[str, str, str]] = []
    client = _adjust_aware_client(requests, [])

    _refresh_market_data(dg.build_op_context(), connection, symbols=TOPT_SYMBOLS, now=LANE_NOW, client=client)

    production = gateway.CAPACITIES["twelvedata"].in_environment("production")
    assert production is not None
    production_budget = production.daily_budget
    assert production_budget is not None
    assert len(requests) + measured_busiest_capture_day <= production_budget, (
        f"the lane makes {len(requests)} Twelve Data calls a run; production's daily share is {production_budget}"
    )


#: The gap that absorbs one slow vendor call between the last refresh call and the backtest.
BACKTEST_MARGIN = timedelta(minutes=5)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_the_backtest_starts_after_the_refresh_has_ended_in_each_environment(connection, environment: str) -> None:
    """#1281: the refresh makes N calls at its environment's per-minute share, and the backtest reads the bars.

    Staging's share is 3 calls a minute, so 60 calls take 20 minutes. Production's share is 4 a minute,
    so 60 calls take 15 minutes. The backtest must start after the last call, with a margin. The test
    measures N, reads the shares from the capacity declaration and reads both crons from the lanes.
    It is red on a backtest at 21:30 UTC in both environments.
    """
    requests: list[tuple[str, str, str]] = []
    _refresh_market_data(
        dg.build_op_context(),
        connection,
        symbols=TOPT_SYMBOLS,
        now=LANE_NOW,
        client=_adjust_aware_client(requests, []),
    )
    twelve = gateway.CAPACITIES["twelvedata"].in_environment(environment)
    assert twelve is not None and twelve.calls_per_window, f"{environment} has no Twelve Data share"
    assert twelve.window_seconds is not None
    refresh_duration = timedelta(seconds=math.ceil(len(requests) / twelve.calls_per_window) * twelve.window_seconds)

    from cron_schedule import Cron as cron

    refresh, backtest = cron.parse(market_data.MARKET_DATA_REFRESH_CRON), cron.parse(backtest_lane.BACKTEST_CRON)
    monday = datetime(2026, 10, 12, tzinfo=UTC)
    refreshes = refresh.fires_between(monday, monday + timedelta(days=7))
    assert refreshes, "the refresh must fire in the sampled week, or this test proves nothing"
    for started in refreshes:
        day_end = started.replace(hour=23, minute=59)
        backtests = backtest.fires_between(started, day_end)
        assert backtests, f"no backtest follows the refresh of {started.date().isoformat()}"
        assert backtests[0] >= started + refresh_duration + BACKTEST_MARGIN, (
            f"{environment}: the refresh of {len(requests)} calls ends at "
            f"{(started + refresh_duration).strftime('%H:%M')} UTC, but the backtest starts at "
            f"{backtests[0].strftime('%H:%M')} UTC"
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
    assert MAX_BAR_LAG_SESSIONS == 1
    assert "newest bar 2026-10-02 is more than 1 session behind 2026-10-06" in verdict.summary, verdict.summary


@pytest.mark.parametrize(
    ("latest", "oldest"),
    [
        (date(2026, 10, 14), date(2026, 10, 13)),  # a Wednesday: Tuesday
        (date(2026, 10, 5), date(2026, 10, 2)),  # a Monday: Friday, not Sunday
        (date(2026, 11, 27), date(2026, 11, 25)),  # the Friday after Thanksgiving: Wednesday
    ],
)
def test_the_oldest_session_within_the_lag_skips_weekends_and_holidays(latest: date, oldest: date) -> None:
    """The lane's verdict and the live seed's split series check share this one definition (#1262)."""
    assert oldest_session_within_lag(latest) == oldest


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


@pytest.fixture
def one_symbol(monkeypatch) -> None:
    """The symbols the lane stores come from the corpus and the relation, which a fake connection cannot answer.
    These tests assert what the lane records after its ingest, so they name one symbol."""
    from data_engine.datahub import market_prices

    monkeypatch.setattr(market_prices, "stored_symbols", lambda *_a, **_k: ("AAPL",))


def _run_job() -> dg.ExecuteInProcessResult:
    return market_data.market_data_refresh_pipeline_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK}, raise_on_error=False
    )


def test_the_lane_declares_the_verdict_it_records() -> None:
    assert market_data.NIGHTLY_VERDICTS == ("market_data_freshness",)
    assert nightly_verdicts.is_valid_name("market_data_freshness")


def test_a_fresh_run_records_a_green_verdict_after_the_commit(
    monkeypatch, written, fake_connection, one_symbol
) -> None:
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
    monkeypatch, written, fake_connection, one_symbol
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


def _denominator(*tickers: str) -> dict[str, Any]:
    """A universe denominator of single-listing issuers, one for each ticker."""
    return {
        "instrument_tuple_fields": ["issuer_id", "instrument_id", "listing_id", "ticker"],
        "instruments": [
            [f"issuer:cik:{index:010d}", f"instrument:{ticker.lower()}", f"listing:xnas:{ticker.lower()}", ticker]
            for index, ticker in enumerate(tickers, start=1)
        ],
    }


def test_the_lane_stores_the_primary_listing_of_a_21st_issuer(monkeypatch, written, fake_connection) -> None:
    """The symbols of the lane come from the corpus of the universe record (#1522): an issuer that the corpus
    gains is stored with no edit in the lane. The 20-symbol tuple used to ignore it. This runs the deployed job."""
    from data_engine.datahub.capture_core import universe_corpus

    monkeypatch.setattr(universe_corpus, "load_corpus", lambda _filename: {"denominator": _denominator("AAPL", "ZZZZ")})
    asked: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        market_data, "_refresh_market_data", lambda _context, _connection, *, symbols, now: asked.append(tuple(symbols))
    )
    monkeypatch.setattr(
        market_data,
        "judge_bar_freshness",
        lambda _connection, symbols, *, now: market_data.BarFreshness(ok=True, summary=f"bars of {len(symbols)}"),
    )

    assert _run_job().success
    assert asked == [("AAPL", "ZZZZ")]
    assert [row["summary"] for row in written] == ["bars of 2"]


def test_a_crashing_ingest_records_a_red_verdict(monkeypatch, written, fake_connection, one_symbol) -> None:
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


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        ("production", dg.DefaultScheduleStatus.RUNNING),
        ("staging", dg.DefaultScheduleStatus.RUNNING),
        ("development", dg.DefaultScheduleStatus.STOPPED),
    ],
)
def test_the_refresh_schedule_runs_where_the_backtest_runs(
    monkeypatch: pytest.MonkeyPatch, app_env: str, expected: dg.DefaultScheduleStatus
) -> None:
    """#1281: staging's tick, projection and backtest read these bars, so staging refreshes them.

    The decorator reads `settings.app_env` when the module loads. The test loads the module again
    under each environment and restores it afterwards. It is red on the production-only rule.
    """
    monkeypatch.setattr(settings, "app_env", app_env)
    try:
        reloaded = importlib.reload(market_data)
        assert reloaded.market_data_refresh_schedule.default_status == expected
    finally:
        monkeypatch.undo()
        importlib.reload(market_data)


def test_judge_bar_freshness_is_red_when_one_symbol_is_stale_or_missing(connection) -> None:
    """#1523 S3: one fresh symbol does not vouch for a stale or missing symbol."""
    fresh_sym = "T1523FRESH"
    stale_sym = "T1523STALE"
    missing_sym = "T1523MISSING"
    _write_newest_bars(connection, fresh_sym, {"none": date(2026, 10, 5), "splits": date(2026, 10, 5)})
    _write_newest_bars(connection, stale_sym, {"none": date(2026, 9, 25), "splits": date(2026, 10, 5)})

    stale_verdict = market_data.judge_bar_freshness(connection, [fresh_sym, stale_sym], now=MONDAY_RUN)
    assert stale_verdict.ok is False
    assert "none: newest bar 2026-09-25" in stale_verdict.summary

    missing_verdict = market_data.judge_bar_freshness(connection, [fresh_sym, missing_sym], now=MONDAY_RUN)
    assert missing_verdict.ok is False
    assert "no bar" in missing_verdict.summary
