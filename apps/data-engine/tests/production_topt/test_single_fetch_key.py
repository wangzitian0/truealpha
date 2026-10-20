"""Standing tests for #1411 / #1418: Single Fetch per Key & S&P 500 Data Plane (K1-K6)."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from data_engine.datahub.fetch_key import FetchKey, LandedKeyIndex
from data_engine.datahub.market_prices import (
    TwelveDataClient,
    ingest_twelve_data_market_prices,
    latest_closed_monthly_session,
    should_fetch_monthly_series,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    OperatingBranch,
    sec_financial_fetcher,
)
from data_engine.datahub.production_topt.source_registrations import (
    REGISTRATIONS,
    origin_per_key_cost,
)
from data_engine.datahub.production_topt.twelve_data_origin import TwelveDataQuoteFetcher
from data_engine.datahub.production_topt.universe_plane import (
    UNIVERSE_SOURCES,
    build_denominator,
)
from data_engine.datahub.standards.filing_extraction import (
    _FILING_CACHE_BY_CLIENT,
    latest_annual_filing,
)
from data_engine.lanes.qqq_prices import CREDITS_PER_SYMBOL
from data_engine.sources import moomoo_ledger as ledger

# --- K1: Capacity & per_key_cost / as_of_axis in registrations ------------------------


def test_k1_every_registration_declares_per_key_cost_and_as_of_axis() -> None:
    for reg in REGISTRATIONS:
        payload = reg.entry_payload()
        assert "per_key_cost" in payload and "as_of_axis" in payload
        assert reg.as_of_axis in ("settled_session", "accession", "release")
        if reg.corroboration_class != "release":
            assert reg.per_key_cost > 0
        for origin in reg.origins:
            assert origin.per_key_cost is not None and origin.per_key_cost > 0
            assert origin.as_of_axis in ("settled_session", "accession", "release")
    assert CREDITS_PER_SYMBOL == origin_per_key_cost("twelve-data:v1") == 2


# --- K2: moomoo monthly call budget deleted; 30d 2,000-stock quota & daily anomaly ---


def test_k2_moomoo_monthly_call_budget_deleted_and_30d_stock_quota_enforced(tmp_path, monkeypatch) -> None:
    assert not hasattr(ledger.settings, "moomoo_monthly_call_budget")
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "json")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger, "MOOMOO_KLINE_30D_STOCK_QUOTA", 2)

    ledger.gate("request_history_kline", "test", request_uri="moomoo://US.AAPL")
    ledger.record("request_history_kline", "test", ok=True, request_uri="moomoo://US.AAPL")
    ledger.gate("request_history_kline", "test", request_uri="moomoo://US.MSFT")
    ledger.record("request_history_kline", "test", ok=True, request_uri="moomoo://US.MSFT")

    # Re-requesting an already-used stock in the 30d window succeeds
    ledger.gate("request_history_kline", "test", request_uri="moomoo://US.AAPL")

    # Requesting a 3rd distinct stock raises BudgetExceededError
    with pytest.raises(ledger.BudgetExceededError, match="30d K-line distinct-stock quota exhausted"):
        ledger.gate("request_history_kline", "test", request_uri="moomoo://US.NVDA")

    # Daily call anomaly triggers only above 2x planned_p99
    assert ledger.is_daily_call_anomaly(planned_p99=1) is False
    ledger.record("get_company_profile", "test", ok=True, request_uri="moomoo://US.NVDA")
    assert ledger.is_daily_call_anomaly(planned_p99=1) is True


# --- K3: Canonical FetchKey and LandedKeyIndex cache-aside ----------------------------


def test_k3_fetch_key_and_landed_key_index_deduplicate_calls() -> None:
    index = LandedKeyIndex()
    key = FetchKey(
        seat="sec",
        endpoint="companyfacts",
        canonical_entity_id="issuer:cik:0001652044",
        as_of="2026-10-16",
    )
    calls = 0

    def _vendor_call() -> bytes:
        nonlocal calls
        calls += 1
        return b'{"cik":1652044}'

    first = index.get_or_fetch(key, _vendor_call)
    second = index.get_or_fetch(key, _vendor_call)
    assert first == second == b'{"cik":1652044}'
    assert calls == 1
    assert index.hits == 1 and index.misses == 1


# --- K4: One price store (D1) & closed-month-only monthly fetch (D2) ------------------


def test_k4_d1_twelve_data_quote_fetcher_reads_staging_market_prices_daily_without_http(monkeypatch) -> None:
    """When staging.market_prices_daily already holds the settled session's unadjusted bar,
    TwelveDataQuoteFetcher returns it with 0 Twelve Data HTTP calls (#1411 K4 D1)."""
    http_calls: list[str] = []
    monkeypatch.setattr(
        "data_engine.sources.gateway.urlopen",
        lambda *_a, **_k: http_calls.append("called") or (200, b"{}"),
    )

    class _FakeCursor:
        def fetchone(self):
            return (
                date(2026, 10, 16),
                Decimal("225.10"),
                Decimal("228.50"),
                Decimal("224.00"),
                Decimal("227.55"),
                Decimal("42000000"),
                datetime(2026, 10, 16, 20, 0, tzinfo=UTC),
            )

    class _FakeConnection:
        def execute(self, sql: str, params: tuple[Any, ...]):
            assert "staging.market_prices_daily" in sql
            assert params == ("AAPL", date(2026, 10, 16))
            return _FakeCursor()

    fetcher = TwelveDataQuoteFetcher("test-key", throttle_seconds=0, connection=_FakeConnection())
    quote = fetcher("AAPL", date(2026, 10, 16))
    assert quote is not None
    assert quote.close == Decimal("227.55")
    assert quote.open == Decimal("225.10")
    assert quote.as_of == date(2026, 10, 16)
    assert http_calls == [], "must make 0 Twelve Data HTTP calls when bar is in staging.market_prices_daily"


def test_k4_d2_monthly_series_skipped_mid_month_when_prior_closed_month_is_stored() -> None:
    """Mid-month weekday run when the prior closed month-end bar is already stored makes 0 1month calls (#1411 K4 D2)."""
    mid_month_now = datetime(2026, 10, 14, 21, 15, tzinfo=UTC)
    closed_month_end = latest_closed_monthly_session(mid_month_now)
    assert closed_month_end == date(2026, 9, 30)

    class _StoredCursor:
        def __init__(self) -> None:
            self._last_query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
            self._last_query = query

        def fetchone(self):
            if "staging.market_prices_monthly" in self._last_query:
                return (1,)
            return None

        def fetchall(self):
            return []

        def executemany(self, _query: str, _rows: Any) -> None:
            pass

    class _StoredConnection:
        def cursor(self):
            return _StoredCursor()

    assert should_fetch_monthly_series(_StoredConnection(), "AAPL", now=mid_month_now) is False

    intervals_called: list[str] = []

    def transport(url: str) -> tuple[int, bytes]:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        intervals_called.append(query["interval"])
        return 200, json.dumps({"values": []}).encode()

    client = TwelveDataClient(api_key="test", transport_fn=transport, sleep_fn=lambda _s: None)
    summary = ingest_twelve_data_market_prices(
        symbols=["AAPL"],
        client=client,
        connection=_StoredConnection(),  # type: ignore[arg-type]
        now=mid_month_now,
    )
    assert "1month" not in intervals_called
    assert summary.total_calls == 2


# --- K5: SEC companyfacts & 10-K deduplication (D3) -----------------------------------


def test_k5_d3_dual_class_shares_share_one_companyfacts_fetch(monkeypatch) -> None:
    """GOOG and GOOGL share CIK 0001652044 and make 1 companyfacts call per run, not 2 (#1411 K5 D3)."""
    calls: list[int] = []
    sample_facts = {
        "cik": 1652044,
        "entityName": "Alphabet Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 350000000000,
                                "accn": "0001652044-26-000010",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-04",
                            }
                        ]
                    }
                }
            }
        },
    }
    raw_bytes = json.dumps(sample_facts).encode()

    def _fake_fetch(cik: int, http: Any = None) -> tuple[bytes, dict[str, Any]]:
        calls.append(cik)
        return raw_bytes, sample_facts

    monkeypatch.setattr("data_engine.sources.sec.fetch_company_facts_response", _fake_fetch)
    monkeypatch.setattr(
        "data_engine.datahub.production_topt.sec_financial_adapter._enrich_multi_class_shares",
        lambda *_a, **_k: None,
    )

    raw_cache: dict[int, tuple[bytes, dict[str, Any]]] = {}
    bundle_googl = sec_financial_fetcher(
        1652044, date(2026, 10, 16), OperatingBranch.NON_FINANCIAL, raw_cache=raw_cache
    )
    bundle_goog = sec_financial_fetcher(1652044, date(2026, 10, 16), OperatingBranch.NON_FINANCIAL, raw_cache=raw_cache)
    assert calls == [1652044], "GOOG and GOOGL must share a single SEC companyfacts call"
    assert bundle_googl.revenue == bundle_goog.revenue


