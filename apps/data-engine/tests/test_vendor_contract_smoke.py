"""The vendor contract smoke script calls the moomoo client with the identity it requires (#1521, step 2).

`scripts/` is outside mypy, so a signature change reaches it only through a test. The script catches every
exception and reports it as a failed check, so a call that omits `identity` reads as an OpenD outage.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from data_engine.datahub.capture_core import moomoo_origin
from data_engine.datahub.fetch_key import FetchKey

pytestmark = pytest.mark.smoke

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT = TESTS_DIR.parent / "scripts" / "vendor_contract_smoke.py"
SAMPLES_DIR = TESTS_DIR.parent / "samples"
_MOOMOO_DDOG_SHA256 = "0971fe838f5d2dbac2242054ff42e176fbca804bf84901b87f66f814ccb9b0b4"


def _bar(day: str, close: float, *, stamp: str = " 00:00:00") -> dict[str, Any]:
    return {
        "code": "US.AAPL",
        "name": "Apple",
        "time_key": f"{day}{stamp}",
        "open": close - 1.5,
        "close": close,
        "high": close + 2.0,
        "low": close - 2.5,
        "pe_ratio": 32.1,
        "turnover_rate": 0.4,
        "volume": 41_000_000,
        "turnover": 41_000_000 * close,
        "change_rate": 0.3,
        "last_close": close - 1.0,
    }


def _statements(ticker: str) -> tuple[dict[str, Any], dict[str, Any]]:
    body = (SAMPLES_DIR / "moomoo" / f"{ticker}.json").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    assert ticker == "DDOG" and digest == _MOOMOO_DDOG_SHA256, f"samples/moomoo/{ticker}.json changed: {digest}"
    sample = json.loads(body)
    return sample["financials_income"]["data"], sample["financials_balance_sheet"]["data"]


class _StubOpenD:
    """An `OpenDClient` whose methods require `identity`, as the deployed one does. It records each key."""

    keys: list[FetchKey] = []

    def history_kline(self, code: str, *, start: date, end: date, identity: FetchKey) -> list[dict[str, Any]]:
        self.keys.append(identity)
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        return [_bar(day.isoformat(), 334.55 + offset) for offset, day in enumerate(days)]

    def financial_statements(self, code: str, *, statement_type: int, identity: FetchKey) -> dict[str, Any]:
        self.keys.append(identity)
        income, balance = _statements("DDOG")
        return {moomoo_origin.INCOME_STATEMENT: income, moomoo_origin.BALANCE_SHEET: balance}[statement_type]


def test_the_moomoo_checks_pass_against_a_client_that_requires_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("vendor_contract_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    monkeypatch.setattr(smoke.settings, "moomoo_opend_host", "opend.test")
    monkeypatch.setattr(smoke.settings, "moomoo_opend_port", 11111)
    monkeypatch.setattr(moomoo_origin, "OpenDClient", _StubOpenD)
    _StubOpenD.keys = []

    smoke.check_moomoo()

    output = capsys.readouterr().out
    assert smoke._FAILURES == [], output
    assert "PASS  moomoo: OpenD reachable and K-line answers" in output
    assert "PASS  moomoo: income statement carries annual rows" in output
    today = datetime.now(UTC).date().isoformat()
    assert _StubOpenD.keys == [
        FetchKey("moomoo", "history_kline", "symbol:US.AAPL", today),
        FetchKey("moomoo", "financial_statements:income", "symbol:US.AAPL", today),
    ], "each request names the resource that it asks for"


def test_skip_unconfigured_and_record_results_to_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unconfigured vendors skip only when requested, and results log cleanly to ingestion_health_log."""
    spec = importlib.util.spec_from_file_location("vendor_contract_smoke_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    monkeypatch.setattr(smoke.settings, "twelve_data_api_key", "")
    monkeypatch.setattr(smoke.settings, "moomoo_opend_host", "")
    monkeypatch.setattr(smoke.settings, "moomoo_opend_port", 0)

    assert smoke.main(["--vendors", "twelvedata,moomoo"]) == 1
    assert smoke.main(["--vendors", "twelvedata,moomoo", "--skip-unconfigured"]) == 0
    assert "SKIP  twelvedata:" in capsys.readouterr().out

    recorded: list[tuple[str, tuple[Any, ...]]] = []

    class _FakeConn:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            recorded.append((sql, params))

    count = smoke.record_results_to_db(
        [
            ("yahoo", "yahoo: chart", True, "5 bars"),
            ("moomoo", "moomoo: kline", False, "timeout"),
        ],
        connection=_FakeConn(),
    )
    assert count == 2
    assert len(recorded) == 2
    assert recorded[0][1][:3] == ("vendor_contract_smoke", "vendor:moomoo:ok", 0)
    assert recorded[1][1][:3] == ("vendor_contract_smoke", "vendor:yahoo:ok", 1)
