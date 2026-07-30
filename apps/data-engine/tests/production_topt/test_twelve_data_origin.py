"""The second price origin asserts the same quantity as the primary (#535 / #344 / #171).

Two guarantees live here.

1. **Quantity.** Reconciliation compares two numbers, so both origins have to be asserting
   the same thing. v1 read Twelve Data's newest `time_series` row at/before the cutoff,
   which on the partition date is the session's in-progress bar — the last trade, extended
   hours included. Against the primary's regular-session close that is a different
   quantity: AAPL on 2026-07-29 was 338.19 (settled) against 340.079987 (post-market),
   0.56% apart under a 0.1% tolerance, so every scheduled 22:15 UTC tick reported
   `conflict_abstained` instead of corroborating. The parser now asks for the partition
   date's settled end-of-day close and *refuses* anything else, so a live quote cannot
   silently become a corroboration input again.
2. **Throttle.** A rate-limited request that skipped its wait would fire the next symbol
   immediately and rate-limit the rest of the tick with it, so one refused request becomes
   many. The throttle belongs to every outcome, not just the successful one.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date
from decimal import Decimal

import pytest
from data_engine.datahub.production_topt import twelve_data_origin as origin_module
from data_engine.datahub.production_topt.market_price_adapter import MarketPriceQuote
from data_engine.datahub.production_topt.twelve_data_origin import (
    NotASessionCloseError,
    TwelveDataQuoteFetcher,
    parse_last_settled_close,
    parse_session_close,
)

_CUTOFF = date(2026, 3, 31)
_PARTITION = date(2026, 7, 29)

# The `/eod` answer for the partition date: one settled session, stamped with its session
# date and nothing finer.
_SETTLED_EOD = json.dumps(
    {
        "symbol": "AAPL",
        "exchange": "NASDAQ",
        "mic_code": "XNGS",
        "currency": "USD",
        "datetime": "2026-07-29",
        "close": "338.19000",
    }
).encode()

# What `/price` answers with — the live last trade that made every in-session tick abstain.
_LIVE_PRICE = json.dumps({"price": "340.079987"}).encode()

# What `/quote` answers with: it carries a real `close`, so only the extended-hours fields
# distinguish it from an end-of-day payload. Accepting it would reintroduce #535 with a
# payload that passes every value-shaped check.
_REAL_TIME_SNAPSHOT = json.dumps(
    {
        "symbol": "AAPL",
        "datetime": "2026-07-29",
        "close": "338.19000",
        "previous_close": "336.02000",
        "is_market_open": False,
        "extended_price": "340.079987",
        "extended_timestamp": 1785450000,
    }
).encode()

_NO_END_OF_DAY = json.dumps(
    {"code": 404, "message": "No data is available on the specified dates", "status": "error"}
).encode()

# A `time_series` window whose newest row is the partition date's IN-PROGRESS bar.
_TIME_SERIES_WITH_IN_PROGRESS_ROW = json.dumps(
    {
        "meta": {"symbol": "AAPL", "interval": "1day", "exchange": "NASDAQ"},
        "values": [
            {"datetime": "2026-07-29", "open": "336.5", "high": "341.0", "low": "335.9", "close": "340.079987"},
            {"datetime": "2026-07-28", "open": "334.1", "high": "337.4", "low": "333.8", "close": "337.10000"},
            {"datetime": "2026-07-27", "open": "331.0", "high": "335.2", "low": "330.6", "close": "334.55000"},
        ],
        "status": "ok",
    }
).encode()


class _RecordingFetcher(TwelveDataQuoteFetcher):
    """The real fetcher with its HTTP call and its sleep replaced by records."""

    def __init__(self, outcomes: dict[str, object]) -> None:
        super().__init__("test-key", throttle_seconds=1)
        self._outcomes = outcomes
        self.slept: list[int] = []
        self.requested: list[str] = []

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        self.requested.append(symbol)
        outcome = self._outcomes[symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("data_engine.datahub.production_topt.twelve_data_origin.time.sleep", slept.append)
    return slept


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def http(monkeypatch):
    """Records every URL the fetcher asks for and answers with queued bodies."""
    requested: list[str] = []
    bodies: list[bytes] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        requested.append(url)
        if not bodies:
            raise AssertionError(f"unexpected extra Twelve Data request: {url}")
        return _FakeResponse(bodies.pop(0))

    monkeypatch.setattr(origin_module.urllib.request, "urlopen", fake_urlopen)
    return requested, bodies


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# -- the accepted quantity -------------------------------------------------------------


def test_the_partition_dates_settled_close_is_accepted() -> None:
    quote = parse_session_close(_SETTLED_EOD, partition=_PARTITION)
    assert quote is not None, "the partition date's settled end-of-day close must corroborate"
    assert quote.close == Decimal("338.19000")
    assert quote.as_of == _PARTITION
    assert quote.knowable_at.date() <= _PARTITION, "a corroboration knowable after the cutoff is look-ahead"
    assert quote.raw_bytes == _SETTLED_EOD, "the landed bytes must be the vendor's own response"


def test_the_fetcher_asks_for_the_partition_dates_end_of_day(http) -> None:
    requested, bodies = http
    bodies.append(_SETTLED_EOD)
    quote = TwelveDataQuoteFetcher("test-key", throttle_seconds=0)("AAPL", _PARTITION)

    assert quote is not None and quote.close == Decimal("338.19000")
    assert len(requested) == 1, "a settled partition date needs exactly one request"
    assert urllib.parse.urlsplit(requested[0]).path == "/eod", (
        "the second origin must ask for the end-of-day close, not a live price"
    )
    assert _query(requested[0])["date"] == str(_PARTITION), "the end of day must be pinned to the partition date"
    assert _query(requested[0])["symbol"] == "AAPL"


# -- the refused quantities ------------------------------------------------------------


def test_a_live_quote_payload_is_rejected() -> None:
    """`/price` — the last trade, extended hours included. Not a session close."""
    with pytest.raises(NotASessionCloseError, match="live quote"):
        parse_session_close(_LIVE_PRICE, partition=_PARTITION)


def test_a_real_time_snapshot_carrying_extended_hours_is_rejected() -> None:
    with pytest.raises(NotASessionCloseError, match="extended_price"):
        parse_session_close(_REAL_TIME_SNAPSHOT, partition=_PARTITION)


def test_an_intraday_bar_is_rejected() -> None:
    intraday = json.dumps({"symbol": "AAPL", "datetime": "2026-07-29 15:59:00", "close": "340.079987"}).encode()
    with pytest.raises(NotASessionCloseError, match="instant, not a session date"):
        parse_session_close(intraday, partition=_PARTITION)


def test_a_close_from_after_the_partition_is_rejected() -> None:
    ahead = json.dumps({"symbol": "AAPL", "datetime": "2026-07-30", "close": "341.00000"}).encode()
    with pytest.raises(NotASessionCloseError, match="after the 2026-07-29 partition"):
        parse_session_close(ahead, partition=_PARTITION)


def test_a_refused_quantity_leaves_the_cell_single_origin(http) -> None:
    """A rejected payload must make the origin ABSENT, never a corroborating number.

    This is the #535 outcome the report has to be able to reach: honest
    `insufficient_independent_origins` rather than `conflict_abstained` built from a
    quantity that was never comparable.
    """
    requested, bodies = http
    bodies.extend([_LIVE_PRICE])
    assert TwelveDataQuoteFetcher("test-key", throttle_seconds=0)("AAPL", _PARTITION) is None
    assert len(requested) == 1, "a refused quantity must not be retried into a corroboration"


# -- the partition date with no end of day of its own ----------------------------------


def test_no_end_of_day_falls_back_to_the_last_settled_session(http) -> None:
    """Weekends, holidays and pre-session ticks keep their corroboration.

    The primary resolves the newest session at/before its cutoff, so on a partition date
    with no session of its own the second origin has to resolve the same one.
    """
    requested, bodies = http
    bodies.extend([_NO_END_OF_DAY, _TIME_SERIES_WITH_IN_PROGRESS_ROW])
    quote = TwelveDataQuoteFetcher("test-key", throttle_seconds=0)("AAPL", _PARTITION)

    assert quote is not None and quote.as_of == date(2026, 7, 28)
    assert quote.close == Decimal("337.10000")
    assert urllib.parse.urlsplit(requested[1]).path == "/time_series"


def test_the_in_progress_bar_for_the_partition_date_is_never_the_fallback() -> None:
    """The exact v1 defect: "newest row at/before the cutoff" takes the unsettled bar.

    Turns red the moment the selection rule loosens from `<` back to `<=`.
    """
    quote = parse_last_settled_close(_TIME_SERIES_WITH_IN_PROGRESS_ROW, partition=_PARTITION)
    assert quote is not None
    assert quote.close != Decimal("340.079987"), "the partition date's in-progress bar is not a settled close"
    assert (quote.as_of, quote.close) == (date(2026, 7, 28), Decimal("337.10000"))


def test_the_fallback_also_refuses_a_live_quote() -> None:
    with pytest.raises(NotASessionCloseError, match="live quote"):
        parse_last_settled_close(_LIVE_PRICE, partition=_PARTITION)


# -- the identity fusion reads back ----------------------------------------------------


def test_the_origins_value_key_is_the_one_fusion_reads(monkeypatch) -> None:
    """A value-key drift drops the second origin out of fusion with no error anywhere.

    `quality_report._SOURCE_BY_PARSER` reads this parser vintage's number out of the
    payload by key. If the origin writes `close` and the registry still looks for `price`,
    every cell silently reports one origin — the same invisible failure #535 was.
    """
    from data_engine.datahub.quality_report import _SOURCE_BY_PARSER

    monkeypatch.setattr(origin_module.settings, "twelve_data_api_key", "test-key")
    origin = origin_module.twelve_data_origin()
    assert origin is not None
    assert origin.parser_version in _SOURCE_BY_PARSER, "the current parser vintage is not mapped to an origin group"
    assert _SOURCE_BY_PARSER[origin.parser_version][2] == origin.value_key
    assert _SOURCE_BY_PARSER["twelve-data-parser:v1"][2] == "price", (
        "v1 observations are still in the warehouse and must keep resolving their own key"
    )


def test_no_key_means_no_second_origin(monkeypatch) -> None:
    monkeypatch.setattr(origin_module.settings, "twelve_data_api_key", "")
    assert origin_module.twelve_data_origin() is None


# -- the throttle belongs to every outcome ---------------------------------------------


def test_a_refused_request_still_throttles_and_is_absent(_no_real_sleep) -> None:
    fetcher = _RecordingFetcher({"AAPL": RuntimeError("429 rate limited")})
    assert fetcher("AAPL", _CUTOFF) is None
    assert _no_real_sleep == [1], "a failed second-origin request must still wait its turn"


def test_api_key_is_required() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        TwelveDataQuoteFetcher("")


def test_one_request_per_symbol_per_run(_no_real_sleep) -> None:
    """Repeat lookups come from the cache — including a cached failure."""
    fetcher = _RecordingFetcher({"AAPL": RuntimeError("boom")})
    assert fetcher("AAPL", _CUTOFF) is None
    assert fetcher("AAPL", _CUTOFF) is None
    assert fetcher.requested == ["AAPL"], "a cached outcome must not re-hit the provider"
    assert _no_real_sleep == [1], "the cached call must not throttle again"


def test_the_fallback_request_takes_its_own_throttle_turn(_no_real_sleep, http) -> None:
    """Two requests in one call must consume two turns, or a fallback tick doubles the rate."""
    requested, bodies = http
    bodies.extend([_NO_END_OF_DAY, _TIME_SERIES_WITH_IN_PROGRESS_ROW])
    fetcher = TwelveDataQuoteFetcher("test-key", throttle_seconds=1)
    assert fetcher("AAPL", _PARTITION) is not None
    assert len(requested) == 2
    assert _no_real_sleep == [1, 1], "the fallback request must wait its turn like any other"