def test_k5_d3_headcount_and_segments_share_one_10k_fetch(monkeypatch) -> None:
    """Two calls to latest_annual_filing for the same (cik, cutoff) on one client make 1 submissions + 1 archive call."""
    _FILING_CACHE_BY_CLIENT.clear()
    endpoints_called: list[str] = []

    def _fake_json(_http: Any, _gw: Any, endpoint: str, _url: str) -> dict[str, Any]:
        endpoints_called.append(endpoint)
        return {
            "filings": {
                "recent": {
                    "form": ["10-K"],
                    "filingDate": ["2026-02-01"],
                    "accessionNumber": ["0000320193-26-000010"],
                    "primaryDocument": ["aapl-20250927.htm"],
                }
            }
        }

    def _fake_bytes(_http: Any, _gw: Any, endpoint: str, _url: str) -> bytes:
        endpoints_called.append(endpoint)
        return b"<html><body>We had 164,000 full-time equivalent employees.</body></html>"

    monkeypatch.setattr("data_engine.datahub.standards.filing_extraction._fetch_json_via_gateway", _fake_json)
    monkeypatch.setattr("data_engine.datahub.standards.filing_extraction._fetch_bytes_via_gateway", _fake_bytes)

    fake_http = object()
    cutoff = datetime(2026, 10, 16, 22, 0, tzinfo=UTC)
    doc1 = latest_annual_filing(320193, http=fake_http, gateway=None, cutoff=cutoff)  # type: ignore[arg-type]
    doc2 = latest_annual_filing(320193, http=fake_http, gateway=None, cutoff=cutoff)  # type: ignore[arg-type]
    assert doc1 is not None and doc1 == doc2
    assert endpoints_called == ["submissions", "archive"]


