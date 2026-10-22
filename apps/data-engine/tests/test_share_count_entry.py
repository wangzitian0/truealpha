"""The one rule that dates a filed share count (#1277).

The TOPT core build and the live strategy seed both call `share_count_entry`. The tick test
`test_live_share_basis.py::test_a_count_with_no_vintage_entry_is_dated_alike_by_the_strategy_seed_and_the_core`
runs both through the deployed tick. These tests state the rule itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from data_engine.datahub.share_count_entry import share_count_entry

KNOWABLE = datetime(2026, 2, 1, 23, 59, tzinfo=UTC)
FULL = {"accession": "0000000000-26-000001", "filed": "2026-02-01", "period_end": "2026-01-20"}


def test_an_entry_with_both_dates_is_returned_as_it_is() -> None:
    assert share_count_entry({"shares_outstanding": FULL}, "2026-03-15", KNOWABLE) is FULL


def test_a_payload_with_only_shares_period_end_is_dated_from_that_date_and_the_knowable_time() -> None:
    expected = {"period_end": "2026-03-15", "filed": "2026-02-01"}

    assert share_count_entry(None, date(2026, 3, 15), KNOWABLE) == expected
    assert share_count_entry({}, "2026-03-15", KNOWABLE) == expected


def test_a_partial_entry_keeps_its_fields_and_gains_the_missing_dates() -> None:
    entry = {"accession": "0000000000-26-000001", "filed": "2026-01-30"}

    assert share_count_entry({"shares_outstanding": entry}, "2026-01-20", KNOWABLE) == {
        "accession": "0000000000-26-000001",
        "filed": "2026-01-30",
        "period_end": "2026-01-20",
    }


def test_a_payload_with_no_date_gets_no_date() -> None:
    """`live_on_close_basis` then names the count `share_count_undated`."""
    assert share_count_entry(None, None, KNOWABLE) == {}
