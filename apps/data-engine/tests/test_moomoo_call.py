import pytest
from data_engine.sources import moomoo as mm
from data_engine.sources import moomoo_ledger as ledger


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 10)


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
