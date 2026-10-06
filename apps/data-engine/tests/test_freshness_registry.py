"""Each scheduled lane has a cadence in the served-artifact registry (#1062).

`mart.served_artifact` maps every served artifact to a cadence family, and the family names the
read-time age limit. A lane that has no registry row would serve data with no limit at all.

No database here. The registry is the seed in
`db/migrations/20261006T1020_datahub_served_head_freshness.sql`, read from the file text. The
lanes are the Dagster definitions that the deployed image loads.
`libs/runtime/tests/test_served_head_freshness.py` proves that the file text and the real
database agree. So reading the text is reading the registry.

A new lane, a new schedule, a renamed schedule or a dead registry row turns a test red.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path

import pytest
from data_engine.lanes import LANE_MODULES, lane_definitions
from truealpha_runtime.testing import SeedValue, read_seed_rows

MIGRATION = (
    Path(__file__).resolve().parents[3] / "db" / "migrations" / "20261006T1020_datahub_served_head_freshness.sql"
)

Row = Mapping[str, SeedValue]


def registry() -> list[Row]:
    return read_seed_rows(MIGRATION, "mart.served_artifact")


def limit_keys() -> set[str]:
    return {str(row["limit_key"]) for row in read_seed_rows(MIGRATION, "mart.freshness_limit")}


def lane_name(module: str) -> str:
    return module.rsplit(".", 1)[1]


def lane_schedules() -> dict[str, set[str]]:
    """Schedule names by lane module, as the deployed definitions declare them."""
    return {
        module: {schedule.name for schedule in definitions.schedules or ()}
        for module, definitions in lane_definitions().items()
    }


# The three checks are pure functions. A test runs each on a synthetic registry.
# That shows that each check reports what it must.


def lanes_without_a_row(modules: Iterable[str], rows: Iterable[Row]) -> list[str]:
    covered = {row["lane"] for row in rows}
    return sorted(module for module in modules if lane_name(module) not in covered)


def schedules_without_a_row(schedules: Mapping[str, Collection[str]], rows: Iterable[Row]) -> list[str]:
    named = {row["schedule_name"] for row in rows if row["schedule_name"] is not None}
    return sorted(name for names in schedules.values() for name in names if name not in named)


def rows_without_a_schedule(schedules: Mapping[str, Collection[str]], rows: Iterable[Row]) -> list[str]:
    """Rows that name a schedule that no lane declares. The row is dead."""
    declared = {name for names in schedules.values() for name in names}
    return sorted(
        str(row["artifact_key"])
        for row in rows
        if row["schedule_name"] is not None and row["schedule_name"] not in declared
    )


def schedules_in_more_than_one_row(rows: Iterable[Row]) -> list[str]:
    """Schedule names that appear in two or more rows. The table has no unique constraint on
    the name. With one, an upsert keyed on `artifact_key` would fail after a rename."""
    counts = Counter(row["schedule_name"] for row in rows if row["schedule_name"] is not None)
    return sorted(str(name) for name, count in counts.items() if count > 1)


def test_every_lane_module_has_a_registry_row() -> None:
    assert lanes_without_a_row(LANE_MODULES, registry()) == []


def test_every_schedule_a_lane_declares_is_in_the_registry() -> None:
    assert schedules_without_a_row(lane_schedules(), registry()) == []


def test_every_registry_schedule_exists_in_a_lane() -> None:
    assert rows_without_a_schedule(lane_schedules(), registry()) == []


def test_each_declared_schedule_appears_in_exactly_one_row() -> None:
    rows = registry()
    assert schedules_in_more_than_one_row(rows) == []
    for names in lane_schedules().values():
        for name in names:
            assert sum(1 for row in rows if row["schedule_name"] == name) == 1, name


def test_a_schedule_in_two_rows_is_reported() -> None:
    rows = [*registry(), {"artifact_key": "twin", "lane": "capture", "schedule_name": "topt_live_schedule"}]
    assert schedules_in_more_than_one_row(rows) == ["topt_live_schedule"]


def test_a_registry_schedule_belongs_to_the_lane_the_row_names() -> None:
    """A row that says lane X for a schedule that lane Y declares sends a reader to the wrong code."""
    declared = {name: lane_name(module) for module, names in lane_schedules().items() for name in names}
    wrong = {
        str(row["artifact_key"]): (row["lane"], declared[str(row["schedule_name"])])
        for row in registry()
        if row["schedule_name"] is not None and declared.get(str(row["schedule_name"])) != row["lane"]
    }
    assert wrong == {}


def test_the_scan_is_not_empty() -> None:
    """Green while empty: a reader that finds no schedule would pass every check above."""
    rows = registry()
    schedules = lane_schedules()
    assert len(rows) >= len(LANE_MODULES), (len(rows), len(LANE_MODULES))
    assert len({name for names in schedules.values() for name in names}) >= 8
    assert sum(1 for row in rows if row["schedule_name"] is not None) >= 8
    assert {row["artifact_key"] for row in rows} >= {"head:topt", "head:qqq", "head:canary"}


def test_every_row_has_a_known_cadence_and_never_the_withhold_limit() -> None:
    rows = registry()
    keys = limit_keys()
    assert {"daily", "weekly", "quarterly", "withhold"} <= keys
    unknown = {str(row["artifact_key"]): row["family"] for row in rows if row["family"] not in keys}
    assert unknown == {}
    assert [row["artifact_key"] for row in rows if row["family"] == "withhold"] == []


def test_a_wired_row_names_the_universe_pattern_that_its_age_source_reads() -> None:
    """`wired` is a readiness flag. Today the only age source is the pointer's `advanced_at`.
    The view reaches it through `universe_like`. A wired row with no pattern names a source that
    no view reads. This test checks that fact in the data. It does not stop a consumer from
    reading an artifact that is not wired. Nothing enforces that."""
    wired = {str(row["artifact_key"]): row["universe_like"] for row in registry() if row["wired"]}
    assert wired, "no row is wired, so the registry ages nothing"
    assert [key for key, universe_like in wired.items() if universe_like is None] == []


def test_market_data_is_daily_and_internal_until_it_is_wired() -> None:
    """The lane runs on weekdays only. After a long weekend its age can pass 72 hours.
    Decide the family again when the row is wired."""
    (row,) = [row for row in registry() if row["artifact_key"] == "market-data"]
    assert (row["family"], row["served_to"], row["wired"]) == ("daily", "internal", False)


def test_the_artifact_keys_and_schedule_names_are_unique() -> None:
    rows = registry()
    keys = [row["artifact_key"] for row in rows]
    names = [row["schedule_name"] for row in rows if row["schedule_name"] is not None]
    assert len(keys) == len(set(keys))
    assert len(names) == len(set(names))


# --- each check goes red on the input it exists for ------------------------------------------


def test_a_new_lane_without_a_row_is_reported() -> None:
    rows = [row for row in registry() if row["lane"] != "quality"]
    assert lanes_without_a_row(LANE_MODULES, rows) == ["data_engine.lanes.quality"]
    assert lanes_without_a_row([*LANE_MODULES, "data_engine.lanes.brand_new"], registry()) == [
        "data_engine.lanes.brand_new"
    ]


def test_a_new_schedule_without_a_row_is_reported() -> None:
    schedules = {**lane_schedules(), "data_engine.lanes.capture": {"topt_live_schedule", "a_new_schedule"}}
    assert schedules_without_a_row(schedules, registry()) == ["a_new_schedule"]


def test_a_row_for_a_schedule_no_lane_declares_is_reported() -> None:
    rows = [*registry(), {"artifact_key": "dead", "lane": "capture", "schedule_name": "gone_schedule"}]
    assert rows_without_a_schedule(lane_schedules(), rows) == ["dead"]


# --- the seed reader refuses what it cannot read ----------------------------------------------


def test_the_seed_reader_names_a_malformed_seed_instead_of_returning_fewer_rows(tmp_path: Path) -> None:
    seed = tmp_path / "seed.sql"
    seed.write_text("insert into mart.t (a, b) values\n    ('x', 1),\n    ('y', now())\non conflict do nothing;\n")
    with pytest.raises(ValueError, match="cannot read the seed"):
        read_seed_rows(seed, "mart.t")
    seed.write_text("insert into mart.t (a, b) values\n    ('x', 1),\n    ('y')\non conflict do nothing;\n")
    with pytest.raises(ValueError, match="1 values, not 2"):
        read_seed_rows(seed, "mart.t")
    seed.write_text("select 1;\n")
    with pytest.raises(ValueError, match="expected exactly 1"):
        read_seed_rows(seed, "mart.t")


def test_the_seed_reader_reads_quotes_nulls_booleans_numbers_and_comments(tmp_path: Path) -> None:
    seed = tmp_path / "seed.sql"
    seed.write_text(
        "insert into mart.t (a, b, c, d)\nvalues\n    -- a comment, with a comma\n"
        "    ('it''s', 7, null, true),\n    ('--not a comment', -2, 'x', FALSE)\non conflict do nothing;\n"
    )
    assert read_seed_rows(seed, "mart.t") == [
        {"a": "it's", "b": 7, "c": None, "d": True},
        {"a": "--not a comment", "b": -2, "c": "x", "d": False},
    ]
