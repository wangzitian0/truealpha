import pytest
from data_engine.sources import moomoo as mm
from data_engine.sources import moomoo_ledger as ledger


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    # Pin the json backend: without this, a developer .env with
    # MOOMOO_LEDGER_BACKEND=postgres would make these tests write fake rows
    # into the real append-only staging.api_call_ledger (autocommitted).
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "json")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 10)
    # _call now throttles; keep the shared window empty so unrelated tests
    # can't accumulate into a real 30s sleep.
    ledger._recent_calls.clear()


def test_call_records_success():
    mm._call(ctx=object(), endpoint="ep", caller="test", fn=lambda c: (mm.moomoo.RET_OK, "data"))
    assert ledger.calls_this_month() == 1


def test_call_records_on_exception_and_wraps_it():
    def raises(c):
        raise TimeoutError("network blip")

    with pytest.raises(mm.MoomooConnectionError) as exc_info:
        mm._call(ctx=object(), endpoint="ep", caller="test", fn=raises)
    assert isinstance(exc_info.value.__cause__, TimeoutError)

    # The request may have reached moomoo's server (spending real quota) even
    # though it raised locally — the ledger must still count it, or the gate
    # undercounts real usage.
    assert ledger.calls_this_month() == 1


def test_call_records_failure_return_code():
    with pytest.raises(mm.MoomooConnectionError):
        mm._call(ctx=object(), endpoint="ep", caller="test", fn=lambda c: (mm.moomoo.RET_ERROR, "boom"))
    assert ledger.calls_this_month() == 1


def test_get_history_kline_hashes_bars_without_page_req_key():
    import hashlib

    class _Ctx:
        def request_history_kline(self, code, **kwargs):
            return (mm.moomoo.RET_OK, [{"close": 100.5, "time_key": "2026-07-29 00:00:00"}], "page-token-2")

    bars, next_key = mm.get_history_kline(_Ctx(), "US.AAPL", start="2026-07-15", end="2026-07-29")
    assert next_key == "page-token-2"
    golden_raw = b'[{"close":100.5,"time_key":"2026-07-29 00:00:00"}]'
    assert mm.canonical_moomoo_bytes(bars) == golden_raw
    recorded = ledger._load()["calls"][-1]
    assert recorded["payload_sha256"] == hashlib.sha256(golden_raw).hexdigest()
    assert recorded["byte_length"] == len(golden_raw)


def test_sweep_owner_plate_uses_canonical_moomoo_bytes(monkeypatch):
    import importlib.util
    from collections import Counter
    from pathlib import Path

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "sweep_moomoo_fundamentals.py"
    spec = importlib.util.spec_from_file_location("sweep_moomoo_fundamentals", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured_fetches: list[dict] = []
    monkeypatch.setattr(module.raw_store, "already_fetched", lambda conn, **kw: False)
    monkeypatch.setattr(
        module.raw_store,
        "insert_fetch",
        lambda conn, **kw: captured_fetches.append(kw) or 1,
    )
    monkeypatch.setattr(
        module.mm,
        "get_owner_plate",
        lambda ctx, chunk, caller: [{"code": "US.NVDA", "plate_code": "US.BK2010"}],
    )

    class _Conn:
        def commit(self):
            pass

    stats: Counter = Counter()
    module.sweep_owner_plate(_Conn(), object(), ["US.NVDA"], stats)
    assert len(captured_fetches) == 1
    assert captured_fetches[0]["source_record_id"] == "owner_plate:US.NVDA"
    assert captured_fetches[0]["body"] == mm.canonical_moomoo_bytes(
        [{"code": "US.NVDA", "plate_code": "US.BK2010"}]
    )
    assert stats[("US", "owner_plate", "ok")] == 1

