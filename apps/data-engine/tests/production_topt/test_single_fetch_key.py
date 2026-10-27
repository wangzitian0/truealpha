"""Standing tests for #1411 / #1418: Single Fetch per Key & S&P 500 Data Plane (K1-K6)."""

from __future__ import annotations

import ast
import json
import logging
import urllib.parse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from data_engine.datahub.fetch_key import FetchKey
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
from data_engine.datahub.standards.filing_extraction import latest_annual_filing
from data_engine.lanes.qqq_prices import CREDITS_PER_SYMBOL
from data_engine.sources import gateway, sec
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


@pytest.fixture(autouse=True)
def _fixed_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys that use the UTC day as their as-of must not change when a test crosses 00:00 UTC."""
    monkeypatch.setattr(gateway, "utc_day", lambda: "2026-10-16")


_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "data_engine"
#: The one module that may write a per-key cost: the source registry.
_REGISTRY = "source_registrations.py"
#: A name that carries the price of one key. The model seat spends `cost=0` tokens, never credits.
_COST_NAME_MARKERS = ("per_key_cost", "credits_per", "cost_per_key", "cost_per_symbol")
_REQUEST_FUNCTIONS = {"http_get", "http_post", "urlopen", "record_call"}


def per_key_cost_literals(source: str, filename: str) -> list[str]:
    """Every place that `source` writes a per-key cost as a number instead of reading the registry.

    Three shapes: an assignment to a name that carries a per-key cost (``CREDITS_PER_SYMBOL = 2``),
    a keyword argument of that name, and a request through the gateway with ``cost=`` set to a number
    above zero. ``cost=0`` is the model seat, which spends tokens and no quota."""
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if node.value is not None and _is_number(node.value):
                findings += [f"{filename}:{node.lineno} {n} = literal" for n in names if _carries_cost(n)]
        if isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for keyword in node.keywords:
                if keyword.arg is None or not _is_number(keyword.value):
                    continue
                if _carries_cost(keyword.arg) or (
                    keyword.arg == "cost" and callee in _REQUEST_FUNCTIONS and keyword.value.value != 0
                ):
                    findings.append(f"{filename}:{node.lineno} {callee}({keyword.arg}=literal)")
    return findings


def _is_number(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool)


def _carries_cost(name: str) -> bool:
    return any(marker in name.lower() for marker in _COST_NAME_MARKERS)


def test_k1_the_checker_names_a_per_key_cost_literal_and_accepts_a_registry_read() -> None:
    """Falsifiable guard (#1411 K1): the scan fails on a literal and passes on a derived value."""
    literal = "CREDITS_PER_SYMBOL = 2\nclient.get(cost=2)\nurlopen(a, b, c, caller='x', timeout=1, cost=2)\n"
    derived = "CREDITS_PER_SYMBOL = origin_per_key_cost('twelve-data:v1')\nurlopen(a, b, c, caller='x', cost=0)\n"
    assert per_key_cost_literals(literal, "lane.py") == [
        "lane.py:1 CREDITS_PER_SYMBOL = literal",
        "lane.py:3 urlopen(cost=literal)",
    ]
    assert per_key_cost_literals(derived, "lane.py") == []


def test_k1_no_module_outside_the_source_registry_holds_a_per_key_cost_literal() -> None:
    """Every per-key cost is read from `source_registrations` (#1411 K1). Red on a lane constant of 2."""
    scanned = [path for path in sorted(_SOURCE_ROOT.rglob("*.py")) if path.name != _REGISTRY]
    assert len(scanned) > 50, "the scan found too few modules; the source root moved"
    findings = [
        finding
        for path in scanned
        for finding in per_key_cost_literals(path.read_text(encoding="utf-8"), str(path.relative_to(_SOURCE_ROOT)))
    ]
    assert findings == []


# --- K2: moomoo monthly call budget deleted; 30d 2,000-stock quota & daily anomaly ---


def test_k2_moomoo_monthly_call_budget_deleted_and_30d_stock_quota_enforced(moomoo_ledger_db, monkeypatch) -> None:
    from data_engine.sources import moomoo as mm

    def spend(endpoint: str, code: str) -> None:
        mm._call(object(), endpoint, "test", lambda ctx: (mm.moomoo.RET_OK, []), request_uri=code)

    assert not hasattr(ledger.settings, "moomoo_monthly_call_budget")
    monkeypatch.setattr(ledger, "MOOMOO_KLINE_30D_STOCK_QUOTA", 2)

    spend("request_history_kline", "US.AAPL")
    spend("request_history_kline", "US.MSFT")

    # Re-requesting an already-used stock in the 30d window succeeds
    ledger.gate("request_history_kline", "test", request_uri="moomoo://US.AAPL")

    # Requesting a 3rd distinct stock raises BudgetExceededError
    with pytest.raises(ledger.BudgetExceededError, match="30d K-line distinct-stock quota exhausted"):
        ledger.gate("request_history_kline", "test", request_uri="moomoo://US.NVDA")

    # Daily call anomaly triggers only above 2x planned_p99
    assert ledger.is_daily_call_anomaly(planned_p99=1) is False
    spend("get_company_profile", "US.NVDA")
    assert ledger.is_daily_call_anomaly(planned_p99=1) is True


# --- K3: the gateway serves a landed key with no vendor call -------------------------


def _socket(counter: list[str]) -> httpx.Client:
    """An HTTP client over a fake socket that records every request it receives."""

    def handler(request: httpx.Request) -> httpx.Response:
        counter.append(str(request.url))
        return httpx.Response(200, content=b'{"cik":1652044}')

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_k3_a_second_fetch_of_a_landed_key_makes_no_vendor_call_and_records_a_reuse(call_ledger, landed_keys) -> None:
    key = FetchKey(
        seat="sec",
        endpoint="companyfacts",
        canonical_entity_id="issuer:cik:0001652044",
        as_of="2026-10-16",
    )
    socket: list[str] = []
    client = _socket(socket)

    def fetch() -> bytes:
        response = gateway.http_get(client, "sec", "companyfacts", "https://data.sec.gov/x", caller="test")
        return bytes(response.content)

    first = gateway.fetch_once(key, fetch)
    second = gateway.fetch_once(key, fetch)

    assert first == second == b'{"cik":1652044}'
    assert socket == ["https://data.sec.gov/x"], "the second fetch reached the vendor socket"
    assert [row.fetch_key for row in call_ledger] == [key.ledger_key], "one ledger row, and it names the key"
    assert landed_keys.reuses == {key: 1}


def test_k3_a_failed_fetch_lands_nothing_so_the_next_caller_asks_again(call_ledger, landed_keys) -> None:
    key = FetchKey("sec", "companyfacts", "issuer:cik:0000000001", "2026-10-16")
    attempts: list[int] = []

    def fetch() -> bytes:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("refused")
        return b"{}"

    with pytest.raises(httpx.ConnectError):
        gateway.fetch_once(key, fetch)
    assert gateway.fetch_once(key, fetch) == b"{}"
    assert len(attempts) == 2 and landed_keys.bodies == {key: b"{}"}


def test_k3_a_window_that_is_not_final_is_asked_each_time_without_a_key(call_ledger, landed_keys) -> None:
    key = FetchKey("yahoo", "chart", "symbol:AAPL", "2026-10-16")
    socket: list[str] = []
    client = _socket(socket)

    def fetch() -> bytes:
        return bytes(gateway.http_get(client, "yahoo", "chart", "https://q.example/x", caller="test").content)

    gateway.fetch_once(key, fetch, keep=False)
    gateway.fetch_once(key, fetch, keep=False)

    assert len(socket) == 2 and landed_keys.bodies == {}
    assert [row.fetch_key for row in call_ledger] == [None, None], "an unfinished window is not a counted key"


def test_k3_an_answer_that_the_caller_refuses_stays_out_of_the_index_and_its_row_carries_no_key(
    call_ledger, landed_keys
) -> None:
    """A refused answer is asked again on purpose, so it is not a counted fetch of the key."""
    key = FetchKey("twelvedata", "eod", "symbol:AAPL", "2026-10-16")
    answers = [b'{"status":"error"}', b'{"close":"1"}']
    socket: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        socket.append(str(request.url))
        return httpx.Response(200, content=answers.pop(0))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fetch() -> bytes:
        return bytes(gateway.http_get(client, "twelvedata", "eod", "https://q.example/x", caller="test").content)

    accept = lambda body: b"error" not in body  # noqa: E731 - the one rule under test
    assert gateway.fetch_once(key, fetch, accept=accept) == b'{"status":"error"}'
    assert landed_keys.bodies == {}
    assert gateway.fetch_once(key, fetch, accept=accept) == b'{"close":"1"}'
    assert landed_keys.bodies == {key: b'{"close":"1"}'}
    assert [row.fetch_key for row in call_ledger] == [None, key.ledger_key]
    assert len(socket) == 2


def test_k3_a_probe_asks_the_vendor_again_carries_no_key_and_leaves_the_index_alone(call_ledger, landed_keys) -> None:
    """A forced tick proves that the release reaches its vendors (`release_fetch_proof`)."""
    key = FetchKey("sec", "companyfacts", "issuer:cik:0000320193", "2026-10-16")
    landed_keys.bodies[key] = b'{"landed":"yesterday"}'
    socket: list[str] = []
    client = _socket(socket)

    def fetch() -> bytes:
        return bytes(gateway.http_get(client, "sec", "companyfacts", "https://data.sec.gov/x", caller="test").content)

    with gateway.probe_scope(True):
        assert gateway.fetch_once(key, fetch) == b'{"cik":1652044}'
        assert gateway.landed(key) is None
        gateway.land(key, b"ignored")
    assert socket == ["https://data.sec.gov/x"], "the probe must reach the vendor socket"
    assert [row.fetch_key for row in call_ledger] == [None]
    assert landed_keys.bodies == {key: b'{"landed":"yesterday"}'} and landed_keys.reuses == {}
    # Outside the probe the landed key is served again, with no request.
    assert gateway.fetch_once(key, fetch) == b'{"landed":"yesterday"}' and len(socket) == 1


def test_k3_a_probe_stamps_no_key_on_the_paths_that_call_fetching_directly(call_ledger, landed_keys) -> None:
    """Twelve Data calls `landed`, `fetching` and `land`; every row, moomoo's included, reads the stamp.

    Red when `fetching` stamps inside a probe: the rows of a forced tick would then count as fetches."""
    key = FetchKey("twelvedata", "eod", "symbol:AAPL", "2026-10-16")
    socket: list[str] = []
    client = _socket(socket)

    def call() -> None:
        gateway.http_get(client, "twelvedata", "eod", "https://q.example/x", caller="test")

    with gateway.probe_scope(True):
        with gateway.fetching(key):
            call()
        gateway.land(key, b"ignored")
    assert [row.fetch_key for row in call_ledger] == [None] and landed_keys.bodies == {}
    with gateway.fetching(key):
        call()
    call()
    assert [row.fetch_key for row in call_ledger] == [None, key.ledger_key, None], "the stamp ends with the block"


def test_k3_an_accept_rule_that_raises_refuses_the_answer_and_says_so(call_ledger, landed_keys, caplog) -> None:
    """A rule that cannot read a valid answer must not stay silent: it would unkey every call of a seat."""
    key = FetchKey("yahoo", "chart", "symbol:AAPL", "2026-10-16")
    client = _socket([])

    def fetch() -> bytes:
        return bytes(gateway.http_get(client, "yahoo", "chart", "https://q.example/x", caller="test").content)

    def broken(_body: bytes) -> bool:
        raise RuntimeError("calendar unavailable")

    with caplog.at_level(logging.ERROR, logger="data_engine.sources.gateway"):
        assert gateway.fetch_once(key, fetch, accept=broken) == b'{"cik":1652044}'
    assert "the accept rule of a fetch raised" in caplog.text
    assert landed_keys.bodies == {} and [row.fetch_key for row in call_ledger] == [None]


def test_k5_the_standards_crosswalk_read_asks_the_index_before_it_admits_a_call(landed_keys) -> None:
    """`ticker_cik_index(http, via=gateway)`: the admission and the ledger row happen only on a miss."""
    calls: list[str] = []

    class StandardsGateway:
        def call(self, source: str, endpoint: str, fn: Any) -> Any:
            calls.append(f"{source}/{endpoint}")
            return fn()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"0":{"ticker":"AAPL","cik_str":320193}}')

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert sec.ticker_cik_index(http, via=StandardsGateway()) == {"AAPL": 320193}  # type: ignore[arg-type]
    assert sec.ticker_cik_index(http, via=StandardsGateway()) == {"AAPL": 320193}  # type: ignore[arg-type]
    assert calls == ["sec/company_tickers"], "the second read must not be admitted as a call"


