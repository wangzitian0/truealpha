"""The lost-corroboration record (#885): what it logs, what it counts, and where."""

from __future__ import annotations

import logging

from data_engine.datahub.production_topt.corroboration_audit import (
    FETCH,
    PERSIST,
    corroboration_tally,
    record_lost_corroboration,
)


def _raised() -> ValueError:
    try:
        raise ValueError("refused quantity")
    except ValueError as error:
        return error


def test_the_warning_carries_the_given_errors_traceback_even_outside_its_except(caplog) -> None:
    """`exc_info=<exception>` is the stdlib's own form (Logger._log turns an exception
    instance into its (type, value, traceback) triple since Python 3.5), so the record
    names the error it was handed, not whatever `sys.exc_info()` holds at call time."""
    error = _raised()
    with caplog.at_level(logging.WARNING):
        try:
            raise KeyError("an unrelated exception being handled")
        except KeyError:
            record_lost_corroboration("twelve-data", FETCH, "AAPL", error)
    [record] = caplog.records
    assert record.exc_info is not None
    assert record.exc_info[1] is error and record.exc_info[2] is error.__traceback__


def test_the_tally_counts_per_origin_and_stage_and_only_inside_its_block(caplog) -> None:
    error = _raised()
    record_lost_corroboration("twelve-data", FETCH, "AAPL", error)  # no tally bound: logged, not counted
    with corroboration_tally() as tally:
        assert tally.summary() == "corroborations refused 0"
        record_lost_corroboration("twelve-data", FETCH, "AAPL", error)
        record_lost_corroboration("twelve-data", FETCH, "MSFT", error)
        record_lost_corroboration("moomoo-kline", PERSIST, "AAPL", error)
    record_lost_corroboration("twelve-data", FETCH, "NVDA", error)  # after the block: not counted
    assert tally.total == 3
    assert tally.summary() == "corroborations refused 3 (moomoo-kline persist 1, twelve-data fetch 2)"
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 5


# -- rule 6 (#729): what the capacity gate refused inside the tick is counted too -------------


def _budget_gate(spent: int):
    from datetime import UTC, datetime

    from data_engine.sources import gateway

    now = datetime(2026, 9, 16, 22, 20, tzinfo=UTC)
    return gateway.CapacityGate(
        capacities={
            "twelvedata": gateway.SourceCapacity("twelvedata", 60.0, 8, 800, environment_shares=(("production", 60),)),
            "yahoo": gateway.SourceCapacity("yahoo", 1.0, 2, 10),
        },
        environment="production",
        spent_since=lambda source, _since: spent if source == "twelvedata" else 0,
        recent_calls=None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        now=lambda: now,
    )


def test_every_gate_refusal_inside_the_tally_is_counted_per_seat_and_kind() -> None:
    import pytest
    from data_engine.sources import gateway

    gate = _budget_gate(spent=480)
    with corroboration_tally() as tally:
        assert tally.refusals() == "capacity refused 0"
        for _ in range(2):
            with pytest.raises(gateway.BudgetExhausted):
                gate.admit("twelvedata")
        with pytest.raises(gateway.CapacityExceeded, match="no declared capacity"):
            gate.admit("never-declared")
        gate.admit("yahoo")  # admitted: nothing to count
    with pytest.raises(gateway.BudgetExhausted):
        gate.admit("twelvedata")  # after the block: not counted

    assert tally.budget_exhausted == 2 and tally.capacity_refused == 3
    assert tally.refusals() == "capacity refused 3 (never-declared capacity 1, twelvedata budget 2)"
    assert tally.total == 0, "a refusal the adapter never reported as a lost corroboration is not one"


def test_a_corroboration_lost_to_the_gate_is_recorded_at_its_own_stage(caplog) -> None:
    import pytest
    from data_engine.sources import gateway

    gate = _budget_gate(spent=480)
    with corroboration_tally() as tally, caplog.at_level(logging.WARNING):
        with pytest.raises(gateway.BudgetExhausted) as refused:
            gate.admit("twelvedata")
        # The fetcher passes FETCH, as it does for any raise; the stage says what happened.
        record_lost_corroboration("twelve-data", FETCH, "AAPL", refused.value)
        record_lost_corroboration("twelve-data", FETCH, "MSFT", gateway.CapacityExceeded("twelvedata", "window"))
    assert tally.summary() == "corroborations refused 2 (twelve-data budget 1, twelve-data capacity 1)"
    assert tally.refusals() == "capacity refused 1 (twelvedata budget 1)"
    lost = [r.getMessage() for r in caplog.records if "lost at budget" in r.getMessage()]
    assert lost and "BudgetExhausted" in lost[0] and "daily budget 480 spent" in lost[0]


def test_a_failing_refusal_listener_never_changes_the_refusal(caplog) -> None:
    import pytest
    from data_engine.sources import gateway

    def broken(_error: gateway.CapacityExceeded) -> None:
        raise RuntimeError("audit sink down")

    with gateway.on_capacity_refusal(broken), caplog.at_level(logging.ERROR):
        with pytest.raises(gateway.BudgetExhausted):
            _budget_gate(spent=480).admit("twelvedata")
    assert "capacity refusal listener failed" in caplog.text
