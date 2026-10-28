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

import ast
import hashlib
import io
import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway

SRC = Path(__file__).resolve().parents[1] / "src" / "data_engine"


def _key(source: str, endpoint: str) -> FetchKey:
    """The identity of a call that the test makes: the test names no resource of its own."""
    return FetchKey(source, endpoint, "symbol:TEST", "2026-10-27")


# --- the gateway's own contract --------------------------------------------------------


def test_a_successful_call_is_one_row_that_names_the_bytes_it_received(call_ledger) -> None:
    body = b'{"chart": {"result": []}}'
    with gateway.record_call(
        "twelvedata",
        "eod",
        caller="test",
        request_uri="https://x/eod?symbol=A&apikey=s3cret",
        identity=_key("twelvedata", "eod"),
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
    with gateway.record_call("nport", "archive", caller="test", identity=_key("nport", "archive")) as call:
        call.observe(status_code=200, body="<xml/>")
    with gateway.record_call("nport", "archive", caller="test", identity=_key("nport", "archive")) as call:
        call.observe(status_code=200, body=object())  # a fake transport's non-bytes payload

    text_row, opaque_row = call_ledger
    assert text_row.payload_sha256 == hashlib.sha256(b"<xml/>").hexdigest() and text_row.byte_length == 6
    assert opaque_row.ok is True and opaque_row.payload_sha256 is None


def test_an_exception_inside_the_block_is_a_failed_row_and_still_raises(call_ledger) -> None:
    with pytest.raises(TimeoutError):
        with gateway.record_call("sec", "companyfacts", caller="test", identity=_key("sec", "companyfacts")):
            raise TimeoutError("read timed out")

    (row,) = call_ledger
    assert row.ok is False
    assert row.error == "TimeoutError: read timed out"
    assert row.payload_sha256 is None, "nothing was received, so nothing can be dereferenced"


def test_an_error_status_is_a_failed_row_even_though_nothing_raised(call_ledger) -> None:
    with gateway.record_call("openfigi", "mapping", caller="test", identity=_key("openfigi", "mapping")) as call:
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
    status, returned = gateway.urlopen(
        "twelvedata", "eod", "https://x/eod", caller="test", timeout=1, identity=_key("twelvedata", "eod")
    )

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
        gateway.urlopen(
            "openfigi",
            "mapping",
            "https://x/mapping",
            caller="test",
            timeout=1,
            raise_for_status=True,
            identity=_key("openfigi", "mapping"),
        )
    assert info.value.status_code == 503
    (row,) = call_ledger
    assert row.ok is False and row.status_code == 503 and row.error == "HTTP 503"


def test_a_ledger_write_failure_never_masks_the_vendor_result(monkeypatch, caplog) -> None:
    def broken_writer(record):
        raise RuntimeError("ledger database is down")

    monkeypatch.setattr(gateway, "_writer", broken_writer)
    with caplog.at_level(logging.ERROR, logger="data_engine.sources.gateway"):
        with gateway.record_call("yahoo", "chart", caller="test", identity=_key("yahoo", "chart")) as call:
            call.observe(status_code=200, body=b"ok")
    assert "external call ledger write failed" in caplog.text
    assert '"source": "yahoo"' in caplog.text, "the unrecorded row is at least in the run log"


def test_run_scope_attributes_rows_to_the_dagster_run_and_a_call_outside_a_run_is_recorded_without_one(
    call_ledger,
) -> None:
    """Owner decision D5 (#1521): a call outside every run is recorded with a NULL run key, never refused."""
    with gateway.run_scope("dagster:run-123"):
        with gateway.record_call("sec", "submissions", caller="test", identity=_key("sec", "submissions")) as call:
            call.observe(status_code=200, body=b"{}")
    with gateway.record_call("sec", "submissions", caller="test", identity=_key("sec", "submissions")) as call:
        call.observe(status_code=200, body=b"{}")

    inside, outside = call_ledger
    assert inside.run_key == "dagster:run-123"
    assert outside.run_key is None


def test_a_float_cost_is_refused_rather_than_written_inexactly() -> None:
    with pytest.raises(TypeError):
        with gateway.record_call("twelvedata", "eod", caller="test", cost=0.1, identity=_key("twelvedata", "eod")):  # type: ignore[arg-type]
            pass
    with gateway.record_call(
        "twelvedata", "eod", caller="test", cost=Decimal("2"), identity=_key("twelvedata", "eod")
    ) as call:
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


def test_capacity_windows_follow_what_the_registry_declares() -> None:
    at = datetime(2026, 9, 4, 22, 15, 30, tzinfo=UTC)
    assert gateway.capacity_window_id("twelvedata", at) == "twelvedata:day:2026-09-04", "daily budget wins"
    assert gateway.capacity_window_id("openfigi", at) == f"openfigi:60s:{int(at.timestamp() // 60)}"
    # Yahoo publishes no limit; the pace is ours and has no daily budget (OD2 A of #1520): a rate window only.
    assert gateway.capacity_window_id("yahoo", at) == f"yahoo:1s:{int(at.timestamp())}"
    assert gateway.capacity_window_id("never-heard-of", at) is None


# --- every deployed adapter, through its real entry point ------------------------------


class _Response:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        assert isinstance(body, bytes), "an httpx response holds bytes"
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
    fetcher = origin.TwelveDataQuoteFetcher("s3cret-key")
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
    response = gateway.http_get(
        _Client(_Response(body, 429)),
        "sec",
        "companyfacts",
        "https://x/f",
        caller="test",
        identity=_key("sec", "companyfacts"),
    )

    assert response.status_code == 429, "the caller still gets the answer and decides what to do with it"
    (row,) = call_ledger
    assert (row.ok, row.status_code, row.error) == (False, 429, "Request rate limit exceeded")


def test_index_operator_fetch_is_one_nasdaq_index_row(call_ledger, monkeypatch) -> None:
    from data_engine.datahub.production_topt import universe_plane

    body = b'{"data": {"data": {"rows": []}}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: io.BytesIO(body))
    status, returned = universe_plane._get(
        "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
        identity=FetchKey("nasdaq-index", "constituents", "index:nasdaq100", "2026-10-27"),
    )

    assert returned == body and status is None, "a transport that reports no status is not a failure"
    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok) == ("nasdaq-index", "constituents", True)


