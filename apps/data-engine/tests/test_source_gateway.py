"""The external call gateway and ledger (#729): every vendor request is one row that
either dereferences to the landed bytes or says why it failed.

Two layers: the gateway's own contract (what a row contains, how failure is recorded,
how a ledger outage is isolated), and the deployed adapters — each vendor call site the
composition root actually runs is exercised through its real entry point with a fake
transport, and must produce exactly one row. The static scan at the end is the standing
check that a new adapter cannot reach a vendor without the ledger (AGENTS.md rule 7:
red against the pre-#729 `universe_plane.py`, which called `urlopen` directly).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.sources import gateway

SRC = Path(__file__).resolve().parents[1] / "src" / "data_engine"


# --- the gateway's own contract --------------------------------------------------------


def test_a_successful_call_is_one_row_that_names_the_bytes_it_received(call_ledger) -> None:
    body = b'{"chart": {"result": []}}'
    with gateway.record_call(
        "twelvedata", "eod", caller="test", request_uri="https://x/eod?symbol=A&apikey=s3cret"
    ) as call:
        call.observe(status_code=200, body=body)

    (row,) = call_ledger
    assert (row.source, row.endpoint, row.caller, row.ok) == ("twelvedata", "eod", "test", True)
    assert row.status_code == 200 and row.error is None
    assert row.payload_sha256 == hashlib.sha256(body).hexdigest(), "the join key to raw.fetches.payload_sha256"
    assert row.byte_length == len(body)
    assert row.duration_ms is not None and row.duration_ms >= 0
    assert row.cost == 1 and isinstance(row.cost, Decimal), "numeric in the ledger, never a binary float"
    assert row.request_uri == "https://x/eod?symbol=A&apikey=%2A%2A%2A", "credentials never reach the ledger"
    assert row.capacity_window_id == f"twelvedata:day:{datetime.now(UTC).date().isoformat()}"


def test_a_text_or_unhashable_body_never_turns_a_recorded_call_into_a_raised_one(call_ledger) -> None:
    with gateway.record_call("nport", "archive", caller="test") as call:
        call.observe(status_code=200, body="<xml/>")
    with gateway.record_call("nport", "archive", caller="test") as call:
        call.observe(status_code=200, body=object())  # a fake transport's non-bytes payload

    text_row, opaque_row = call_ledger
    assert text_row.payload_sha256 == hashlib.sha256(b"<xml/>").hexdigest() and text_row.byte_length == 6
    assert opaque_row.ok is True and opaque_row.payload_sha256 is None


def test_an_exception_inside_the_block_is_a_failed_row_and_still_raises(call_ledger) -> None:
    with pytest.raises(TimeoutError):
        with gateway.record_call("sec", "companyfacts", caller="test"):
            raise TimeoutError("read timed out")

    (row,) = call_ledger
    assert row.ok is False
    assert row.error == "TimeoutError: read timed out"
    assert row.payload_sha256 is None, "nothing was received, so nothing can be dereferenced"


def test_an_error_status_is_a_failed_row_even_though_nothing_raised(call_ledger) -> None:
    with gateway.record_call("openfigi", "mapping", caller="test") as call:
        call.observe(status_code=429, body=b"")

    (row,) = call_ledger
    assert row.ok is False and row.status_code == 429
    assert row.error == "HTTP 429", "a rate-limited request is a failed request that still spent quota"


def test_urlopen_returns_the_error_body_and_records_the_vendor_message(call_ledger, monkeypatch) -> None:
    """Status-honest (#557): Twelve Data's 'no end of day' 400 must reach the parser
    AND be recorded as the failed call it is — with the vendor's own words."""
    body = json.dumps({"code": 400, "message": "No data is available on the specified dates"}).encode()

    def fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 400, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status, returned = gateway.urlopen("twelvedata", "eod", "https://x/eod", caller="test", timeout=1)

    assert (status, returned) == (400, body)
    (row,) = call_ledger
    assert row.ok is False and row.status_code == 400
    assert row.error == "No data is available on the specified dates"
    assert row.payload_sha256 == hashlib.sha256(body).hexdigest()


def test_urlopen_can_raise_on_an_error_status_when_the_caller_needs_that(call_ledger, monkeypatch) -> None:
    def fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, io.BytesIO(b"busy"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(gateway.SourceHTTPError) as info:
        gateway.urlopen("openfigi", "mapping", "https://x/mapping", caller="test", timeout=1, raise_for_status=True)
    assert info.value.status_code == 503
    (row,) = call_ledger
    assert row.ok is False and row.status_code == 503 and row.error == "HTTP 503"


def test_a_ledger_write_failure_never_masks_the_vendor_result(monkeypatch, caplog) -> None:
    def broken_writer(record):
        raise RuntimeError("ledger database is down")

    monkeypatch.setattr(gateway, "_writer", broken_writer)
    with caplog.at_level(logging.ERROR, logger="data_engine.sources.gateway"):
        with gateway.record_call("yahoo", "chart", caller="test") as call:
            call.observe(status_code=200, body=b"ok")
    assert "external call ledger write failed" in caplog.text
    assert '"source": "yahoo"' in caplog.text, "the unrecorded row is at least in the run log"


def test_run_scope_attributes_rows_to_the_dagster_run(call_ledger) -> None:
    with gateway.run_scope("dagster:run-123"):
        with gateway.record_call("sec", "submissions", caller="test") as call:
            call.observe(status_code=200, body=b"{}")
    with gateway.record_call("sec", "submissions", caller="test") as call:
        call.observe(status_code=200, body=b"{}")

    inside, outside = call_ledger
    assert inside.run_key == "dagster:run-123"
    assert outside.run_key is None


def test_a_float_cost_is_refused_rather_than_written_inexactly() -> None:
    with pytest.raises(TypeError):
        with gateway.record_call("twelvedata", "eod", caller="test", cost=0.1):  # type: ignore[arg-type]
            pass
    with gateway.record_call("twelvedata", "eod", caller="test", cost=Decimal("2")) as call:
        call.observe(status_code=200, body=b"")
    assert call.cost == Decimal("2")


def test_the_ledger_backend_setting_only_takes_its_two_values() -> None:
    from data_engine.config import Settings
    from pydantic import ValidationError

    assert Settings(external_call_ledger="off").external_call_ledger == "off"
    with pytest.raises(ValidationError):
        Settings(external_call_ledger="postgress")


def test_credential_query_keys_are_blanked_whatever_their_casing() -> None:
    redacted = gateway.redact_uri(
        "https://x/q?symbol=AAPL&APIKEY=a&ApiKey=b&access_token=c&X-Api-Key=d&client_secret=e"
    )
    assert (
        redacted
        == "https://x/q?symbol=AAPL&APIKEY=%2A%2A%2A&ApiKey=%2A%2A%2A&access_token=%2A%2A%2A&X-Api-Key=%2A%2A%2A&client_secret=%2A%2A%2A"
    )
    assert gateway.redact_uri("https://x/q?symbol=AAPL&date=2026-09-04") == "https://x/q?symbol=AAPL&date=2026-09-04"


def test_capacity_windows_follow_what_the_vendor_declares() -> None:
    at = datetime(2026, 9, 4, 22, 15, 30, tzinfo=UTC)
    assert gateway.capacity_window_id("twelvedata", at) == "twelvedata:day:2026-09-04", "daily budget wins"
    assert gateway.capacity_window_id("openfigi", at) == f"openfigi:6s:{int(at.timestamp() // 6)}"
    assert gateway.capacity_window_id("yahoo", at) is None, "the vendor publishes no limit"
    assert gateway.capacity_window_id("never-heard-of", at) is None


# --- every deployed adapter, through its real entry point ------------------------------


class _Response:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self.content = body
        self.status_code = status_code

    def json(self):
        return json.loads(self.content.decode())

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """An httpx-shaped client that answers queued responses (get and post alike)."""

    def __init__(self, *responses: _Response, **_: object) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None: ...

    def get(self, url: str, **kwargs):
        self.calls.append(("get", url))
        return self._responses.pop(0)

    def post(self, url: str, **kwargs):
        self.calls.append(("post", url))
        return self._responses.pop(0)


def test_yahoo_chart_fetch_is_one_yahoo_row(call_ledger, monkeypatch) -> None:
    from data_engine.sources import yahoo

    body = b'{"chart": {"result": []}}'
    monkeypatch.setattr(yahoo.httpx, "Client", lambda **kwargs: _Client(_Response(body)))
    yahoo.fetch_daily_bars("AAPL", end=date(2026, 9, 1))

    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok) == ("yahoo", "chart", True)
    assert row.payload_sha256 == hashlib.sha256(body).hexdigest()
    assert row.request_uri.startswith(yahoo.CHART_URL.format(symbol="AAPL") + "?period1=")
    assert "interval=1d" in row.request_uri, "the query the request actually sent is part of the row (review on #741)"


def test_twelve_data_no_end_of_day_is_a_failed_twelvedata_row_with_the_key_redacted(call_ledger, monkeypatch) -> None:
    from data_engine.datahub.production_topt import twelve_data_origin as origin

    body = json.dumps({"code": 400, "message": "No data is available on the specified dates"}).encode()

    def fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 400, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = origin.TwelveDataQuoteFetcher("s3cret-key", throttle_seconds=0)
    assert fetcher._get(origin._EOD_URL, {"symbol": "AAPL", "date": "2026-08-29"}) == body

    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok, row.status_code) == ("twelvedata", "eod", False, 400)
    assert row.error == "No data is available on the specified dates"
    assert "s3cret-key" not in (row.request_uri or ""), "the API key never reaches the ledger"
    assert "symbol=AAPL" in row.request_uri


