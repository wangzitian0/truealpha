"""#1277: the QQQ price lane stores both daily series of the QQQ listings outside TOPT within the Twelve Data budget.

The pure tests hold the symbol rule, the fetch order, the budget formula, the coverage rule and the
schedule's place in the evening. The database tests drive the op body against the real schema with a
fake vendor. Where the ledger rows matter, the fake sits behind `urllib.request.urlopen`, so every
request travels the real gateway path (`gateway.urlopen`, `record_call`). No test reaches a vendor.
"""

from __future__ import annotations

import importlib
import io
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine import dagster_defs
from data_engine.config import settings
from data_engine.datahub.market_prices import (
    DEFAULT_TOPT_SYMBOLS,
    SINGLE_SOURCE_CONFIDENCE,
    TwelveDataApiError,
    TwelveDataClient,
    ingest_daily_series,
    ingest_twelve_data_market_prices,
    most_recent_xnys_session,
    xnys_session_close_utc,
)
from data_engine.datahub.production_topt.source_registrations import LEDGER_CAPACITIES, environment_share
from data_engine.datahub.production_topt.universe_plane import parse_nasdaq_index_rows
from data_engine.datahub.share_basis import LiveShareCount, OnCloseBasis, SplitHistory, live_on_close_basis
from data_engine.lanes import capture, market_data, qqq_prices
from data_engine.lanes.market_data import _daily_cutoff
from data_engine.sources import gateway
from truealpha_runtime.testing import load_tool

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from qqq_head_seed import daily_series_client, daily_values, publish_qqq_head  # noqa: E402
from split_series_seed import store_daily_series  # noqa: E402

#: The Nasdaq-100 list as the index operator answered it on 2026-08-17 (sha-pinned by `test_universe_plane.py`).
NASDAQ100_CASSETTE = Path(__file__).parent / "production_topt" / "cassettes" / "nasdaq100_index.bcc3fb15.json"
#: The Twelve Data calls of all other callers on 2026-10-15, from read-only SQL on each ledger (#1277).
PRODUCTION_OTHER_CALLERS_DAY = 284
STAGING_OTHER_CALLERS_DAY = 306
#: The gap that absorbs one slow vendor call between this lane's last call and the next core tick.
TICK_MARGIN = timedelta(minutes=5)
#: A caller other than this lane, as the capture ticks record it.
OTHER_CALLER = "twelve_data_origin.eod"


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