# --- Yahoo: a chart lands only when its session is final and its bar is in it -----------


def _chart(*days: str) -> bytes:
    stamps = [int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()) for day in days]
    closes = [40.0] * len(days)
    quote = {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(days)}
    result = {"timestamp": stamps, "indicators": {"quote": [quote], "adjclose": [{"adjclose": closes}]}}
    return json.dumps({"chart": {"result": [result]}}).encode()


@pytest.fixture
def yahoo_socket(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """The chart answers a fake Yahoo gives, in order; the list of answers left is the test's script."""
    answers: list[bytes] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=answers.pop(0))

    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)})
    )
    return answers


def _fetch_chart(end: date, now: datetime) -> bytes:
    from data_engine.sources import yahoo

    gateway_now = gateway._utcnow
    gateway._utcnow = lambda: now  # type: ignore[assignment]
    try:
        return yahoo.fetch_daily_chart("AAPL", end=end)[0]
    finally:
        gateway._utcnow = gateway_now  # type: ignore[assignment]


def test_k3_yahoo_lands_a_closed_session_that_has_its_bar_and_serves_the_second_reader(
    yahoo_socket, landed_keys
) -> None:
    yahoo_socket.append(_chart("2026-03-30", "2026-03-31"))
    after_close = datetime(2026, 3, 31, 23, 0, tzinfo=UTC)
    first = _fetch_chart(date(2026, 3, 31), after_close)
    second = _fetch_chart(date(2026, 3, 31), after_close)
    assert first == second and yahoo_socket == [], "the second reader must not ask Yahoo"
    assert len(landed_keys.bodies) == 1