# --- K6: Correct listing identity for S&P 500 members (D4) ----------------------------


def test_k6_d4_sp500_resolves_aapl_to_xnas_and_jpm_to_xnys() -> None:
    """AAPL in sp500 resolves to listing:xnas:aapl (matching qqq and topt), while JPM resolves to listing:xnys:jpm."""

    class _FakeResult:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self._rows

    class _FakeTx:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: Any) -> None:
            return None

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        def execute(self, sql: str, _params: tuple[Any, ...] = ()) -> _FakeResult:
            if "from staging.etf_constituent_facts" in sql and "select ticker, cik, figi" in sql:
                return _FakeResult(
                    [
                        ("AAPL", 320193, "bbg001s5n8v8"),
                        ("JPM", 19617, "bbg001s8x7p0"),
                    ]
                )
            if "etf_symbol = 'qqq'" in sql:
                return _FakeResult([("AAPL",)])
            if "from staging.entity_aliases" in sql:
                return _FakeResult([])
            return _FakeResult([])

    denominator = build_denominator(
        _FakeConn(),  # type: ignore[arg-type]
        UNIVERSE_SOURCES["sp500"],
        report_date=date(2026, 10, 16),
    )
    by_ticker = {row[3]: row[2] for row in denominator["instruments"]}
    assert by_ticker["AAPL"] == "listing:xnas:aapl"
    assert by_ticker["JPM"] == "listing:xnys:jpm"