@pytest.mark.usefixtures("idle_moomoo_gate")
def test_moomoo_failures_reach_the_same_ledger_with_their_error(call_ledger) -> None:
    from data_engine.sources import moomoo as mm

    def raises(ctx):
        raise TimeoutError("OpenD did not answer")

    with pytest.raises(mm.MoomooConnectionError):
        mm._call(
            ctx=object(),
            endpoint="get_market_snapshot",
            caller="test",
            fn=raises,
            identity=FetchKey("moomoo", "get_market_snapshot", "symbol:TEST", "2026-10-27"),
        )
    mm._call(
        ctx=object(),
        endpoint="get_market_snapshot",
        caller="test",
        fn=lambda c: (mm.moomoo.RET_OK, "data"),
        identity=FetchKey("moomoo", "get_market_snapshot", "symbol:TEST", "2026-10-27"),
    )

    failed, succeeded = call_ledger
    assert (failed.source, failed.endpoint, failed.ok) == ("moomoo", "get_market_snapshot", False)
    assert failed.error == "TimeoutError: OpenD did not answer"
    assert failed.duration_ms is not None
    assert succeeded.ok is True and succeeded.status_code == mm.moomoo.RET_OK


# --- the standing check: no vendor call outside the gateway, and every gateway call names its key ----

#: The source tree and the scripts: the two places that make vendor calls (`mypy` skips `scripts/`).
_SCANNED = (SRC, Path(__file__).resolve().parents[1] / "scripts")
_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send"})
#: What builds an HTTP client. A name bound to one of these (or annotated as one) is a client name.
_CLIENT_TYPES = frozenset({"httpx.Client", "httpx.AsyncClient"})
_CLIENT_FACTORIES = _CLIENT_TYPES | {"data_engine.sources.sec.client"}
#: Calls that take the key of the resource as the keyword `identity` (#1521 step 2).
_IDENTITY_FUNCTIONS = frozenset(
    {"data_engine.sources.gateway." + name for name in ("record_call", "http_get", "http_post", "urlopen")}
    | {
        "data_engine.sources.moomoo." + name
        for name in (
            "_call",
            "get_analyst_consensus",
            "get_rating_summary",
            "get_market_rating_changes",
            "get_company_profile",
            "get_financials_statements",
            "get_financials_revenue_breakdown",
            "get_valuation_detail",
            "get_research_morningstar_report",
            "get_shareholders_overview",
            "get_insider_trade_list",
            "get_corporate_actions_dividends",
            "get_short_interest",
            "get_owner_plate",
            "get_history_kline",
            "get_history_kl_quota",
        )
    }
    | {"data_engine.datahub.production_topt.universe_plane._get"}
)
#: Methods of the moomoo client protocol; any owner (`client.history_kline`, `self._client.history_kline`).
_IDENTITY_METHODS = frozenset({"history_kline", "financial_statements"})


def _module_of(path: Path, root: Path) -> str:
    """The dotted module of ``path`` under ``root`` (`src/data_engine/sources/gateway.py` is `data_engine.sources.gateway`)."""
    return ".".join((root.name, *path.relative_to(root).with_suffix("").parts))


def _aliases(tree: ast.Module) -> dict[str, str]:
    """The dotted name that each imported name stands for."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr, aliases: dict[str, str], module: str) -> str | None:
    """The dotted name of a name or attribute chain, through the imports of the file and its own module."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, f"{module}.{node.id}")
    if isinstance(node, ast.Attribute):
        owner = _dotted(node.value, aliases, module)
        return None if owner is None else f"{owner}.{node.attr}"
    return None


def _client_names(tree: ast.Module, aliases: dict[str, str], module: str) -> set[str]:
    """Names bound to an HTTP client: built by a client factory, or annotated as a client."""

    def builds_client(value: ast.expr | None) -> bool:
        return isinstance(value, ast.Call) and _dotted(value.func, aliases, module) in _CLIENT_FACTORIES

    def annotated_client(annotation: ast.expr | None) -> bool:
        return annotation is not None and any(
            _dotted(part, aliases, module) in _CLIENT_TYPES
            for part in ast.walk(annotation)
            if isinstance(part, ast.Name | ast.Attribute)
        )

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and builds_client(node.value):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if builds_client(node.value) or annotated_client(node.annotation):
                names.add(node.target.id)
        elif isinstance(node, ast.withitem) and builds_client(node.context_expr):
            if isinstance(node.optional_vars, ast.Name):
                names.add(node.optional_vars.id)
        elif isinstance(node, ast.arg) and annotated_client(node.annotation):
            names.add(node.arg)
    return names


