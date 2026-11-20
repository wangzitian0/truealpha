"""S&P 500 Yahoo daily & split series ingestion, zero-duplicate FetchKey, and bounded share-basis WARNs (#1661, #1606)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from data_engine.contracts.datahub import CaptureWorkItem
from data_engine.datahub.capture_core import market_price_adapter
from data_engine.datahub.capture_core.executor import FetchSuccess
from data_engine.datahub.capture_core.materialization import (
    FinancialFactPayload,
    MarketPricePayload,
    PostgresToptCoreRepository,
    _ObservationRow,
)
from data_engine.datahub.capture_core.source_registrations import RouteCell, RouteContext
from data_engine.datahub.market_prices import (
    SPLIT_ADJUSTED,
    UNADJUSTED,
    PriceBarRecord,
    ingest_yahoo_daily_series,
    insert_market_prices_daily,
)
from data_engine.datahub.share_basis import split_history
from data_engine.sources import gateway, yahoo
from factors.backtest.corporate_actions import SplitEvent
from factors.snapshot_inputs import MetricFreshness, OperatingBranch

NVDA_SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "events" / "NVDA_YAHOO_EVENTS_20240501_20240701.json"


class _FakeCursor:
    def __init__(self, conn: _FakePriceConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self._conn.executed.append((sql, params))
        if "select distinct on (symbol, trading_date, adjust)" in sql:
            symbols, dates = set(params[0]), set(params[1])
            self._rows = [
                (sym, day, adj, *vals)
                for (sym, day, adj), vals in self._conn.stored.items()
                if sym in symbols and day in dates
            ]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self._conn.inserted.append((sql, list(rows)))
        for sym, day, open_, high, low, close, vol, src, adj, _tx, conf, raw_ref in rows:
            self._conn.stored[(sym, day, adj)] = (open_, high, low, close, vol, src)
            self._conn.metadata[(sym, day, adj)] = (conf, raw_ref)


class _FakePriceConnection:
    def __init__(self) -> None:
        self.stored: dict[tuple[str, date, str], tuple[Any, Any, Any, Any, Any, str]] = {}
        self.metadata: dict[tuple[str, date, str], tuple[Decimal, str]] = {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.inserted: list[tuple[str, list[tuple[Any, ...]]]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_nvda_captured_yahoo_events_parse_split_and_unadjusted_closes() -> None:
    """Real captured Yahoo NVDA sample (10:1 split on 2024-06-10) reconstructs exact unadjusted + split series."""
    payload = json.loads(NVDA_SAMPLE.read_bytes().decode("utf-8"), parse_float=Decimal)
    split_adj, unadj = yahoo.parse_yahoo_daily_series("NVDA", payload, now=datetime(2024, 7, 2, tzinfo=UTC))

    split_adj_by_day = {r.date: (r.open, r.close) for r in split_adj}
    unadj_by_day = {r.date: (r.open, r.close) for r in unadj}
    pre_split_day = date(2024, 6, 7)
    post_split_day = date(2024, 6, 10)

    assert unadj_by_day[pre_split_day][1] == Decimal("1208.88")
    assert split_adj_by_day[pre_split_day][1] == Decimal("120.888")
    assert unadj_by_day[post_split_day][1] == Decimal("121.79")
    assert split_adj_by_day[post_split_day][1] == Decimal("121.79")

    history = split_history("NVDA", {"NVDA": unadj_by_day}, {"NVDA": split_adj_by_day})
    assert not isinstance(history, type(None)) and hasattr(history, "events")
    assert history.events == (SplitEvent("NVDA", post_split_day, 10, 1),)


def test_yahoo_daily_series_assigns_low_confidence_preserves_dots_and_never_overwrites_twelvedata() -> None:
    """Yahoo rows carry single-source Low confidence, preserve 'BRK.B', and skip overwriting Twelve Data bars."""
    conn = _FakePriceConnection()
    now = datetime(2026, 4, 1, 22, 0, tzinfo=UTC)
    td_day = date(2026, 3, 31)
    aapl_td = [
        PriceBarRecord(
            symbol="AAPL",
            date=td_day,
            open=Decimal("200.00"),
            high=Decimal("201.00"),
            low=Decimal("199.00"),
            close=Decimal("200.50"),
            volume=Decimal("1000"),
            source="twelvedata",
        )
    ]
    assert insert_market_prices_daily(conn, aapl_td, adjust=SPLIT_ADJUSTED) == 1  # type: ignore[arg-type]
    assert insert_market_prices_daily(conn, aapl_td, adjust=UNADJUSTED) == 1  # type: ignore[arg-type]
    td_conf = conn.metadata[("AAPL", td_day, SPLIT_ADJUSTED)][0]

    chart_bytes = json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "timestamp": [
                            int(datetime(2026, 3, 30, 20, 0, tzinfo=UTC).timestamp()),
                            int(datetime(2026, 3, 31, 20, 0, tzinfo=UTC).timestamp()),
                        ],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [198.0, 199.0],
                                    "high": [199.0, 200.0],
                                    "low": [197.0, 198.0],
                                    "close": [198.5, 199.5],
                                    "volume": [100, 200],
                                }
                            ]
                        },
                    }
                ]
            }
        }
    ).encode()

    res_aapl = ingest_yahoo_daily_series(conn, "AAPL", chart_bytes, now=now)  # type: ignore[arg-type]
    assert res_aapl == {SPLIT_ADJUSTED: 1, UNADJUSTED: 1}  # Only 2026-03-30 added; 2026-03-31 Twelve Data kept!
    assert conn.stored[("AAPL", td_day, SPLIT_ADJUSTED)][5] == "twelvedata"
    assert conn.stored[("AAPL", td_day, SPLIT_ADJUSTED)][3] == Decimal("200.50")

    yahoo_conf = conn.metadata[("AAPL", date(2026, 3, 30), SPLIT_ADJUSTED)][0]
    assert Decimal("0") < yahoo_conf < td_conf

    res_brk = ingest_yahoo_daily_series(conn, "BRK.B", chart_bytes, now=now)  # type: ignore[arg-type]
    assert res_brk == {SPLIT_ADJUSTED: 2, UNADJUSTED: 2}
    assert ("BRK.B", td_day, SPLIT_ADJUSTED) in conn.stored
    assert conn.metadata[("BRK.B", td_day, SPLIT_ADJUSTED)][1].startswith("yahoo:chart:BRK.B:1D:")


def test_cross_universe_route_fetches_each_yahoo_key_once_and_sp500_spends_zero_twelvedata(
    landed_keys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOPT -> QQQ -> S&P 500 share one FetchKey per (symbol, settled_date); S&P 500 populates daily bars with 0 TD calls."""
    cutoff = date(2026, 3, 31)
    run_cutoff = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
    monkeypatch.setattr(gateway, "utc_day", lambda: "2026-04-01")
    monkeypatch.setattr(gateway, "_utcnow", lambda: run_cutoff)

    chart_payload = json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [
                            int(datetime(2026, 3, 30, 20, 0, tzinfo=UTC).timestamp()),
                            int(datetime(2026, 3, 31, 20, 0, tzinfo=UTC).timestamp()),
                        ],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [400.0, 405.0],
                                    "high": [401.0, 406.0],
                                    "low": [399.0, 404.0],
                                    "close": [400.5, 405.5],
                                    "volume": [10, 20],
                                }
                            ]
                        },
                    }
                ]
            }
        }
    ).encode()

    requested_urls: list[str] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        assert request.url.params.get("events") == "splits"
        return httpx.Response(200, content=chart_payload)

    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)})
    )

    from data_engine.datahub.capture_core import moomoo_origin, twelve_data_origin

    td_calls: list[str] = []
    monkeypatch.setattr(
        twelve_data_origin,
        "twelve_data_origin",
        lambda **_k: market_price_adapter.CorroboratingOrigin(
            origin="twelve-data",
            parser_version="twelve-data:v1",
            mapping_version="m1",
            value_key="close",
            fetch=lambda sym, _c: td_calls.append(sym) or None,
        ),
    )
    monkeypatch.setattr(moomoo_origin, "moomoo_kline_origin", lambda: None)

    price_conn = _FakePriceConnection()

    def _ctx(universe_id: str) -> RouteContext:
        return RouteContext(
            cutoff=run_cutoff,
            cutoff_date=cutoff,
            price_cutoff_date=cutoff,
            partition_start=datetime(2026, 3, 31, tzinfo=UTC),
            universe_published_at=None,
            coordinates={},
            connection=price_conn,
            universe_id=universe_id,
        )

    def _work_item(digest_char: str) -> CaptureWorkItem:
        return CaptureWorkItem(
            campaign_id="capture-campaign:" + "1" * 64,
            source_request_id="source-request:" + (digest_char * 64),
            schedule_policy_id="schedule-policy:" + "2" * 64,
        )

    for digest_char, universe_id, ticker in (
        ("a", "universe:topt-us-2026-03-31", "AAPL"),
        ("b", "universe:qqq-us-2026-03-31", "AAPL"),
        ("c", "universe:sp500-us-2026-03-31", "AAPL"),
        ("d", "universe:sp500-us-2026-03-31", "BRK.B"),
    ):
        item = _work_item(digest_char)
        adapter = market_price_adapter.build_route(
            _ctx(universe_id),
            [RouteCell(item.work_item_id, "market-price", "issuer:1", "sec:1", f"listing:x:{ticker}", ticker)],
        )
        res = adapter.fetch(item)
        assert isinstance(res, FetchSuccess)

    assert sum("AAPL" in url for url in requested_urls) == 1
    assert sum("BRK-B" in url for url in requested_urls) == 1
    assert td_calls == ["AAPL", "AAPL"]  # TOPT + QQQ only; S&P 500 spends 0 Twelve Data calls!
    assert ("AAPL", date(2026, 3, 31), SPLIT_ADJUSTED) in price_conn.stored
    assert ("BRK.B", date(2026, 3, 31), UNADJUSTED) in price_conn.stored


