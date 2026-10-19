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
    raw = mm.canonical_moomoo_bytes(bars)
    recorded = ledger._load()["calls"][-1]
    assert recorded["payload_sha256"] == hashlib.sha256(raw).hexdigest()
    assert recorded["byte_length"] == len(raw)