def scan_vendor_calls(path: Path, root: Path, *, text: str | None = None) -> tuple[list[str], int]:
    """The findings of one file, and how many gateway calls it makes.

    A finding is a call that leaves the gateway (outside `sources/`): `httpx.get` and its verbs, any
    `requests.*`, `urllib.request.urlopen`, the `get` or `post` of an HTTP client name, `OpenQuoteContext`.
    Or it is a call to a gateway entry point that passes no `identity`."""
    tree = ast.parse(path.read_text() if text is None else text)
    module = _module_of(path, root)
    aliases = _aliases(tree)
    clients = _client_names(tree, aliases, module)
    in_sources = module.startswith("data_engine.sources.")
    findings: list[str] = []
    gateway_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = _dotted(func, aliases, module) or ""
        where = f"{path.relative_to(root)}:{node.lineno}"
        if not in_sources:
            if (
                name in {f"httpx.{verb}" for verb in _HTTP_VERBS}
                or name.startswith("requests.")
                or name == "urllib.request.urlopen"
                or name in {"moomoo.OpenQuoteContext", "moomoo.OpenTradeContext"}
                or (
                    isinstance(func, ast.Attribute)
                    and func.attr in _HTTP_VERBS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in clients
                )
            ):
                findings.append(f"{where}: {ast.unparse(func)}(...) leaves the gateway")
        names_identity = any(keyword.arg == "identity" for keyword in node.keywords)
        takes_identity = (
            name in _IDENTITY_FUNCTIONS
            or (isinstance(func, ast.Attribute) and func.attr in _IDENTITY_METHODS)
            or (
                isinstance(func, ast.Attribute)
                and func.attr == "call"
                and len(node.args) >= 3
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            )
        )
        if takes_identity:
            gateway_calls += 1
            if not names_identity:
                findings.append(f"{where}: {ast.unparse(func)}(...) names no `identity`")
        elif name == "data_engine.datahub.treasury_rates.fetch_year":
            gateway_calls += 1
            if len(node.args) < 2 and not names_identity:
                findings.append(f"{where}: fetch_year(...) names no key")
    return findings, gateway_calls


def _scan_all() -> tuple[list[str], int, int]:
    """The findings of `src/` and `scripts/`, the number of files scanned and the number of gateway calls seen."""
    findings: list[str] = []
    files = calls = 0
    for root in _SCANNED:
        for path in sorted(root.rglob("*.py")):
            found, made = scan_vendor_calls(path, root)
            findings += [f"{root.name}/{item}" for item in found]
            files += 1
            calls += made
    return findings, files, calls


def test_no_vendor_call_leaves_the_gateway_and_every_gateway_call_names_its_key() -> None:
    """One scan of `src/` and `scripts/`, no file exempt. A vendor call that leaves the gateway writes no ledger
    row and spends quota nobody counts; a gateway call without `identity` raises at the call. `scripts/` is
    outside `mypy`, so this scan is the only check that sees a script."""
    findings, files, gateway_calls = _scan_all()
    assert files > 0 and gateway_calls > 0, "the scan found nothing to scan: it would pass on an empty tree"
    assert findings == [], "vendor calls outside the gateway, or gateway calls without a key:\n" + "\n".join(findings)


_MUTATIONS = [
    ("datahub/x.py", "import httpx\nhttpx.get('https://v.example/x')\n", "httpx.get(...) leaves the gateway"),
    ("datahub/x.py", "import httpx\nhttpx.request('GET', 'https://v.example/x')\n", "httpx.request(...) leaves"),
    ("lanes/x.py", "import requests\nrequests.Session().get('https://v.example')\n", "requests"),
    ("lanes/x.py", "import urllib.request\nurllib.request.urlopen('https://v.example')\n", "urlopen(...) leaves"),
    ("lanes/x.py", "from urllib.request import urlopen\nurlopen('https://v.example')\n", "urlopen(...) leaves"),
    ("lanes/x.py", "from urllib import request\nrequest.urlopen('https://v.example')\n", "urlopen(...) leaves"),
    (
        "datahub/x.py",
        "import httpx\nwith httpx.Client() as http:\n    http.get('https://v.example')\n",
        "http.get(...) leaves",
    ),
    (
        "datahub/x.py",
        "import httpx\nclient = httpx.Client()\nclient.post('https://v.example')\n",
        "client.post(...) leaves",
    ),
    (
        "datahub/x.py",
        "import httpx\ndef f(client: httpx.Client | None):\n    client.get('https://v.example')\n",
        "client.get(...) leaves",
    ),
    (
        "datahub/x.py",
        "from data_engine.sources.sec import client as sec_client\nwith sec_client() as http:\n    http.get('u')\n",
        "http.get(...) leaves",
    ),
    ("datahub/x.py", "import moomoo\nmoomoo.OpenQuoteContext(host='h', port=1)\n", "OpenQuoteContext(...) leaves"),
    (
        "datahub/x.py",
        "from data_engine.sources import gateway\ngateway.http_get(c, 'sec', 'e', 'u', caller='x')\n",
        "names no `identity`",
    ),
    (
        "datahub/x.py",
        "from data_engine.sources import gateway\ngateway.urlopen('sec', 'e', 'u', caller='x', timeout=1)\n",
        "names no `identity`",
    ),
    (
        "datahub/x.py",
        "from data_engine.sources import moomoo as mm\nmm.get_owner_plate(ctx, ['US.A'], caller='x')\n",
        "names no `identity`",
    ),
    ("datahub/x.py", "client.history_kline('US.A', start=s, end=e)\n", "names no `identity`"),
    ("datahub/x.py", "gw.call('sec', 'submissions', lambda: None)\n", "names no `identity`"),
]


@pytest.mark.parametrize(
    ("relative", "source", "expected"), _MUTATIONS, ids=[m[1].splitlines()[-1][:48] for m in _MUTATIONS]
)
def test_the_scan_finds_each_call_it_names(tmp_path: Path, relative: str, source: str, expected: str) -> None:
    """The mutation proof: the same text in a file under `src/` is a finding, so a bare `httpx.get` cannot pass."""
    root = tmp_path / "data_engine"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text(source)
    findings, _calls = scan_vendor_calls(path, root)
    assert any(expected in finding for finding in findings), findings