def _denominator(rows: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """A universe denominator of (issuer_id, ticker) rows, in the shape `universe_plane.build_denominator` writes."""
    return {
        "instrument_tuple_fields": ["issuer_id", "instrument_id", "listing_id", "ticker"],
        "instruments": [
            [issuer, f"security:figi:bbg{ticker}", f"listing:xnas:{ticker.lower()}", ticker] for issuer, ticker in rows
        ],
    }


def _calls(ledger: gateway.MemoryLedger, caller: str, at: datetime, count: int, *, source: str = "twelvedata") -> None:
    for _ in range(count):
        ledger.append(gateway.CallRecord(source=source, endpoint="time_series", caller=caller, called_at=at))


# --- which symbols, in which order ------------------------------------------------------------


def test_the_lane_keeps_the_declared_primary_listing_per_issuer_and_skips_the_topt_issuers(connection) -> None:
    from data_engine.datahub.resolve_coordinates import resolve_coordinates

    known_at = datetime(2026, 10, 6, 22, tzinfo=UTC)
    denominator = _denominator(
        [
            ("issuer:cik:0000320193", "AAPL"),  # a TOPT symbol: the market-data lane stores it
            ("issuer:cik:0001652044", "GOOG"),  # Alphabet, the secondary class
            ("issuer:cik:0001652044", "GOOGL"),  # Alphabet, the declared primary listing and a TOPT symbol
            ("issuer:cik:0000796343", "ADBE"),
            ("issuer:cik:0001754301", "FOXA"),  # two listings of one issuer, and no row declares either:
            ("issuer:cik:0001754301", "FOX"),  # the issuer adds no symbol (#1518)
        ]
    )
    resolve_coordinates(connection, denominator["instruments"], as_of=known_at.date(), known_at=known_at)

    assert qqq_prices.target_symbols(connection, denominator, covered=DEFAULT_TOPT_SYMBOLS, known_at=known_at) == (
        "ADBE",
    )
    # Without the TOPT rule each declared issuer adds its primary listing only. GOOG never appears.
    assert qqq_prices.target_symbols(connection, denominator, covered=(), known_at=known_at) == (
        "AAPL",
        "ADBE",
        "GOOGL",
    )


def test_symbols_without_both_series_come_first_then_the_oldest() -> None:
    newest = {"BBB": date(2026, 10, 5), "DDD": date(2026, 10, 1), "EEE": date(2026, 10, 1)}

    order = qqq_prices.refresh_order(["EEE", "CCC", "BBB", "AAA", "DDD"], newest)

    assert order == ["AAA", "CCC", "DDD", "EEE", "BBB"]


# --- how much -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "reserve", "spent_today", "targets", "credits"),
    [
        # Production as measured: 85 % of 480 is 408. 408 - 284 = 124 covers the need of 89 targets, 90.
        ("production", PRODUCTION_OTHER_CALLERS_DAY, 0, 89, 90),
        # A larger universe meets the ceiling, not its need of 300: 408 - 284 = 124.
        ("production", PRODUCTION_OTHER_CALLERS_DAY, 0, 300, 124),
        # A second run on one day spends what the first one left: 408 - 284 - 70 = 54.
        ("production", PRODUCTION_OTHER_CALLERS_DAY, 70, 300, 54),
        # Staging as measured: 85 % of 320 is 272, less than the 306 the other callers spend.
        ("staging", STAGING_OTHER_CALLERS_DAY, 0, 89, 0),
        # An empty ledger: the need alone.
        ("production", 0, 0, 89, 90),
        # An environment without a share of the key gets nothing (local, CI).
        ("development", 0, 0, 89, 0),
    ],
)
def test_the_budget_is_the_need_capped_by_what_the_other_callers_leave(
    environment: str, reserve: int, spent_today: int, targets: int, credits: int
) -> None:
    budget = qqq_prices.credit_budget(
        share=qqq_prices.environment_share(environment), reserve=reserve, spent_today=spent_today, targets=targets
    )

    assert budget.credits == credits, budget


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
def test_the_share_is_the_one_the_gate_enforces(environment: str) -> None:
    twelve = LEDGER_CAPACITIES["twelvedata"]
    assert twelve.daily_budget is not None

    assert qqq_prices.environment_share(environment) == environment_share(
        twelve.daily_budget, twelve.environment_shares, environment
    )


@pytest.mark.parametrize(("targets", "need"), [(0, 0), (1, 2), (2, 2), (3, 4), (89, 90), (91, 92)])
def test_a_run_needs_two_credits_for_each_symbol_of_the_stalest_half(targets: int, need: int) -> None:
    assert qqq_prices.need_credits(targets) == need


def test_the_reserve_is_the_busiest_complete_day_of_the_other_callers(call_ledger: gateway.MemoryLedger) -> None:
    today = date(2026, 10, 19)

    def at(days_ago: int, hour: int) -> datetime:
        return datetime.combine(today - timedelta(days=days_ago), time(hour), tzinfo=UTC)

    _calls(call_ledger, OTHER_CALLER, at(1, 22), 200)
    _calls(call_ledger, "market_prices.fetch_time_series", at(1, 21), 85)  # day -1: 285 by the other callers
    _calls(call_ledger, OTHER_CALLER, at(3, 23), 250)
    _calls(call_ledger, OTHER_CALLER, at(8, 23), 500)  # older than the last seven complete days
    _calls(call_ledger, qqq_prices.QQQ_PRICE_CALLER, at(1, 21), 90)  # this lane never reserves for itself
    _calls(call_ledger, OTHER_CALLER, at(0, 0), 28)  # today is not a complete day
    _calls(call_ledger, qqq_prices.QQQ_PRICE_CALLER, at(0, 21), 10)
    _calls(call_ledger, "sec-submissions", at(2, 12), 900, source="sec")  # another seat

    assert qqq_prices.ledger_spend(today) == qqq_prices.LedgerSpend(reserve=285, spent_today=10)


def test_an_empty_ledger_reserves_nothing(call_ledger: gateway.MemoryLedger) -> None:
    assert qqq_prices.ledger_spend(date(2026, 10, 19)) == qqq_prices.LedgerSpend(reserve=0, spent_today=0)


# --- when -------------------------------------------------------------------------------------