def test_k3_yahoo_does_not_land_a_chart_before_the_close_of_its_session(yahoo_socket, landed_keys) -> None:
    """The bar of an open session moves (#637). A smoke at 14:00 UTC must not feed the 22:15 UTC tick."""
    yahoo_socket.extend([_chart("2026-03-30", "2026-03-31")] * 2)
    midday = datetime(2026, 3, 31, 14, 0, tzinfo=UTC)
    _fetch_chart(date(2026, 3, 31), midday)
    _fetch_chart(date(2026, 3, 31), midday)
    assert yahoo_socket == [] and landed_keys.bodies == {}, "both reads asked Yahoo, and none landed"


def test_k3_yahoo_does_not_land_a_closed_session_whose_bar_the_vendor_has_not_published(
    yahoo_socket, landed_keys, call_ledger
) -> None:
    """Yahoo can lag after the close. The chart then ends one session early and must be asked again.

    The first, refused request is a request that is asked again on purpose: its row has no key, so
    `fetch_keys_once` does not count the second request as a duplicate."""
    yahoo_socket.extend([_chart("2026-03-27", "2026-03-30"), _chart("2026-03-30", "2026-03-31")])
    after_close = datetime(2026, 3, 31, 23, 0, tzinfo=UTC)
    _fetch_chart(date(2026, 3, 31), after_close)
    assert landed_keys.bodies == {}
    _fetch_chart(date(2026, 3, 31), after_close)
    assert len(landed_keys.bodies) == 1 and yahoo_socket == []
    assert [row.fetch_key is None for row in call_ledger] == [True, False]