def test_the_scan_lets_the_gateway_and_a_keyed_call_through(tmp_path: Path) -> None:
    root = tmp_path / "data_engine"
    gateway_file = root / "sources" / "x.py"
    gateway_file.parent.mkdir(parents=True)
    gateway_file.write_text("import httpx\nhttpx.get('https://v.example')\n")  # `sources/` is the gateway's own
    keyed = root / "datahub" / "y.py"
    keyed.parent.mkdir()
    keyed.write_text(
        "from data_engine.sources import gateway\ngateway.http_get(c, 'sec', 'e', 'u', identity=key, caller='x')\n"
    )
    assert scan_vendor_calls(gateway_file, root) == ([], 0)
    assert scan_vendor_calls(keyed, root) == ([], 1)


# --- #740's enforcing gateway: declared capacity, throttle, budget, refusal --------------


class _Ledger:
    """Enough of a connection for the gateway's budget read: a daily count. A row never reaches it:
    the gateway writes through `emit` (the `call_ledger` fixture), so an INSERT here is an error."""

    def __init__(self, spent_today: int = 0) -> None:
        self.spent_today = spent_today

    def execute(self, sql: str, params: tuple = ()):
        assert sql.lstrip().startswith("select count"), "the gateway writes rows through emit, not this connection"
        return _Row((self.spent_today,))


class _Row:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return self._value


def _four(row: gateway.CallRecord) -> tuple:
    """(source, endpoint, caller, ok) of a row — the four #740 asserted on."""
    return (row.source, row.endpoint, row.caller, row.ok)


def _gateway(ledger: _Ledger, capacity: gateway.SourceCapacity, clock: list[float], slept: list[float]):
    """A gateway on a fake clock. The shared window is read by the wall clock, so it is off here and the
    gate paces on its own window; `test_the_explicit_gateway_waits_for_other_processes_in_the_shared_window`
    covers the shared read."""
    return gateway.SourceGateway(
        ledger,
        caller="test",
        capacities={capacity.source: capacity},
        clock=lambda: clock[0],
        sleep=lambda seconds: (slept.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        recent_calls=None,
    )


def test_every_gateway_call_lands_in_the_ledger_with_its_caller_and_outcome(call_ledger) -> None:
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])

    assert gw.call("sec", "submissions", lambda: "ok", identity=_key("sec", "submissions")) == "ok"
    with pytest.raises(RuntimeError, match="boom"):
        gw.call("sec", "archive", lambda: (_ for _ in ()).throw(RuntimeError("boom")), identity=_key("sec", "archive"))

    assert [_four(r) for r in call_ledger] == [("sec", "submissions", "test", True), ("sec", "archive", "test", False)]
    assert call_ledger[1].error == "RuntimeError: boom", "the failed row carries the error (#729)"


def test_a_gateway_call_around_an_adapter_is_one_row_with_the_adapters_detail(call_ledger) -> None:
    """The standards lane wraps `sec.ticker_cik_index(http)` in `gateway.call`; the adapter
    itself goes through `http_get`. That must be ONE row, with the status, the digest and
    the URI the inner call observed."""
    from data_engine.sources import sec

    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    body = json.dumps({"0": {"ticker": "AAPL", "cik_str": 320193}}).encode()
    index = gw.call(
        "sec",
        "company_tickers",
        lambda: sec.ticker_cik_index(_Client(_Response(body))),
        identity=sec.company_tickers_key(),
    )

    assert index == {"AAPL": 320193}
    (row,) = call_ledger
    assert _four(row) == ("sec", "company_tickers", "test", True), "the inner http_get did not emit a second row"
    assert row.status_code == 200 and row.payload_sha256 == hashlib.sha256(body).hexdigest()
    assert row.request_uri == sec.TICKERS_URL
    assert row.fetch_key == sec.company_tickers_key().ledger_key, "the outer and the inner call name one key"


def test_a_gateway_call_that_returns_the_body_records_its_digest(call_ledger) -> None:
    """`filing_extraction._get_bytes` returns the bytes, not a response: the row still
    names them (Copilot on #741)."""
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    assert gw.call("sec", "archive", lambda: b"<xml/>", identity=_key("sec", "archive")) == b"<xml/>"
    (row,) = call_ledger
    assert row.payload_sha256 == hashlib.sha256(b"<xml/>").hexdigest() and row.byte_length == 6


def test_a_call_inside_a_full_window_waits_for_the_window_to_roll() -> None:
    clock, slept = [0.0], []
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 2, 100), clock, slept)

    gw.call("sec", "a", lambda: None, identity=_key("sec", "a"))
    gw.call("sec", "b", lambda: None, identity=_key("sec", "b"))
    gw.call("sec", "c", lambda: None, identity=_key("sec", "c"))  # third call in a 2-per-second window

    assert slept and slept[0] == pytest.approx(1.0)


def test_the_daily_budget_refuses_rather_than_fires(call_ledger) -> None:
    gw = _gateway(_Ledger(spent_today=5), gateway.SourceCapacity("search", 60.0, 20, 5), [0.0], [])

    with pytest.raises(gateway.CapacityExceeded, match="daily budget 5 spent"):
        gw.call("search", "query", lambda: "must not run", identity=_key("search", "query"))
    assert call_ledger == []  # a refused call spends nothing and records nothing


def test_capacity_one_defers_the_second_call_of_the_day(call_ledger) -> None:
    """#729 criterion 4, #735 acceptance: red-proof shape — with capacity 1 the second
    call is refused, not fired."""
    gw = _gateway(_Ledger(spent_today=0), gateway.SourceCapacity("filing-extraction-model", 60.0, 10, 1), [0.0], [])

    gw.call("filing-extraction-model", "select", lambda: "first", identity=_key("filing-extraction-model", "select"))
    with pytest.raises(gateway.CapacityExceeded):
        gw.call(
            "filing-extraction-model", "select", lambda: "second", identity=_key("filing-extraction-model", "select")
        )
    assert len(call_ledger) == 1