def test_share_basis_missing_warning_bounded_once_per_issuer_reason_per_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1606: missing split series warns once per (issuer, reason) per run while keeping shares NULL and recording reason."""
    now = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
    warned: set[tuple[str, str]] = set()
    financial = FinancialFactPayload(
        issuer_id="issuer:cik:0001652044",
        instrument_id="security:goog",
        listing_id="listing:xnas:goog",
        operating_branch=OperatingBranch.NON_FINANCIAL,
        currency="USD",
        gross_profit=Decimal("100"),
        total_assets=Decimal("200"),
        headcount=Decimal("10"),
        revenue=Decimal("300"),
        shares_outstanding=Decimal("1000"),
        shares_period_end=date(2026, 2, 1),
        listing_units_per_share=Decimal("1"),
        pre_provision_profit=None,
    )
    caplog.set_level(logging.WARNING, logger="data_engine.datahub.capture_core.materialization")
    for lid in ("listing:xnas:goog", "listing:xnas:googl"):
        fin_row = _ObservationRow(
            obligation_id=f"ob:{lid}:f",
            listing_id=lid,
            semantic_type="financial-fact",
            observation_id=f"obs:{lid}:f",
            freshness=MetricFreshness.FRESH,
            knowable_at=now,
            payload={"issuer_id": "issuer:cik:0001652044"},
        )
        mkt_row = _ObservationRow(
            obligation_id=f"ob:{lid}:m",
            listing_id=lid,
            semantic_type="market-price",
            observation_id=f"obs:{lid}:m",
            freshness=MetricFreshness.FRESH,
            knowable_at=now,
            payload={},
        )
        price = MarketPricePayload(
            issuer_id="issuer:cik:0001652044",
            instrument_id="security:goog",
            listing_id=lid,
            currency="USD",
            close=Decimal("150"),
        )
        val, _ts, reason = PostgresToptCoreRepository._resolve_shares_on_close_basis(
            lid,
            financial=financial,
            financial_row=fin_row,
            market_row=mkt_row,
            price=price,
            price_value=Decimal("150"),
            shares_cell=None,
            histories=(),
            documents=None,
            ruleset=None,
            stores_price_series=True,
            warned_share_reasons=warned,
        )
        assert val is None
        assert reason is not None and reason.value == "no_split_adjusted_series"

    share_warnings = [rec.message for rec in caplog.records if "share basis:" in rec.message]
    assert len(share_warnings) == 1