def test_k3_yahoo_does_not_land_a_window_that_ends_after_today(yahoo_socket, landed_keys) -> None:
    """A future `end` has no session to settle: whatever the vendor answers is not final."""
    from data_engine.sources import yahoo

    now = datetime(2026, 3, 31, 23, 0, tzinfo=UTC)
    gateway_now = gateway._utcnow
    gateway._utcnow = lambda: now  # type: ignore[assignment]
    try:
        assert yahoo._session_is_final(_chart("2026-03-31"), date(2026, 4, 4)) is False  # a Saturday
        assert yahoo._session_is_final(_chart("2026-03-31"), date(2026, 3, 31)) is True
    finally:
        gateway._utcnow = gateway_now  # type: ignore[assignment]


def test_k5_the_vendor_oracle_reads_the_company_facts_that_the_adapter_landed(monkeypatch) -> None:
    """One resource, one key: the oracle and the adapter fetch one company-facts document once a day."""
    from data_engine.quality import vendor_oracle

    socket: list[str] = []
    document = json.dumps({"facts": {}}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        socket.append(str(request.url))
        return httpx.Response(200, content=document)

    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the oracle asked SEC for a document that the adapter landed")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    sec.fetch_company_facts_response(1652044)
    assert vendor_oracle.company_facts(1652044, "TrueAlpha test test@example.com") == {"facts": {}}
    assert len(socket) == 1


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
    """GOOG and GOOGL share CIK 0001652044 and make 1 companyfacts request, not 2 (#1411 K5 D3).

    The fetcher is the deployed one, over a fake socket. A second call in the same UTC day, from
    another run or another universe, reads the landed key the same way."""
    socket: list[str] = []
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [_annual_revenue()]}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        socket.append(str(request.url))
        return httpx.Response(200, content=json.dumps(facts).encode())

    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(
        "data_engine.datahub.production_topt.sec_financial_adapter._enrich_multi_class_shares",
        lambda *_a, **_k: None,
    )

    bundle_googl = sec_financial_fetcher(1652044, date(2026, 10, 16), OperatingBranch.NON_FINANCIAL)
    bundle_goog = sec_financial_fetcher(1652044, date(2026, 10, 16), OperatingBranch.NON_FINANCIAL)

    assert socket == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0001652044.json"]
    assert bundle_googl.revenue == bundle_goog.revenue == Decimal("350000000000")


