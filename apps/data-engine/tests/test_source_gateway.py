"""The source gateway (#729): declared capacity, throttle, ledger row per call."""

from datetime import UTC, datetime

import pytest
from data_engine.sources.gateway import CapacityExceeded, SourceCapacity, SourceGateway


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


def _gateway(ledger: _Ledger, capacity: SourceCapacity, clock: list[float], slept: list[float]) -> SourceGateway:
    return SourceGateway(
        ledger,
        caller="test",
        capacities={capacity.source: capacity},
        clock=lambda: clock[0],
        sleep=lambda seconds: (slept.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
    )


def test_every_call_lands_in_the_ledger_with_its_caller_and_outcome() -> None:
    ledger = _Ledger()
    gateway = _gateway(ledger, SourceCapacity("sec", 1.0, 8, 100), [0.0], [])

    assert gateway.call("sec", "submissions", lambda: "ok") == "ok"
    with pytest.raises(RuntimeError, match="boom"):
        gateway.call("sec", "archive", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert ledger.rows == [("sec", "submissions", "test", True), ("sec", "archive", "test", False)]


def test_a_call_inside_a_full_window_waits_for_the_window_to_roll() -> None:
    clock, slept = [0.0], []
    gateway = _gateway(_Ledger(), SourceCapacity("sec", 1.0, 2, 100), clock, slept)

    gateway.call("sec", "a", lambda: None)
    gateway.call("sec", "b", lambda: None)
    gateway.call("sec", "c", lambda: None)  # third call in a 2-per-second window

    assert slept and slept[0] == pytest.approx(1.0)


def test_the_daily_budget_refuses_rather_than_fires() -> None:
    ledger = _Ledger(spent_today=5)
    gateway = _gateway(ledger, SourceCapacity("search", 60.0, 20, 5), [0.0], [])

    with pytest.raises(CapacityExceeded, match="daily budget 5 spent"):
        gateway.call("search", "query", lambda: "must not run")
    assert ledger.rows == []  # a refused call spends nothing and records nothing


def test_capacity_one_defers_the_second_call_of_the_day() -> None:
    """#729 criterion 4, #735 acceptance: red-proof shape — with capacity 1 the second
    call is refused, not fired."""
    ledger = _Ledger(spent_today=0)
    gateway = _gateway(ledger, SourceCapacity("filing-extraction-model", 60.0, 10, 1), [0.0], [])

    gateway.call("filing-extraction-model", "select", lambda: "first")
    with pytest.raises(CapacityExceeded):
        gateway.call("filing-extraction-model", "select", lambda: "second")
    assert len(ledger.rows) == 1


def test_an_undeclared_source_cannot_be_called() -> None:
    gateway = _gateway(_Ledger(), SourceCapacity("sec", 1.0, 8, 100), [0.0], [])
    with pytest.raises(CapacityExceeded, match="no declared capacity"):
        gateway.call("yahoo", "quote", lambda: None)


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SourceCapacity("sec", 0, 8, 100)