def _valid_at_tick(stored: date | None, close: date) -> bool:
    """Does a symbol whose stored series end on `stored` let the core value the close of `close`?

    Asked of the deployed check (`share_basis.live_on_close_basis`), with no split and no price jump.
    """
    if stored is None:
        return False
    count = LiveShareCount(
        shares=Decimal(1000),
        knowable_at=datetime(2020, 2, 3, tzinfo=UTC),
        entry={"period_end": "2020-01-31", "filed": "2020-02-03"},
        close_session=close,
        close_knowable_at=xnys_session_close_utc(close),
        close=Decimal(40),
    )
    history = SplitHistory(
        "SYM", (), first_session=date(2019, 1, 2), last_session=stored, last_unadjusted_close=Decimal(40)
    )
    outcome = live_on_close_basis(
        count,
        (history,),
        documents=lambda: pytest.fail("no split: the documents are not read"),
        ruleset=lambda: pytest.fail("no split: the ruleset is not read"),
    )
    return isinstance(outcome, OnCloseBasis)


@pytest.mark.parametrize("targets", [1, 2, 5, 89, 91])
def test_refreshing_the_stalest_half_each_weekday_keeps_every_symbol_valid_at_every_tick(targets: int) -> None:
    """The coverage rule. A run on trading day D stores session D, which stays valid for the ticks of D and D+1.

    The weekend ticks after a Friday run value Friday's close. So the weekday runs, each of
    `need_credits` worth of the stalest symbols, keep every symbol valid at every QQQ tick once the
    first two runs have stored each symbol. The span crosses the US clock change and the holidays of
    late 2026. It is red when a run refreshes fewer symbols or runs before the close.
    """
    cron = load_tool("scheduler_liveness").Cron
    start, end = datetime(2026, 10, 26, tzinfo=UTC), datetime(2027, 1, 9, tzinfo=UTC)
    runs = cron.parse(qqq_prices.QQQ_PRICE_REFRESH_CRON).fires_between(start, end)
    ticks = cron.parse(capture.tick_cron("qqq", "production")).fires_between(start, end)
    symbols = [f"S{number:03d}" for number in range(targets)]
    per_run = qqq_prices.need_credits(targets) // qqq_prices.CREDITS_PER_SYMBOL
    stored: dict[str, date] = {}
    checked = 0
    for moment, kind in sorted([(run, "run") for run in runs] + [(tick, "tick") for tick in ticks]):
        if kind == "run":
            for symbol in qqq_prices.refresh_order(symbols, stored)[:per_run]:
                stored[symbol] = _daily_cutoff(moment)  # the session that closed last before the run
        elif moment > runs[1]:
            close = _daily_cutoff(moment)  # the close the tick values
            stale = [symbol for symbol in symbols if not _valid_at_tick(stored.get(symbol), close)]
            assert not stale, f"the tick of {moment:%a %Y-%m-%d} values {close}: {len(stale)} symbols are too old"
            checked += 1
    assert checked > 60, "the span must hold weeks of ticks, or this test proves nothing"


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_the_run_starts_after_the_market_data_refresh_and_ends_before_the_core_tick(
    environment: str, connection
) -> None:
    """Both durations are calls divided by the environment's per-minute share.

    The refresh's calls are measured through its own ingest function. This lane's calls are its need
    for the operator's 2026-08-17 list, with each ticker as its own issuer: an upper bound.
    """
    tickers = [row["ticker"] for row in parse_nasdaq_index_rows(NASDAQ100_CASSETTE.read_bytes())]
    targets = qqq_prices.target_symbols(
        connection,
        _denominator([(f"issuer:{ticker}", ticker) for ticker in tickers]),
        covered=DEFAULT_TOPT_SYMBOLS,
        known_at=datetime(2026, 10, 6, 22, tzinfo=UTC),
    )
    refresh = ingest_twelve_data_market_prices(
        symbols=DEFAULT_TOPT_SYMBOLS,
        client=daily_series_client([], [], caller="market_prices.fetch_time_series"),
        connection=None,
        now=datetime(2026, 10, 6, 22, tzinfo=UTC),
    )
    twelve = LEDGER_CAPACITIES["twelvedata"]
    per_minute = environment_share(twelve.calls_per_window, twelve.environment_shares, environment)
    assert per_minute, f"{environment} has no Twelve Data share"
    window = timedelta(seconds=twelve.window_seconds)
    refresh_takes = math.ceil(refresh.total_calls / per_minute) * window
    run_takes = math.ceil(qqq_prices.need_credits(len(targets)) / per_minute) * window

    cron = load_tool("scheduler_liveness").Cron
    week = (datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 18, 23, 59, tzinfo=UTC))
    refreshes = cron.parse(market_data.MARKET_DATA_REFRESH_CRON).fires_between(*week)
    runs = cron.parse(qqq_prices.QQQ_PRICE_REFRESH_CRON).fires_between(*week)
    core = cron.parse(capture.tick_cron("topt", environment))
    assert len(runs) == len(refreshes) == 5, "one run after each weekday refresh"
    for refreshed, run in zip(refreshes, runs, strict=True):
        assert run >= refreshed + refresh_takes, (
            f"{environment}: the refresh of {refresh.total_calls} calls runs past {run:%H:%M}"
        )
        next_core = core.next_fire(run)
        assert next_core is not None
        assert run + run_takes + TICK_MARGIN <= next_core, (
            f"{environment}: {len(targets)} targets take {run_takes} from {run:%H:%M} UTC; the core tick is at {next_core:%H:%M}"
        )


