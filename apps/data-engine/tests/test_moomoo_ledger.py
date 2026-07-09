from data_engine.sources import moomoo_ledger as ledger


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