def test_sec_companyfacts_fetch_is_one_sec_row(call_ledger) -> None:
    from data_engine.sources import sec

    body = b'{"facts": {}}'
    client = _Client(_Response(body))
    raw, parsed = sec.fetch_company_facts_response(320193, http=client)

    assert raw == body and parsed == {"facts": {}}
    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok) == ("sec", "companyfacts", True)
    assert row.request_uri == sec.COMPANY_FACTS_URL.format(cik=320193)
    assert row.payload_sha256 == hashlib.sha256(body).hexdigest()


def test_nport_fund_lookup_is_one_nport_row(call_ledger) -> None:
    from data_engine.sources import nport

    body = json.dumps({"data": [[36405, "S000002839", "C000007873", "VOO"]]}).encode()
    assert nport.fund_series(_Client(_Response(body)), "voo") == (36405, "S000002839")

    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok) == ("nport", "mf_tickers", True)


def test_openfigi_records_the_rate_limited_attempt_and_the_answer(call_ledger) -> None:
    from data_engine.sources import openfigi

    answer = json.dumps([{"data": [{"figi": "BBG000B9XRY4"}]}]).encode()
    client = _Client(_Response(b"", 429), _Response(answer))
    mapped = openfigi.map_isins(client, ["US0378331005"], api_key="k", sleep=lambda _s: None)

    assert mapped == {"US0378331005": [{"figi": "BBG000B9XRY4"}]}
    first, second = call_ledger
    assert (first.source, first.endpoint, first.ok, first.status_code, first.error) == (
        "openfigi",
        "mapping",
        False,
        429,
        "HTTP 429",
    )
    assert second.ok is True and second.payload_sha256 == hashlib.sha256(answer).hexdigest()