# --- the deployed op body, against the real schema ---------------------------------------------


class _Answer:
    def __init__(self, body: bytes) -> None:
        assert isinstance(body, bytes), "a urllib response reads bytes"
        self.status = 200
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _Vendor:
    """A fake Twelve Data behind `urllib.request.urlopen`. The requests travel the real gateway path.

    `failing` maps a symbol to the status every request for it answers. `flaky` holds the
    (symbol, adjust) pairs whose first request answers 429 and whose retry succeeds.
    """

    def __init__(self, sessions: Sequence[date], *, failing: dict[str, int] | None = None) -> None:
        self.sessions = sessions
        self.failing = dict(failing or {})
        self.flaky: set[tuple[str, str]] = set()
        self.requests: list[tuple[str, str, str]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> _Answer:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(request.full_url).query))
        key = (query["symbol"], query["adjust"])
        self.requests.append((query["symbol"], query["interval"], query["adjust"]))
        status = self.failing.get(query["symbol"])
        if status is None and key in self.flaky:
            self.flaky.discard(key)
            status = 429
        if status is not None:
            body = json.dumps({"code": status, "message": "vendor refusal", "status": "error"}).encode()
            raise urllib.error.HTTPError(request.full_url, status, "vendor refusal", hdrs=None, fp=io.BytesIO(body))
        return _Answer(json.dumps({"values": daily_values(self.sessions)}).encode())


def _vendor(monkeypatch: pytest.MonkeyPatch, sessions: Sequence[date], **kwargs: Any) -> _Vendor:
    vendor = _Vendor(sessions, **kwargs)
    monkeypatch.setattr(urllib.request, "urlopen", vendor)
    return vendor


def _recent_sessions(now: datetime, count: int) -> list[date]:
    """The last `count` sessions that closed before `now`."""
    sessions = [_daily_cutoff(now)]
    while len(sessions) < count:
        sessions.append(most_recent_xnys_session(sessions[-1] - timedelta(days=1)))
    return sorted(sessions)


def _client() -> TwelveDataClient:
    """The lane's client on the real transport (`gateway.urlopen`). The rate limiter and the backoff never sleep."""
    return TwelveDataClient(api_key="test", caller=qqq_prices.QQQ_PRICE_CALLER, sleep_fn=lambda _seconds: None)


def _run(
    connection: psycopg.Connection,
    now: datetime,
    *,
    environment: str = "production",
    dry_run: bool = False,
    max_symbols: int | None = None,
) -> qqq_prices.RefreshOutcome:
    commits: list[int] = []
    outcome = qqq_prices._refresh_qqq_prices(
        dg.build_op_context(),
        connection,
        now=now,
        environment=environment,
        client=_client(),
        commit=lambda: commits.append(1),
        dry_run=dry_run,
        max_symbols=max_symbols,
    )
    assert len(commits) == len(outcome.stored), "the run commits each stored symbol alone"
    return outcome


def _series(connection: psycopg.Connection, symbol: str) -> list[tuple[str, int, date]]:
    return connection.execute(
        "select adjust, count(*), max(trading_date) from staging.market_prices_daily "
        "where symbol = %s group by adjust order by adjust",
        (symbol,),
    ).fetchall()


def _yesterday(now: datetime) -> datetime:
    return datetime.combine(now.date() - timedelta(days=1), time(22), tzinfo=UTC)