def test_the_daily_count_resets_when_the_utc_day_rolls_over(call_ledger) -> None:
    """Review on #740: a long-lived gateway must not carry yesterday's count into today."""
    clock, slept = [0.0], []
    day = [datetime(2026, 9, 4, 23, 59, tzinfo=UTC)]
    gw = gateway.SourceGateway(
        _Ledger(spent_today=0),
        caller="test",
        capacities={"sec": gateway.SourceCapacity("sec", 1.0, 100, 2)},
        clock=lambda: clock[0],
        sleep=lambda s: slept.append(s),
        now=lambda: day[0],
    )
    gw.call("sec", "a", lambda: None, identity=_key("sec", "a"))
    gw.call("sec", "b", lambda: None, identity=_key("sec", "b"))
    with pytest.raises(gateway.CapacityExceeded):
        gw.call("sec", "c", lambda: None, identity=_key("sec", "c"))
    day[0] = datetime(2026, 9, 5, 0, 1, tzinfo=UTC)  # midnight passed; the ledger says 0 today
    gw.call("sec", "d", lambda: None, identity=_key("sec", "d"))  # allowed again
    assert [r.endpoint for r in call_ledger] == ["a", "b", "d"]


def test_an_undeclared_or_unbudgeted_source_cannot_be_called_through_the_gateway() -> None:
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    with pytest.raises(gateway.CapacityExceeded, match="no declared capacity"):
        gw.call("yahoo", "quote", lambda: None, identity=_key("yahoo", "quote"))
    unwindowed = gateway.SourceGateway(
        _Ledger(), caller="test", capacities={"budget-only": gateway.SourceCapacity("budget-only", None, None, 5)}
    )
    with pytest.raises(gateway.CapacityExceeded, match="needs a rate window"):
        unwindowed.call("budget-only", "mapping", lambda: None, identity=_key("budget-only", "mapping"))


def test_capacity_must_be_positive_and_paired() -> None:
    with pytest.raises(ValueError):
        gateway.SourceCapacity("sec", 0, 8, 100)
    with pytest.raises(ValueError):
        gateway.SourceCapacity("sec", 1.0, None, 100)
    assert gateway.SourceCapacity("yahoo", None, None, None).enforceable is False
    assert gateway.SourceCapacity("sec", 1.0, 8, None).enforceable is True, "a rate window is enough (#1520)"


# --- rule 6 enforced (#729 criterion 4): one declaration, queue on the rate, refuse on budget


_NOW = datetime(2026, 9, 16, 22, 20, tzinfo=UTC)
_EARLIER = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)  # today, and long outside any rate window


def _seat_rows(source: str, count: int, *, at: datetime = _EARLIER) -> list[gateway.CallRecord]:
    return [gateway.CallRecord(source=source, endpoint="x", caller="earlier", called_at=at, ok=True)] * count


def _gate(
    ledger: gateway.MemoryLedger, capacity: gateway.SourceCapacity, *, environment: str = "production"
) -> gateway.CapacityGate:
    return gateway.CapacityGate(
        capacities={capacity.source: capacity},
        environment=environment,
        spent_since=ledger.calls_since,
        recent_calls=ledger.window,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        now=lambda: _NOW,
    )


def test_the_gateways_capacities_are_the_registrys_and_nothing_else() -> None:
    """One declaration (2026-09-16 design audit: the gateway's own table said SEC 8/s and
    moomoo 60/30 s while the registry said 10/s and 8/30 s, and nothing read the registry)."""
    from data_engine.datahub.production_topt.source_registrations import LEDGER_CAPACITIES

    assert set(gateway.CAPACITIES) == set(LEDGER_CAPACITIES)
    for seat, declared in LEDGER_CAPACITIES.items():
        view = gateway.CAPACITIES[seat]
        assert (view.window_seconds, view.calls_per_window, view.daily_budget, view.account) == (
            declared.window_seconds,
            declared.calls_per_window,
            declared.daily_budget,
            declared.account,
        ), seat
    assert 'SourceCapacity("' not in (SRC / "sources" / "gateway.py").read_text(), "the gateway writes no number"
    # The whole-account figures are the published limits of the vendors (the contract row holds them).
    sec, nport, moomoo, twelve = (gateway.CAPACITIES[seat] for seat in ("sec", "nport", "moomoo", "twelvedata"))
    assert (sec.calls_per_window, sec.window_seconds, sec.daily_budget) == (10, 1.0, None), "SEC: 10 a second"
    assert (nport.calls_per_window, nport.pool) == (10, ("sec", "nport")), "N-PORT shares the SEC counter"
    assert (moomoo.calls_per_window, moomoo.window_seconds) == (30, 30.0), "moomoo: 30 requests in 30 s"
    assert (twelve.calls_per_window, twelve.window_seconds, twelve.daily_budget) == (8, 60.0, 800)


def test_every_deployed_ledger_source_has_a_declared_seat() -> None:
    from data_engine.sources import llm
    from truealpha_contracts.models import DataSource

    vendors = {source.value for source in DataSource} - {"release"}  # release-frozen config calls nobody
    assert vendors | {llm.SOURCE} <= set(gateway.CAPACITIES)
    assert "search" not in gateway.CAPACITIES, "the placeholder seat had no caller (#1520)"