def test_an_httpx_error_status_carries_the_vendor_message(call_ledger) -> None:
    body = json.dumps({"message": "Request rate limit exceeded"}).encode()
    response = gateway.http_get(_Client(_Response(body, 429)), "sec", "companyfacts", "https://x/f", caller="test")

    assert response.status_code == 429, "the caller still gets the answer and decides what to do with it"
    (row,) = call_ledger
    assert (row.ok, row.status_code, row.error) == (False, 429, "Request rate limit exceeded")


def test_index_operator_fetch_is_one_nasdaq_index_row(call_ledger, monkeypatch) -> None:
    from data_engine.datahub.production_topt import universe_plane

    body = b'{"data": {"data": {"rows": []}}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: io.BytesIO(body))
    status, returned = universe_plane._get("https://api.nasdaq.com/api/quote/list-type/nasdaq100")

    assert returned == body and status is None, "a transport that reports no status is not a failure"
    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok) == ("nasdaq-index", "constituents", True)


def test_moomoo_failures_reach_the_same_ledger_with_their_error(call_ledger, monkeypatch) -> None:
    from data_engine.sources import moomoo as mm
    from data_engine.sources import moomoo_ledger as ledger

    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "postgres")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 10)
    monkeypatch.setattr(ledger, "calls_this_month", lambda: 0)
    monkeypatch.setattr(gateway, "postgres_writer", call_ledger)
    ledger._recent_calls.clear()

    def raises(ctx):
        raise TimeoutError("OpenD did not answer")

    with pytest.raises(mm.MoomooConnectionError):
        mm._call(ctx=object(), endpoint="get_market_snapshot", caller="test", fn=raises)
    mm._call(ctx=object(), endpoint="get_market_snapshot", caller="test", fn=lambda c: (mm.moomoo.RET_OK, "data"))

    failed, succeeded = call_ledger
    assert (failed.source, failed.endpoint, failed.ok) == ("moomoo", "get_market_snapshot", False)
    assert failed.error == "TimeoutError: OpenD did not answer"
    assert failed.duration_ms is not None
    assert succeeded.ok is True and succeeded.status_code == mm.moomoo.RET_OK