#: Five targets beside a TOPT symbol: a run needs 2 * ceil(5 / 2) = 6 credits.
FIVE_TARGETS = [
    ("AAPL", 320193),
    ("T1277A", 9001),
    ("T1277B", 9002),
    ("T1277C", 9003),
    ("T1277D", 9004),
    ("T1277E", 9005),
]


def test_runs_store_both_series_through_the_gateway_and_a_failed_symbol_goes_first_next_time(
    connection: psycopg.Connection, call_ledger: gateway.MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_qqq_head(connection, FIVE_TARGETS)
    now = datetime.now(UTC)
    sessions = _recent_sessions(now, 5)
    # 408 - 396 leaves this lane 12 credits a day: two runs of 6.
    _calls(call_ledger, OTHER_CALLER, _yesterday(now), 396)
    vendor = _vendor(monkeypatch, sessions, failing={"T1277B": 404})

    first = _run(connection, now)

    assert first.planned == ("T1277A", "T1277B", "T1277C")
    assert (first.stored, list(first.failed), first.calls) == (["T1277A", "T1277C"], ["T1277B"], 5)
    assert "404" in first.failed["T1277B"]
    lane_rows = [row for row in call_ledger if row.caller == qqq_prices.QQQ_PRICE_CALLER]
    assert [(row.source, row.endpoint, row.ok) for row in lane_rows] == [
        ("twelvedata", "time_series", True),
        ("twelvedata", "time_series", True),
        ("twelvedata", "time_series", False),
        ("twelvedata", "time_series", True),
        ("twelvedata", "time_series", True),
    ], "every request, the refused one too, is one ledger row under this lane's caller"
    for symbol in ("T1277A", "T1277C"):
        assert _series(connection, symbol) == [("none", 5, sessions[-1]), ("splits", 5, sessions[-1])]
    assert _series(connection, "T1277B") == [], "a failed symbol writes nothing"
    assert ("AAPL", "1day", "splits") not in vendor.requests, "the market-data lane stores the TOPT symbols"

    vendor.failing.clear()
    second = _run(connection, now)

    assert second.budget.spent_today == 5, "the second run reads the first run's calls from the ledger"
    assert second.planned == ("T1277B", "T1277D", "T1277E"), "the failed symbol keeps its place"
    assert second.stored == ["T1277B", "T1277D", "T1277E"]

    third = _run(connection, now)

    assert (third.budget.credits, third.planned, third.calls) == (1, (), 0)
    assert third.skipped_reason is not None and "less than the 2 credits" in third.skipped_reason


def test_the_newest_session_is_the_last_one_both_series_hold(connection: psycopg.Connection) -> None:
    """The order reads what the core reads: a session counts only when both series hold its close."""
    first, last, flat = date(2026, 9, 28), date(2026, 10, 6), Decimal(40)
    store_daily_series(connection, "T1277N1", lambda _day: (flat, flat), first=first, last=last)
    # The unadjusted series ends on 2026-10-02, so the core can use 2026-10-02 at most.
    store_daily_series(
        connection, "T1277N2", lambda day: (flat if day <= date(2026, 10, 2) else None, flat), first=first, last=last
    )
    store_daily_series(connection, "T1277N3", lambda _day: (None, flat), first=first, last=last)

    newest = qqq_prices.newest_sessions(connection, ["T1277N1", "T1277N2", "T1277N3", "T1277N4"])

    assert newest == {"T1277N1": last, "T1277N2": date(2026, 10, 2)}


def test_both_series_land_with_the_provenance_of_every_daily_bar(connection: psycopg.Connection) -> None:
    requests: list[tuple[str, str, str]] = []
    now = datetime(2026, 10, 6, 22, tzinfo=UTC)  # a Tuesday, after its close
    sessions = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)]
    client = daily_series_client(requests, sessions, caller=qqq_prices.QQQ_PRICE_CALLER)

    appended = ingest_daily_series("T1277P", client=client, connection=connection, now=now)

    assert appended == {"splits": 4, "none": 4}
    assert requests == [("T1277P", "1day", "splits"), ("T1277P", "1day", "none")]
    rows = connection.execute(
        "select adjust, trading_date, source, confidence, transaction_time, raw_ref "
        "from staging.market_prices_daily where symbol = 'T1277P' order by adjust, trading_date"
    ).fetchall()
    assert len(rows) == 8
    for adjust, trading_date, source, confidence, transaction_time, raw_ref in rows:
        assert (source, confidence) == ("twelvedata", SINGLE_SOURCE_CONFIDENCE)
        assert transaction_time == xnys_session_close_utc(trading_date), "knowable at the session close"
        assert raw_ref.endswith(f":{adjust}")


