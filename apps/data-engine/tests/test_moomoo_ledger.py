import pytest
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import moomoo as mm
from data_engine.sources import moomoo_ledger as ledger


def _kline(code: str) -> None:
    """One K-line call on ``code`` through the real wrapper: gate, throttle, call, record."""
    mm._call(
        object(),
        "request_history_kline",
        "test",
        lambda ctx: (mm.moomoo.RET_OK, []),
        request_uri=code,
        identity=FetchKey("moomoo", "request_history_kline", "symbol:TEST", "2026-10-27"),
    )


def test_gate_allows_calls_under_quota(moomoo_ledger_db, monkeypatch):
    monkeypatch.setattr(ledger, "MOOMOO_KLINE_30D_STOCK_QUOTA", 2)

    _kline("US.AAPL")
    assert ledger.calls_this_month() == 1
    assert ledger.kline_distinct_stocks_30d() == {"moomoo://US.AAPL"}


def test_gate_blocks_once_30d_kline_stock_quota_exhausted(moomoo_ledger_db, monkeypatch):
    monkeypatch.setattr(ledger, "MOOMOO_KLINE_30D_STOCK_QUOTA", 1)

    _kline("US.AAPL")

    # Requesting the SAME stock again within the 30-day window does not consume a new stock slot
    ledger.gate("request_history_kline", "test", request_uri="moomoo://US.AAPL")

    # Requesting a SECOND distinct stock exceeds the 30-day stock quota
    with pytest.raises(ledger.BudgetExceededError):
        ledger.gate("request_history_kline", "test", request_uri="moomoo://US.MSFT")


def test_throttle_paces_a_full_window(monkeypatch):
    monkeypatch.setattr(ledger.settings, "moomoo_calls_per_30s", 2)
    ledger._recent_calls.clear()
    clock = {"t": 100.0}
    sleeps = []

    def fake_now():
        return clock["t"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    ledger.throttle(now=fake_now, sleep=fake_sleep)
    ledger.throttle(now=fake_now, sleep=fake_sleep)
    assert sleeps == []  # under the cap: no waiting

    ledger.throttle(now=fake_now, sleep=fake_sleep)
    assert len(sleeps) == 1 and 29.0 < sleeps[0] <= 30.1  # third call waits the window out

    ledger.throttle(now=fake_now, sleep=fake_sleep)
    assert len(sleeps) == 1  # window slid past the first two: no further wait
    ledger._recent_calls.clear()


def test_throttle_reads_its_ceiling_from_the_registry_and_the_setting_only_lowers_it(monkeypatch):
    """Rule 6: the moomoo seat is read once (`LEDGER_CAPACITIES`, the burst limit of the moomoo account);
    MOOMOO_CALLS_PER_30S may pace more gently but never past the declaration."""
    from data_engine.sources import gateway

    seat = gateway.CAPACITIES["moomoo"]
    assert (seat.calls_per_window, seat.window_seconds) == (30, 30.0)
    assert ledger.settings.moomoo_calls_per_30s < seat.calls_per_window, "the shipped default paces more gently"
    monkeypatch.setattr(ledger.settings, "moomoo_calls_per_30s", 50)
    ledger._recent_calls.clear()
    clock = {"t": 0.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    for _ in range(31):
        ledger.throttle(now=lambda: clock["t"], sleep=fake_sleep)
    assert len(sleeps) == 1, "the 31st call waits: 50 in the env cannot raise the declared 30"
    ledger._recent_calls.clear()