def test_a_shared_seat_spends_only_this_environments_share() -> None:
    """One Twelve Data key, two environments, two ledgers (init.md §3.1). Each enforces its
    declared share from the only ledger it can read, so together they stay inside 800/day
    and 8/minute — the August freeze was the two of them spending one allowance blind."""
    seat = gateway.CAPACITIES["twelvedata"]
    production, staging = seat.in_environment("production"), seat.in_environment("staging")
    assert production is not None and staging is not None
    assert (production.daily_budget, production.calls_per_window) == (480, 4)
    assert (staging.daily_budget, staging.calls_per_window) == (320, 3)
    assert seat.in_environment("preview") is None, "a share of 0 is a share below one call"
    assert seat.in_environment("local_dev") is None, "an environment the table does not name has no share"

    production_ledger, staging_ledger = gateway.MemoryLedger(), gateway.MemoryLedger()
    production_ledger.extend(_seat_rows("twelvedata", 480))
    staging_ledger.extend(_seat_rows("twelvedata", 319))
    with pytest.raises(gateway.BudgetExhausted, match=r"daily budget 480 spent \(480 calls today\) in production"):
        _gate(production_ledger, seat).admit("twelvedata")
    _gate(staging_ledger, seat, environment="staging").admit("twelvedata")  # staging's 320th
    with pytest.raises(gateway.ShareBelowOneCall, match="share_below_one_call: the twelvedata account .* local_dev"):
        _gate(gateway.MemoryLedger(), seat, environment="local_dev").admit("twelvedata")


def test_a_call_past_the_budget_is_refused_before_anything_is_sent(call_ledger, monkeypatch) -> None:
    """Through the deployed Yahoo client: the refusal is raised where the request would
    have been made, the client is never asked, no row is written, and the refusal is
    announced to whoever audits the scope."""
    from data_engine.sources import yahoo

    call_ledger.extend(_seat_rows("yahoo", 2))
    client = _Client()  # nothing queued: a request that got through would fail differently
    monkeypatch.setattr(yahoo.httpx, "Client", lambda **kwargs: client)
    refused: list[gateway.CapacityExceeded] = []
    gate = _gate(call_ledger, gateway.SourceCapacity("yahoo", 1.0, 100, 2))
    with gateway.on_capacity_refusal(refused.append), gateway.capacity_scope(gate):
        with pytest.raises(gateway.BudgetExhausted, match=r"daily budget 2 spent \(2 calls today\)"):
            yahoo.fetch_daily_bars("AAPL", end=date(2026, 9, 15))

    assert client.calls == [], "the vendor was never asked"
    assert len(call_ledger) == 2, "a refused call writes no row"
    assert [(error.source, error.budget, error.spent) for error in refused] == [("yahoo", 2, 2)]


def test_outside_a_capacity_scope_a_call_is_recorded_not_gated(call_ledger, monkeypatch) -> None:
    from data_engine.sources import yahoo

    call_ledger.extend(_seat_rows("yahoo", 5000, at=datetime.now(UTC)))
    monkeypatch.setattr(yahoo.httpx, "Client", lambda **kwargs: _Client(_Response(b'{"chart": {"result": []}}')))
    assert yahoo.fetch_daily_bars("AAPL", end=date(2026, 9, 15)) == []
    assert len(call_ledger) == 5001


def test_the_gate_counts_its_own_admissions_when_the_ledger_lags() -> None:
    """A call in flight is not a row yet: the gate's own count is a floor under the ledger's."""
    gate = gateway.CapacityGate(
        capacities={"search": gateway.SourceCapacity("search", 60.0, 20, 1)},
        environment="production",
        spent_since=lambda _source, _since: 0,
        recent_calls=None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        now=lambda: _NOW,
    )
    gate.admit("search")
    with pytest.raises(gateway.BudgetExhausted):
        gate.admit("search")


def test_the_rate_window_is_the_one_every_process_writes() -> None:
    """A QQQ tick and a canary tick of one environment are two processes: the window is
    read from the ledger both write, so one waits for the other's calls to age out."""
    ledger = gateway.MemoryLedger()
    for age in (0.5, 0.25):  # another process's two calls; the seat allows 2 per second
        ledger.append(
            gateway.CallRecord(
                source="sec", endpoint="x", caller="other", called_at=_NOW - timedelta(seconds=age), ok=True
            )
        )
    clock, now, slept = [0.0], [_NOW], []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        now[0] += timedelta(seconds=seconds)

    gate = gateway.CapacityGate(
        capacities={"sec": gateway.SourceCapacity("sec", 1.0, 2, 100)},
        environment="production",
        spent_since=ledger.calls_since,
        recent_calls=ledger.window,
        clock=lambda: clock[0],
        sleep=sleep,
        now=lambda: now[0],
    )
    gate.admit("sec")
    assert slept == [pytest.approx(0.51)], "waited for the older call to leave the window, no longer"


def test_the_explicit_gateway_waits_for_other_processes_in_the_shared_window(call_ledger) -> None:
    """Review on #900: the standards lane's `SourceGateway` paces against the ledger's
    window too, so it cannot add its 8/s to a capture tick's 8/s on the same SEC seat."""
    call_ledger.extend(
        gateway.CallRecord(source="sec", endpoint="x", caller="tick", called_at=_NOW - timedelta(seconds=age), ok=True)
        for age in (0.5, 0.25)
    )
    clock, now, slept = [0.0], [_NOW], []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        now[0] += timedelta(seconds=seconds)

    gw = gateway.SourceGateway(
        _Ledger(),
        caller="standards",
        capacities={"sec": gateway.SourceCapacity("sec", 1.0, 2, 100)},
        clock=lambda: clock[0],
        sleep=sleep,
        now=lambda: now[0],
    )
    assert gw.call("sec", "submissions", lambda: "ok", identity=_key("sec", "submissions")) == "ok"
    assert slept == [pytest.approx(0.51)]


def test_a_window_that_never_drains_is_refused_rather_than_waited_on_forever() -> None:
    clock = [0.0]
    gate = gateway.CapacityGate(
        capacities={"sec": gateway.SourceCapacity("sec", 1.0, 2, 100)},
        environment="production",
        spent_since=lambda _source, _since: 0,
        recent_calls=lambda _source, _since: (5, _NOW),
        clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        now=lambda: _NOW,
        max_wait_seconds=3.0,
    )
    with pytest.raises(gateway.CapacityExceeded, match="stayed full for 3 s") as refused:
        gate.admit("sec")
    assert not isinstance(refused.value, gateway.BudgetExhausted)


