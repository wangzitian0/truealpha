"""Unit tests for Twelve Data ingestion and market prices staging (#101).

Tests:
- Built-in token bucket rate limiter (8 calls/min compliant)
- Exponential backoff on 429
- XNYS holidays and monthly snapping to the last trading day of month
- 20 TOPT symbols, 1 call per symbol per resolution (40 calls total)
- Daily and monthly price bar parsing with Decimal precision
- Database insertion and upsert idempotency
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from data_engine.datahub.market_prices import (
    DEFAULT_TOPT_SYMBOLS,
    PriceBarRecord,
    TokenBucketRateLimiter,
    TwelveDataApiError,
    TwelveDataClient,
    TwelveDataRateLimitError,
    easter_sunday,
    ingest_twelve_data_market_prices,
    insert_market_prices_daily,
    insert_market_prices_monthly,
    is_xnys_trading_day,
    last_xnys_session_of_month,
    parse_daily_bars,
    parse_monthly_bars,
    snap_to_last_xnys_session_of_month,
    xnys_holidays,
)

# -----------------------------------------------------------------------------
# Rate Limiter Tests
# -----------------------------------------------------------------------------


def test_token_bucket_initial_capacity() -> None:
    clock = 100.0
    slept: list[float] = []

    limiter = TokenBucketRateLimiter(
        rate_per_minute=8.0,
        capacity=8.0,
        time_fn=lambda: clock,
        sleep_fn=slept.append,
    )
    # First 8 calls should not sleep
    for _ in range(8):
        wait = limiter.acquire(1.0)
        assert wait == 0.0
    assert len(slept) == 0

    # 9th call should require waiting
    wait = limiter.acquire(1.0)
    assert wait > 0.0
    assert len(slept) == 1
    # 8 calls per minute = 1 call every 7.5 seconds
    assert pytest.approx(wait, rel=1e-3) == 7.5


def test_token_bucket_refill_over_time() -> None:
    clock = 1000.0
    slept: list[float] = []

    def get_time() -> float:
        return clock

    limiter = TokenBucketRateLimiter(
        rate_per_minute=8.0,
        capacity=8.0,
        time_fn=get_time,
        sleep_fn=slept.append,
    )
    # Drain all 8 tokens
    for _ in range(8):
        limiter.acquire(1.0)

    # Advance clock by 60 seconds -> should replenish 8 tokens
    clock += 60.0

    # Now 8 calls should succeed without sleeping
    for _ in range(8):
        assert limiter.acquire(1.0) == 0.0
    assert len(slept) == 0


# -----------------------------------------------------------------------------
# Exchange Calendar & Snapping Tests
# -----------------------------------------------------------------------------


def test_easter_sunday_calculation() -> None:
    assert easter_sunday(2023) == date(2023, 4, 9)
    assert easter_sunday(2024) == date(2024, 3, 31)
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2026) == date(2026, 4, 5)


def test_xnys_holidays_includes_key_dates() -> None:
    holidays_2024 = xnys_holidays(2024)
    # Good Friday 2024: March 29
    assert date(2024, 3, 29) in holidays_2024
    # Juneteenth 2024: June 19
    assert date(2024, 6, 19) in holidays_2024
    # Christmas 2024: Dec 25
    assert date(2024, 12, 25) in holidays_2024
    # Thanksgiving 2024: Nov 28
    assert date(2024, 11, 28) in holidays_2024


def test_snap_to_last_xnys_session_march_2024() -> None:
    # In March 2024:
    # 2024-03-31 is Sunday (closed)
    # 2024-03-30 is Saturday (closed)
    # 2024-03-29 is Good Friday (holiday, closed)
    # 2024-03-28 is Thursday (valid session)
    assert not is_xnys_trading_day(date(2024, 3, 31))
    assert not is_xnys_trading_day(date(2024, 3, 30))
    assert not is_xnys_trading_day(date(2024, 3, 29))
    assert is_xnys_trading_day(date(2024, 3, 28))

    snapped = snap_to_last_xnys_session_of_month(date(2024, 3, 1))
    assert snapped == date(2024, 3, 28)


def test_snap_to_last_xnys_session_weekday_month_end() -> None:
    # April 30, 2026 is Thursday (valid trading day)
    snapped = last_xnys_session_of_month(2026, 4)
    assert snapped == date(2026, 4, 30)

    # May 31, 2026 is Sunday, May 30 is Saturday -> May 29 (Friday)
    # Memorial day 2026 is May 25, so Friday May 29 is open
    assert snap_to_last_xnys_session_of_month(date(2026, 5, 15)) == date(2026, 5, 29)


# -----------------------------------------------------------------------------
# Twelve Data Client & Exponential Backoff Tests
# -----------------------------------------------------------------------------


def test_twelvedata_client_handles_429_exponential_backoff() -> None:
    attempts = 0
    slept: list[float] = []

    def mock_transport(url: str) -> tuple[int, bytes]:
        nonlocal attempts
        attempts += 1
        assert "symbol=AAPL" in url
        if attempts < 3:
            return 429, json.dumps({"code": 429, "message": "API credits exhausted", "status": "error"}).encode()
        return 200, json.dumps(
            {
                "meta": {"symbol": "AAPL", "interval": "1day"},
                "values": [{"datetime": "2026-03-31", "close": "217.49"}],
                "status": "ok",
            }
        ).encode()

    limiter = TokenBucketRateLimiter(rate_per_minute=1000.0, capacity=1000.0)
    client = TwelveDataClient(
        api_key="test_key",
        rate_limiter=limiter,
        transport_fn=mock_transport,
        sleep_fn=slept.append,
        backoff_base=2.0,
        max_retries=4,
    )

    data = client.fetch_time_series("AAPL", interval="1day")
    assert attempts == 3
    # Exponential backoff: 2^0 = 1.0s, 2^1 = 2.0s
    assert slept == [1.0, 2.0]
    assert data["status"] == "ok"
    assert len(data["values"]) == 1


def test_twelvedata_client_exhausts_429_retries() -> None:
    slept: list[float] = []

    def always_429(url: str) -> tuple[int, bytes]:
        return 429, json.dumps({"code": 429, "message": "Minute limit", "status": "error"}).encode()

    limiter = TokenBucketRateLimiter(rate_per_minute=1000.0, capacity=1000.0)
    client = TwelveDataClient(
        api_key="test_key",
        rate_limiter=limiter,
        transport_fn=always_429,
        sleep_fn=slept.append,
        backoff_base=1.5,
        max_retries=2,
    )

    with pytest.raises(TwelveDataRateLimitError, match="rate limit exceeded"):
        client.fetch_time_series("MSFT", interval="1day")
    assert len(slept) == 2


def test_twelvedata_client_raises_api_error_on_400() -> None:
    def error_transport(url: str) -> tuple[int, bytes]:
        return 400, json.dumps({"code": 400, "message": "Invalid symbol", "status": "error"}).encode()

    limiter = TokenBucketRateLimiter(rate_per_minute=1000.0, capacity=1000.0)
    client = TwelveDataClient(
        api_key="test_key",
        rate_limiter=limiter,
        transport_fn=error_transport,
    )

    with pytest.raises(TwelveDataApiError, match="Twelve Data error 400"):
        client.fetch_time_series("INVALID", interval="1day")


def test_symbol_translation_for_vendor() -> None:
    queried_urls: list[str] = []

    def record_url(url: str) -> tuple[int, bytes]:
        queried_urls.append(url)
        return 200, json.dumps({"meta": {}, "values": [], "status": "ok"}).encode()

    limiter = TokenBucketRateLimiter(rate_per_minute=1000.0, capacity=1000.0)
    client = TwelveDataClient(
        api_key="test_key",
        rate_limiter=limiter,
        transport_fn=record_url,
    )

    client.fetch_time_series("BRK.B", interval="1day")
    assert len(queried_urls) == 1
    assert "symbol=BRK%2FB" in queried_urls[0] or "symbol=BRK/B" in queried_urls[0]


# -----------------------------------------------------------------------------
# Parsing Daily & Monthly Bars Tests
# -----------------------------------------------------------------------------


def test_parse_daily_bars() -> None:
    payload = {
        "values": [
            {
                "datetime": "2026-03-31",
                "open": "215.05",
                "high": "219.16",
                "low": "214.85",
                "close": "217.49",
                "volume": "8311700",
            },
            {
                "datetime": "2023-01-01",  # older than min_date
                "open": "150.00",
                "close": "152.00",
            },
        ]
    }
    records = parse_daily_bars("AAPL", payload, min_date=date(2024, 1, 1))
    assert len(records) == 1
    rec = records[0]
    assert rec.symbol == "AAPL"
    assert rec.date == date(2026, 3, 31)
    assert rec.open == Decimal("215.05")
    assert rec.high == Decimal("219.16")
    assert rec.low == Decimal("214.85")
    assert rec.close == Decimal("217.49")
    assert rec.volume == Decimal("8311700")
    assert rec.resolution == "1D"
    assert rec.source == "twelvedata"


def test_parse_monthly_bars_snaps_dates() -> None:
    payload = {
        "values": [
            {
                "datetime": "2024-03-01",
                "open": "180.00",
                "high": "185.00",
                "low": "178.00",
                "close": "182.50",
                "volume": "50000000",
            }
        ]
    }
    records = parse_monthly_bars("MSFT", payload)
    assert len(records) == 1
    rec = records[0]
    assert rec.symbol == "MSFT"
    # March 2024 must snap to 2024-03-28 (Good Friday March 29)
    assert rec.date == date(2024, 3, 28)
    assert rec.close == Decimal("182.50")
    assert rec.resolution == "1M"


# -----------------------------------------------------------------------------
# 20 TOPT Symbols 40 Calls Ingestion Test
# -----------------------------------------------------------------------------


def test_ingest_twelve_data_market_prices_issues_exactly_40_calls() -> None:
    calls_made: list[tuple[str, str]] = []

    def mock_transport(url: str) -> tuple[int, bytes]:
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        sym = params.get("symbol", [""])[0]
        interval = params.get("interval", [""])[0]
        outputsize = params.get("outputsize", [""])[0]
        assert outputsize == "5000"
        calls_made.append((sym, interval))

        return 200, json.dumps(
            {
                "values": [
                    {"datetime": "2026-03-31", "close": "100.0", "volume": "1000"},
                ],
                "status": "ok",
            }
        ).encode()

    limiter = TokenBucketRateLimiter(rate_per_minute=10000.0, capacity=10000.0)
    client = TwelveDataClient(
        api_key="test_key",
        rate_limiter=limiter,
        transport_fn=mock_transport,
    )

    assert len(DEFAULT_TOPT_SYMBOLS) == 20
    summary = ingest_twelve_data_market_prices(
        symbols=DEFAULT_TOPT_SYMBOLS,
        client=client,
        connection=None,
    )

    assert summary.total_symbols == 20
    assert summary.total_calls == 40
    assert len(calls_made) == 40

    # 1 call daily + 1 call monthly per symbol
    daily_calls = [c for c in calls_made if c[1] == "1day"]
    monthly_calls = [c for c in calls_made if c[1] == "1month"]
    assert len(daily_calls) == 20
    assert len(monthly_calls) == 20

    assert summary.daily_records_count == 20
    assert summary.monthly_records_count == 20


# -----------------------------------------------------------------------------
# Database Persistence Tests
# -----------------------------------------------------------------------------


def test_database_insert_and_upsert_idempotency() -> None:
    import psycopg
    from data_engine.config import settings

    try:
        conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("Postgres unreachable")

    try:
        daily_records = [
            PriceBarRecord(
                symbol="TEST.AAPL",
                date=date(2026, 3, 31),
                open=Decimal("215.00"),
                high=Decimal("219.00"),
                low=Decimal("214.00"),
                close=Decimal("217.50"),
                volume=Decimal("1000000"),
                source="twelvedata",
                resolution="1D",
            )
        ]
        monthly_records = [
            PriceBarRecord(
                symbol="TEST.AAPL",
                date=date(2026, 3, 31),
                open=Decimal("210.00"),
                high=Decimal("220.00"),
                low=Decimal("205.00"),
                close=Decimal("217.50"),
                volume=Decimal("20000000"),
                source="twelvedata",
                resolution="1M",
            )
        ]

        # Insert first time
        d_count = insert_market_prices_daily(conn, daily_records)
        m_count = insert_market_prices_monthly(conn, monthly_records)
        assert d_count == 1
        assert m_count == 1

        # Upsert with updated close price
        updated_daily = [
            PriceBarRecord(
                symbol="TEST.AAPL",
                date=date(2026, 3, 31),
                open=Decimal("215.00"),
                high=Decimal("219.00"),
                low=Decimal("214.00"),
                close=Decimal("218.00"),  # updated
                volume=Decimal("1000000"),
                source="twelvedata",
                resolution="1D",
            )
        ]
        insert_market_prices_daily(conn, updated_daily)

        with conn.cursor() as cur:
            cur.execute(
                "select close from staging.market_prices_daily where symbol = %s and date = %s",
                ("TEST.AAPL", date(2026, 3, 31)),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == Decimal("218.00")

    finally:
        with conn.cursor() as cur:
            cur.execute("delete from staging.market_prices_daily where symbol = 'TEST.AAPL'")
            cur.execute("delete from staging.market_prices_monthly where symbol = 'TEST.AAPL'")
        conn.close()
