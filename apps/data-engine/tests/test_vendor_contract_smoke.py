"""The vendor contract smoke script calls the moomoo client with the identity it requires (#1521, step 2).

`scripts/` is outside mypy, so a signature change reaches it only through a test. The script catches every
exception and reports it as a failed check, so a call that omits `identity` reads as an OpenD outage.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt import moomoo_origin

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT = TESTS_DIR.parent / "scripts" / "vendor_contract_smoke.py"


class _StubOpenD:
    """An `OpenDClient` whose methods require `identity`, as the deployed one does. It records each key."""

    keys: list[FetchKey] = []

    def history_kline(self, code: str, *, start: date, end: date, identity: FetchKey) -> list[dict[str, Any]]:
        self.keys.append(identity)
        from production_topt.test_moomoo_origin import _bar

        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        return [_bar(day.isoformat(), 334.55 + offset) for offset, day in enumerate(days)]

    def financial_statements(self, code: str, *, statement_type: int, identity: FetchKey) -> dict[str, Any]:
        self.keys.append(identity)
        from production_topt.test_moomoo_origin import _statements

        income, balance = _statements("DDOG")
        return {moomoo_origin.INCOME_STATEMENT: income, moomoo_origin.BALANCE_SHEET: balance}[statement_type]


def test_the_moomoo_checks_pass_against_a_client_that_requires_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.syspath_prepend(str(TESTS_DIR))
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
