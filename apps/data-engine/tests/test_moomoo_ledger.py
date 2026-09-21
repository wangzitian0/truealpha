import pytest
from data_engine.sources import moomoo_ledger as ledger


@pytest.fixture(autouse=True)
def _json_backend(monkeypatch):
    # Pin the json backend: a developer .env with MOOMOO_LEDGER_BACKEND=postgres
    # must not make these tests write fake rows into the real append-only
    # staging.api_call_ledger (see test_kg_integration for the postgres path).
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "json")


def test_gate_allows_calls_under_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 2)

    ledger.gate("get_research_analyst_consensus", "test")
    ledger.record("get_research_analyst_consensus", "test", ok=True)
    assert ledger.calls_this_month() == 1


def test_gate_blocks_once_budget_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 1)

    ledger.gate("get_rating_change", "test")
    ledger.record("get_rating_change", "test", ok=True)

    try:
        ledger.gate("get_rating_change", "test")
        raise AssertionError("expected BudgetExceededError")
    except ledger.BudgetExceededError:
        pass


def test_record_counts_failed_calls_too(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 10)

    ledger.record("get_rating_change", "test", ok=False)
    assert ledger.calls_this_month() == 1


def test_record_preserves_payload_sha_and_byte_length(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    ledger.record(
        "get_history_kline",
        "test",
        ok=True,
        request_uri="moomoo://US.AAPL",
        payload_sha256="abc123sha",
        byte_length=42,
    )
    calls = ledger._load()["calls"]
    assert len(calls) == 1
    assert calls[0]["request_uri"] == "moomoo://US.AAPL"
    assert calls[0]["payload_sha256"] == "abc123sha"
    assert calls[0]["byte_length"] == 42


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
    """Rule 6: the moomoo seat is declared once (`LEDGER_CAPACITIES`); MOOMOO_CALLS_PER_30S
    may pace more gently but never past the declaration."""
    from data_engine.sources import gateway

    seat = gateway.CAPACITIES["moomoo"]
    assert (seat.calls_per_window, seat.window_seconds) == (8, 30.0)
    assert ledger.settings.moomoo_calls_per_30s == seat.calls_per_window, "the shipped default is the declaration"
    monkeypatch.setattr(ledger.settings, "moomoo_calls_per_30s", 50)
    ledger._recent_calls.clear()
    clock = {"t": 0.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    for _ in range(9):
        ledger.throttle(now=lambda: clock["t"], sleep=fake_sleep)
    assert len(sleeps) == 1, "the ninth call waits: 50 in the env cannot raise the declared 8"
    ledger._recent_calls.clear()
