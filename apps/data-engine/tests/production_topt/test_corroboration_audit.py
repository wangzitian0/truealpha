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