def test_a_call_is_admitted_once_however_it_is_wrapped(call_ledger) -> None:
    """`SourceGateway.call` admits through its own gate, and a nested block belongs to the
    outer call: neither is admitted a second time by the scope's gate."""
    from data_engine.sources import sec

    admitted: list[str] = []

    class Counting(gateway.CapacityGate):
        def admit(self, source: str) -> None:
            admitted.append(source)

    body = json.dumps({"0": {"ticker": "AAPL", "cik_str": 320193}}).encode()
    gw = _gateway(_Ledger(), gateway.SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    with gateway.capacity_scope(Counting()):
        gw.call(
            "sec",
            "company_tickers",
            lambda: sec.ticker_cik_index(_Client(_Response(body))),
            identity=sec.company_tickers_key(),
        )
        with gateway.record_call("yahoo", "chart", caller="outer", identity=_key("yahoo", "chart")) as call:
            with gateway.record_call("yahoo", "chart", caller="inner", identity=_key("yahoo", "chart")):
                call.observe(status_code=200, body=b"{}")
    assert admitted == ["yahoo"]


def test_the_gates_ledger_reads_follow_the_writer(call_ledger, monkeypatch) -> None:
    call_ledger.extend(_seat_rows("sec", 3))
    # The unit is the request: a model call's token cost is not three thousand calls.
    call_ledger.append(
        gateway.CallRecord(
            source="filing-extraction-model", endpoint="x", caller="x", called_at=_EARLIER, cost=Decimal(3000)
        )
    )
    assert gateway.ledger_calls_since("filing-extraction-model", _EARLIER) == 1
    since = _EARLIER - timedelta(seconds=1)
    assert gateway.ledger_calls_since("sec", since) == 3
    assert gateway.ledger_window("sec", since) == (3, _EARLIER)
    monkeypatch.setattr(gateway.settings, "external_call_ledger", "off")
    assert (gateway.ledger_calls_since("sec", since), gateway.ledger_window("sec", since)) == (0, (0, None))
    monkeypatch.setattr(gateway.settings, "external_call_ledger", "postgres")
    monkeypatch.setattr(gateway, "_writer", lambda record: None)  # a sink nothing can read back
    assert (gateway.ledger_calls_since("sec", since), gateway.ledger_window("sec", since)) == (0, (0, None))


@pytest.fixture
def warehouse_seat(monkeypatch):
    """The real `staging.api_call_ledger` behind the gateway's autocommit connection, under
    a seat name no other test writes; its rows are deleted afterwards."""
    import os
    import uuid

    import psycopg
    from truealpha_runtime.testing import guard_db_write

    url = guard_db_write()
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    monkeypatch.setattr(gateway, "_writer", gateway.postgres_writer)
    monkeypatch.setattr(gateway.settings, "external_call_ledger", "postgres")
    seat = f"gate-test-{uuid.uuid4().hex[:12]}"
    try:
        yield seat
    finally:
        gateway._pg_execute("delete from staging.api_call_ledger where source = %s", (seat,))


def test_the_gate_enforces_the_warehouse_ledger_every_process_shares(warehouse_seat) -> None:
    seat = warehouse_seat
    started = datetime.now(UTC)
    capacity = gateway.SourceCapacity(seat, 60.0, 100, 2)
    with gateway.capacity_scope(gateway.CapacityGate(capacities={seat: capacity}, environment="production")):
        for _ in range(2):
            with gateway.record_call(seat, "probe", caller="test", identity=_key(seat, "probe")) as call:
                call.observe(status_code=200, body=b"{}")
        with pytest.raises(gateway.BudgetExhausted, match="daily budget 2 spent"):
            with gateway.record_call(seat, "probe", caller="test", identity=_key(seat, "probe")):
                raise AssertionError("a refused call never runs")

    assert gateway.ledger_calls_since(seat, started - timedelta(seconds=1)) == 2, "the refusal wrote nothing"
    count, oldest = gateway.ledger_window(seat, started - timedelta(seconds=1))
    assert count == 2 and oldest is not None and oldest >= started - timedelta(seconds=1)
    # A second process's gate has admitted nothing itself, and still finds the budget spent.
    with pytest.raises(gateway.BudgetExhausted):
        gateway.CapacityGate(capacities={seat: capacity}, environment="production").admit(seat)


def test_the_warehouse_ledger_stores_the_attempt_columns_with_their_types(warehouse_seat) -> None:
    """#1444: attempt, provider code and request parameters reach the table; a row of a source
    that sets none of them leaves the columns null."""
    seat = warehouse_seat
    params = {"model": "m", "thinking": "disabled", "max_tokens": 300}
    with gateway.record_call(seat, "chat.completions", caller="test", identity=_key(seat, "chat.completions")) as call:
        call.observe(status_code=429, body=b"{}")
        call.attempt, call.provider_code, call.request_params = 2, "1302", params
        call.fail("HTTP 429")
    with gateway.record_call(seat, "plain", caller="test", identity=_key(seat, "plain")) as call:
        call.observe(status_code=200, body=b"{}")
    rows = gateway._pg_execute(
        "select endpoint, attempt, provider_code, request_params, pg_typeof(attempt)::text, "
        "pg_typeof(request_params)::text from staging.api_call_ledger where source = %s order by id",
        (seat,),
    ).fetchall()
    assert rows == [
        ("chat.completions", 2, "1302", params, "smallint", "jsonb"),
        ("plain", None, None, None, "smallint", "jsonb"),
    ]


_NEW_YORK = ZoneInfo("America/New_York")


def test_the_daily_calls_split_the_warehouse_ledger_by_caller_and_utc_day(warehouse_seat) -> None:
    """#1277: the QQQ price lane budgets against the other callers of its seat, per UTC day.

    The deployed read is the warehouse SQL. The test double answers the same question from its rows.
    """
    seat = warehouse_seat
    records = [
        gateway.CallRecord(seat, "probe", "lane-a", datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC), ok=True),
        gateway.CallRecord(seat, "probe", "lane-a", datetime(2026, 10, 15, 0, 0, 1, tzinfo=UTC), ok=True),
        gateway.CallRecord(seat, "probe", "lane-b", datetime(2026, 10, 15, 0, 0, 1, tzinfo=UTC), ok=False),
        # 20:30 in New York is 00:30 UTC on the next day: the day is the UTC day.
        gateway.CallRecord(seat, "probe", "lane-b", datetime(2026, 10, 15, 20, 30, tzinfo=_NEW_YORK), ok=True),
    ]
    for record in records:
        gateway.postgres_writer(record)
    since, until = datetime(2026, 10, 14, tzinfo=UTC), datetime(2026, 10, 17, tzinfo=UTC)
    expected = {
        ("lane-a", date(2026, 10, 14)): 1,
        ("lane-a", date(2026, 10, 15)): 1,
        ("lane-b", date(2026, 10, 15)): 1,
        ("lane-b", date(2026, 10, 16)): 1,
    }

    assert gateway.ledger_daily_calls(seat, since, until) == expected
    # `since` is included and `until` is not.
    assert gateway.ledger_daily_calls(seat, records[1].called_at, records[3].called_at) == {
        ("lane-a", date(2026, 10, 15)): 1,
        ("lane-b", date(2026, 10, 15)): 1,
    }
    assert gateway.MemoryLedger(records).daily_calls(seat, since, until) == expected