def _annual_revenue() -> dict[str, Any]:
    return {
        "start": "2025-01-01",
        "end": "2025-12-31",
        "val": 350000000000,
        "accn": "0001652044-26-000010",
        "fy": 2025,
        "fp": "FY",
        "form": "10-K",
        "filed": "2026-02-04",
    }


class _FilingSocket:
    """EDGAR as one fake client: the submissions index and the primary document of one 10-K."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> Any:
        self.urls.append(url)
        if "/submissions/" in url:
            body: bytes = json.dumps(
                {
                    "filings": {
                        "recent": {
                            "form": ["10-K"],
                            "filingDate": ["2026-02-01"],
                            "accessionNumber": ["0000320193-26-000010"],
                            "primaryDocument": ["aapl-20250927.htm"],
                        }
                    }
                }
            ).encode()
        else:
            body = b"<html><body>We had 164,000 full-time equivalent employees.</body></html>"
        return httpx.Response(200, content=body, request=httpx.Request("GET", url))


def test_k5_d3_headcount_and_segments_share_one_10k_fetch() -> None:
    """Two readers of the latest 10-K of one CIK make one submissions request and one archive request.

    The first call reaches the socket twice. The second call, with a new client as every caller makes,
    reaches it zero times: the keys landed. The module cache keyed by the id of the client is gone."""
    cutoff = datetime(2026, 10, 16, 22, 0, tzinfo=UTC)
    first_socket, second_socket = _FilingSocket(), _FilingSocket()
    doc1 = latest_annual_filing(320193, http=first_socket, gateway=None, cutoff=cutoff)  # type: ignore[arg-type]
    doc2 = latest_annual_filing(320193, http=second_socket, gateway=None, cutoff=cutoff)  # type: ignore[arg-type]
    assert doc1 is not None and doc1 == doc2
    assert [url.rsplit("/", 1)[-1] for url in first_socket.urls] == ["CIK0000320193.json", "aapl-20250927.htm"]
    assert second_socket.urls == []


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