# --- the standing check: no vendor call site outside the gateway -----------------------

_NETWORK_ENTRY = re.compile(r"urlopen\(|httpx\.Client\(|requests\.(get|post|Session)\(")


def test_every_network_call_site_imports_the_gateway() -> None:
    """A module that opens a vendor connection must route the call through
    `sources.gateway`; importing it is the cheapest observable proof, and the per-adapter
    tests above prove the rows. Red against the pre-#729 universe_plane/twelve_data_origin/
    vendor_oracle, which called `urlopen` with no ledger at all."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "gateway.py":
            continue
        text = path.read_text()
        if _NETWORK_ENTRY.search(text) and "from data_engine.sources import gateway" not in text:
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"vendor call sites without the external call ledger: {offenders}"


# --- #740's enforcing gateway: declared capacity, throttle, budget, refusal --------------


class _Ledger:
    """Enough of a connection for the gateway: a daily count and an append."""

    def __init__(self, spent_today: int = 0) -> None:
        self.spent_today = spent_today
        self.rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()):
        if sql.lstrip().startswith("select count"):
            return _Row((self.spent_today,))
        assert "insert into staging.api_call_ledger" in sql
        self.rows.append(params)
        return _Row(None)


class _Row:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return self._value


def _four(row: tuple) -> tuple:
    """(source, endpoint, caller, ok) out of the rich row — the four #740 asserted on."""
    return (row[0], row[1], row[2], row[4])