# --- #1521 step 1: one writer for every external call ------------------------------------


@pytest.fixture
def moomoo_caller(warehouse_seat):
    """A caller name no other test writes, and an empty moomoo pacing window; the moomoo rows it
    wrote are deleted afterwards (the seat name `moomoo` is fixed, so the caller is the key)."""
    from data_engine.sources import moomoo_ledger

    moomoo_ledger.reset_stored_history_kl_quota()
    caller = f"{warehouse_seat}-caller"
    try:
        yield caller
    finally:
        gateway._pg_execute("delete from staging.api_call_ledger where source = 'moomoo' and caller = %s", (caller,))


def test_a_moomoo_call_writes_one_row_through_emit(moomoo_caller, monkeypatch) -> None:
    from data_engine.sources import moomoo as mm

    emitted: list[gateway.CallRecord] = []
    real_emit = gateway.emit

    def spy(record, *args, **kwargs):
        emitted.append(record)
        return real_emit(record, *args, **kwargs)

    monkeypatch.setattr(gateway, "emit", spy)
    with gateway.run_scope("dagster:moomoo-run"):
        payload = mm._call(
            object(),
            "get_research_analyst_consensus",
            moomoo_caller,
            lambda ctx: (mm.moomoo.RET_OK, {"rating": "buy"}),
            request_uri="US.AAPL",
            identity=FetchKey("moomoo", "get_research_analyst_consensus", "symbol:TEST", "2026-10-27"),
        )

    assert payload == {"rating": "buy"}
    assert [(r.source, r.endpoint, r.caller) for r in emitted] == [
        ("moomoo", "get_research_analyst_consensus", moomoo_caller)
    ], "the one writer: the row passes `emit`, not a second INSERT path"
    raw = mm.canonical_moomoo_bytes({"rating": "buy"})
    rows = gateway._pg_execute(
        "select ok, status_code, request_uri, payload_sha256, byte_length, run_key "
        "from staging.api_call_ledger where source = 'moomoo' and caller = %s",
        (moomoo_caller,),
    ).fetchall()
    assert rows == [(True, 0, "moomoo://US.AAPL", hashlib.sha256(raw).hexdigest(), len(raw), "dagster:moomoo-run")]


def test_a_row_the_ledger_rejects_leaves_the_moomoo_payload_with_the_caller(warehouse_seat, caplog) -> None:
    """Decision A of #1521: the vendor answer is kept and the full row goes to the log. Postgres text
    cannot hold a NUL byte, so the ledger rejects this row for real."""
    from data_engine.sources import moomoo as mm

    with caplog.at_level(logging.ERROR, logger="data_engine.sources.gateway"):
        payload = mm._call(
            object(),
            "get_company_profile",
            f"{warehouse_seat}\x00",
            lambda ctx: (mm.moomoo.RET_OK, "kept"),
            identity=FetchKey("moomoo", "get_company_profile", "symbol:TEST", "2026-10-27"),
        )

    assert payload == "kept"
    assert "external call ledger write failed" in caplog.text
    assert '"source": "moomoo"' in caplog.text, "the unrecorded row is at least in the run log"


def test_a_gateway_call_row_survives_the_rollback_of_the_callers_transaction(warehouse_seat) -> None:
    import psycopg

    seat = warehouse_seat
    capacity = gateway.SourceCapacity(seat, 60.0, 100, 10)
    with psycopg.connect(gateway.settings.database_url) as caller:
        gw = gateway.SourceGateway(
            caller, caller="rollback-test", capacities={seat: capacity}, environment="production"
        )
        assert gw.call(seat, "probe", lambda: "answer", identity=_key(seat, "probe")) == "answer"
        caller.rollback()

    row = gateway._pg_execute("select count(*) from staging.api_call_ledger where source = %s", (seat,)).fetchone()
    assert row == (1,), "the vendor was called; the caller's rollback does not undo the audit row"