def test_a_symbol_whose_second_series_is_empty_writes_nothing(connection: psycopg.Connection) -> None:
    def transport(url: str) -> tuple[int, bytes]:
        adjust = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["adjust"]
        values = daily_values([date(2026, 10, 5)]) if adjust == "splits" else []
        return 200, json.dumps({"values": values}).encode()

    client = TwelveDataClient(api_key="test", transport_fn=transport, sleep_fn=lambda _s: None)

    with pytest.raises(TwelveDataApiError, match="no closed daily bar for T1277H"):
        ingest_daily_series("T1277H", client=client, connection=connection, now=datetime(2026, 10, 6, 22, tzinfo=UTC))
    assert _series(connection, "T1277H") == [], "one series without the other makes the issuer unavailable"


def test_a_rate_limit_stops_the_run(
    connection: psycopg.Connection, call_ledger: gateway.MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight targets need 8 credits. After T1277A's six attempts the budget still pays for T1277B: only the stop prevents it."""
    publish_qqq_head(connection, [(f"T1277{letter}", 9000 + n) for n, letter in enumerate("ABCDEFGH")])
    now = datetime.now(UTC)
    vendor = _vendor(monkeypatch, _recent_sessions(now, 3), failing={"T1277A": 429})

    outcome = _run(connection, now)

    assert outcome.budget.credits == 8
    assert (outcome.stored, list(outcome.failed), outcome.stopped_by) == ([], ["T1277A"], "TwelveDataRateLimitError")
    assert outcome.calls == 1 + _client().max_retries
    assert {symbol for symbol, _interval, _adjust in vendor.requests} == {"T1277A"}


def test_every_retry_counts_against_the_budget(
    connection: psycopg.Connection, call_ledger: gateway.MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four targets need 4 credits. T1277A spends 3 with one retry, so T1277B would pass the budget."""
    publish_qqq_head(connection, [(f"T1277{letter}", 9000 + n) for n, letter in enumerate("ABCD")])
    now = datetime.now(UTC)
    vendor = _vendor(monkeypatch, _recent_sessions(now, 3))
    vendor.flaky.add(("T1277A", "none"))

    outcome = _run(connection, now)

    assert outcome.planned == ("T1277A", "T1277B")
    assert (outcome.stored, outcome.failed, outcome.calls, outcome.stopped_by) == (["T1277A"], {}, 3, "budget")
    assert {symbol for symbol, _interval, _adjust in vendor.requests} == {"T1277A"}


def test_a_dry_run_plans_within_the_budget_and_calls_nothing(
    connection: psycopg.Connection, call_ledger: gateway.MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_qqq_head(connection, FIVE_TARGETS)
    now = datetime.now(UTC)
    vendor = _vendor(monkeypatch, _recent_sessions(now, 3))

    outcome = _run(connection, now, dry_run=True)

    assert (outcome.budget.need, outcome.budget.credits) == (6, 6)
    assert outcome.planned == ("T1277A", "T1277B", "T1277C")
    assert (outcome.stored, outcome.calls, vendor.requests, list(call_ledger)) == ([], 0, [], [])


def test_max_symbols_caps_a_run(
    connection: psycopg.Connection, call_ledger: gateway.MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_qqq_head(connection, FIVE_TARGETS)
    now = datetime.now(UTC)
    _vendor(monkeypatch, _recent_sessions(now, 3))

    outcome = _run(connection, now, max_symbols=1)

    assert (outcome.planned, outcome.stored, outcome.calls) == (("T1277A",), ["T1277A"], 2)


def test_staging_fetches_nothing_while_its_measured_spend_fills_its_share(
    connection: psycopg.Connection, call_ledger: gateway.MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_qqq_head(connection, FIVE_TARGETS)
    now = datetime.now(UTC)
    _calls(call_ledger, OTHER_CALLER, _yesterday(now), STAGING_OTHER_CALLERS_DAY)
    vendor = _vendor(monkeypatch, _recent_sessions(now, 3))

    outcome = _run(connection, now, environment="staging")

    assert (outcome.budget.ceiling, outcome.budget.reserve, outcome.budget.credits) == (272, 306, 0)
    assert (outcome.planned, vendor.requests) == ((), [])
    assert outcome.skipped_reason is not None


# --- the Dagster surface ------------------------------------------------------------------------


class _Connection:
    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1


def _outcome(**kwargs: Any) -> qqq_prices.RefreshOutcome:
    budget = qqq_prices.credit_budget(share=480, reserve=0, spent_today=0, targets=4)
    return qqq_prices.RefreshOutcome(environment="production", targets=4, budget=budget, planned=("A", "B"), **kwargs)


def test_the_job_and_its_weekday_schedule_are_deployed() -> None:
    job = dagster_defs.defs.get_job_def(qqq_prices.QQQ_PRICE_REFRESH_JOB_NAME)
    schedule = dagster_defs.defs.get_schedule_def(qqq_prices.qqq_price_refresh_schedule.name)

    assert schedule.job_name == job.name == "qqq_price_refresh_pipeline"
    assert (schedule.cron_schedule, schedule.execution_timezone) == ("35 21 * * 1-5", "UTC")
    others = {
        other.name: other.cron_schedule
        for other in dagster_defs.defs.schedules or ()
        if other.name != schedule.name and str(other.cron_schedule).split()[:2] == ["35", "21"]
    }
    assert not others, f"another schedule fires at 21:35 UTC: {others}"

    tick = datetime(2026, 10, 19, 21, 35, tzinfo=UTC)
    result = schedule.evaluate_tick(dg.build_schedule_context(scheduled_execution_time=tick))
    ((request),) = result.run_requests or ()
    assert request.run_key == tick.isoformat()
    assert not request.run_config, "a scheduled run uses the defaults: no dry run, no cap"


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        ("production", dg.DefaultScheduleStatus.RUNNING),
        ("staging", dg.DefaultScheduleStatus.STOPPED),
        ("development", dg.DefaultScheduleStatus.STOPPED),
    ],
)
def test_the_schedule_runs_where_the_contract_schedules_the_qqq_universe(
    monkeypatch: pytest.MonkeyPatch, app_env: str, expected: dg.DefaultScheduleStatus
) -> None:
    """Staging runs TOPT only (#1416), so its QQQ prices wait: the status follows the contract, not an environment case."""
    monkeypatch.setattr(settings, "app_env", app_env)
    try:
        reloaded = importlib.reload(qqq_prices)
        assert reloaded.qqq_price_refresh_schedule.default_status == expected
    finally:
        monkeypatch.undo()
        importlib.reload(qqq_prices)


def test_a_manual_launch_reaches_the_run_with_its_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def body(_context: object, _connection: object, **kwargs: Any) -> qqq_prices.RefreshOutcome:
        seen.update(kwargs)
        return _outcome()

    fake = _Connection()
    monkeypatch.setattr(qqq_prices, "_refresh_qqq_prices", body)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: fake)

    result = qqq_prices.qqq_price_refresh_pipeline_job.execute_in_process(
        run_config={"ops": {"refresh_qqq_prices_op": {"config": {"dry_run": True, "max_symbols": 3}}}}
    )

    assert result.success
    assert (seen["dry_run"], seen["max_symbols"]) == (True, 3)
    assert seen["client"].caller == qqq_prices.QQQ_PRICE_CALLER
    assert seen["environment"] == gateway.capacity_environment()
    assert seen["commit"] == fake.commit
    with pytest.raises(dg.DagsterInvalidConfigError):
        qqq_prices.qqq_price_refresh_pipeline_job.execute_in_process(
            run_config={"ops": {"refresh_qqq_prices_op": {"config": {"max_symbols": "all"}}}}
        )


def test_a_failed_symbol_fails_the_run_and_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qqq_prices,
        "_refresh_qqq_prices",
        lambda *_a, **_k: _outcome(stored=["A"], failed={"B": "Twelve Data error 404: symbol is missing or invalid"}),
    )
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Connection())

    result = qqq_prices.qqq_price_refresh_pipeline_job.execute_in_process(raise_on_error=False)

    assert not result.success
    (failure,) = [event for event in result.all_events if event.event_type_value == "STEP_FAILURE"]
    error = failure.step_failure_data.user_failure_data
    assert error is not None and "B: Twelve Data error 404" in str(error.description)
    assert error.metadata["stored"].value == "A"