def _gateway(ledger: _Ledger, capacity: gateway.SourceCapacity, clock: list[float], slept: list[float]):
    return gateway.SourceGateway(
        ledger,
        caller="test",
        capacities={capacity.source: capacity},
        clock=lambda: clock[0],
        sleep=lambda seconds: (slept.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
    )


def test_every_gateway_call_lands_in_the_ledger_with_its_caller_and_outcome() -> None:
    ledger = _Ledger()
    gw = _gateway(ledger, gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])

    assert gw.call("sec", "submissions", lambda: "ok") == "ok"
    with pytest.raises(RuntimeError, match="boom"):
        gw.call("sec", "archive", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert [_four(r) for r in ledger.rows] == [("sec", "submissions", "test", True), ("sec", "archive", "test", False)]
    assert ledger.rows[1][6] == "RuntimeError: boom", "the failed row carries the error (#729)"


def test_a_gateway_call_around_an_adapter_is_one_row_with_the_adapters_detail(call_ledger) -> None:
    """The standards lane wraps `sec.ticker_cik_index(http)` in `gateway.call`; the adapter
    itself goes through `http_get`. That must be ONE row — on the gateway's connection,
    with the status, the digest and the URI the inner call observed."""
    from data_engine.sources import sec

    ledger = _Ledger()
    gw = _gateway(ledger, gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    body = json.dumps({"0": {"ticker": "AAPL", "cik_str": 320193}}).encode()
    index = gw.call("sec", "company_tickers", lambda: sec.ticker_cik_index(_Client(_Response(body))))

    assert index == {"AAPL": 320193}
    assert call_ledger == [], "the inner http_get did not emit a second row"
    (row,) = ledger.rows
    assert _four(row) == ("sec", "company_tickers", "test", True)
    assert row[5] == 200 and row[9] == hashlib.sha256(body).hexdigest() and row[8] == sec.TICKERS_URL


def test_a_gateway_call_that_returns_the_body_records_its_digest() -> None:
    """`filing_extraction._get_bytes` returns the bytes, not a response: the row still
    names them (Copilot on #741)."""
    ledger = _Ledger()
    gw = _gateway(ledger, gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    assert gw.call("sec", "archive", lambda: b"<xml/>") == b"<xml/>"
    (row,) = ledger.rows
    assert row[9] == hashlib.sha256(b"<xml/>").hexdigest() and row[10] == 6


def test_a_call_inside_a_full_window_waits_for_the_window_to_roll() -> None:
    clock, slept = [0.0], []
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 2, 100), clock, slept)

    gw.call("sec", "a", lambda: None)
    gw.call("sec", "b", lambda: None)
    gw.call("sec", "c", lambda: None)  # third call in a 2-per-second window

    assert slept and slept[0] == pytest.approx(1.0)


def test_the_daily_budget_refuses_rather_than_fires() -> None:
    ledger = _Ledger(spent_today=5)
    gw = _gateway(ledger, gateway.SourceCapacity("search", 60.0, 20, 5), [0.0], [])

    with pytest.raises(gateway.CapacityExceeded, match="daily budget 5 spent"):
        gw.call("search", "query", lambda: "must not run")
    assert ledger.rows == []  # a refused call spends nothing and records nothing


def test_capacity_one_defers_the_second_call_of_the_day() -> None:
    """#729 criterion 4, #735 acceptance: red-proof shape — with capacity 1 the second
    call is refused, not fired."""
    ledger = _Ledger(spent_today=0)
    gw = _gateway(ledger, gateway.SourceCapacity("filing-extraction-model", 60.0, 10, 1), [0.0], [])

    gw.call("filing-extraction-model", "select", lambda: "first")
    with pytest.raises(gateway.CapacityExceeded):
        gw.call("filing-extraction-model", "select", lambda: "second")
    assert len(ledger.rows) == 1


def test_the_daily_count_resets_when_the_utc_day_rolls_over() -> None:
    """Review on #740: a long-lived gateway must not carry yesterday's count into today."""
    ledger = _Ledger(spent_today=0)
    clock, slept = [0.0], []
    day = [datetime(2026, 9, 4, 23, 59, tzinfo=UTC)]
    gw = gateway.SourceGateway(
        ledger,
        caller="test",
        capacities={"sec": gateway.SourceCapacity("sec", 1.0, 100, 2)},
        clock=lambda: clock[0],
        sleep=lambda s: slept.append(s),
        now=lambda: day[0],
    )
    gw.call("sec", "a", lambda: None)
    gw.call("sec", "b", lambda: None)
    with pytest.raises(gateway.CapacityExceeded):
        gw.call("sec", "c", lambda: None)
    day[0] = datetime(2026, 9, 5, 0, 1, tzinfo=UTC)  # midnight passed; the ledger says 0 today
    gw.call("sec", "d", lambda: None)  # allowed again
    assert [r[1] for r in ledger.rows] == ["a", "b", "d"]


def test_an_undeclared_or_record_only_source_cannot_be_called_through_the_gateway() -> None:
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    with pytest.raises(gateway.CapacityExceeded, match="no declared capacity"):
        gw.call("yahoo", "quote", lambda: None)
    gw_default = gateway.SourceGateway(_Ledger(), caller="test")
    with pytest.raises(gateway.CapacityExceeded, match="no declared capacity"):
        gw_default.call("yahoo", "quote", lambda: None)  # declared, but record-only: no window/budget


def test_capacity_must_be_positive_and_paired() -> None:
    with pytest.raises(ValueError):
        gateway.SourceCapacity("sec", 0, 8, 100)
    with pytest.raises(ValueError):
        gateway.SourceCapacity("sec", 1.0, None, 100)
    assert gateway.SourceCapacity("yahoo", None, None, None).enforceable is False
