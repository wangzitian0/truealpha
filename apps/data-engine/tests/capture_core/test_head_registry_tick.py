"""The tick registers the head of every universe in the record (#1522 step 2b).

`mart.served_artifact` maps a served head to its cadence. The four rows `head:topt`, `head:qqq`, `head:sp500`
and `head:canary` come from the seed of `20261006T1020`: history, and they stay. A universe that the record
adds needs its row too, and the record is the only place that says so. The deployed tick op writes the missing
rows, with no new migration and no write at boot.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.lanes import capture
from truealpha_contracts import universes

_SUPPORT = Path(__file__).resolve().parents[1] / "support"
sys.path.insert(0, str(_SUPPORT))
from deployed_tick import run_deployed_tick  # noqa: E402
from variant_contract import NEW_ID_PREFIX, NEW_KEY, NEW_LANE, serve_variant  # noqa: E402

HISTORICAL_KEYS = ["head:canary", "head:qqq", "head:sp500", "head:topt"]


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.skip(f"no local database: {error}")
    try:
        # The served head reads the environment this database declares.
        active.execute(
            "insert into mart.environment_identity (singleton, environment, declared_at) "
            "values (true, 'staging', now()) on conflict (singleton) do nothing"
        )
        yield active
    finally:
        active.rollback()
        active.close()


def _head_rows(connection) -> list[tuple]:
    """The registry rows of the heads, with the row version: a row that the tick rewrote has a new `xmin`."""
    return connection.execute(
        "select artifact_key, lane, schedule_name, universe_like, family, served_to, wired, xmin::text "
        "from mart.served_artifact where artifact_key like 'head:%' order by artifact_key"
    ).fetchall()


def _advance_pointer(connection, universe_id: str, age: timedelta) -> None:
    """Serve one governed head of `universe_id`, `age` before now."""
    digest = uuid.uuid4().hex * 2
    run_id = f"capture-run:{digest}"
    connection.execute(
        "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, "
        "recorded_at) values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
        (run_id, digest),
    )
    pointer = uuid.uuid4().hex * 2
    connection.execute(
        "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, "
        "universe_version, factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
        "select %s, %s, environment, %s, 'v1', 'f', %s, 0, null, now() - %s from mart.environment_identity",
        (f"current-pointer:{pointer}", pointer, universe_id, run_id, age),
    )


def test_the_tick_registers_the_head_of_a_universe_that_the_record_adds(monkeypatch, connection) -> None:
    """A variant record adds one universe. The deployed tick op writes its `head:<key>` row, and
    `mart.served_head` then grades a head of that universe by the daily cadence."""
    assert NEW_KEY not in " ".join(row[0] for row in _head_rows(connection))

    with serve_variant():
        run_deployed_tick(monkeypatch, capture.run_topt_live_tick, connection=connection)

    row = connection.execute(
        "select artifact_key, lane, schedule_name, universe_like, family, served_to, wired "
        "from mart.served_artifact where artifact_key = %s",
        (f"head:{NEW_KEY}",),
    ).fetchone()
    assert row == (
        f"head:{NEW_KEY}",
        "capture",
        f"{NEW_KEY}_live_schedule",
        f"{NEW_ID_PREFIX}%",
        "daily",
        "consumers",
        True,
    )

    _advance_pointer(connection, f"{NEW_ID_PREFIX}2026-10-27", timedelta(hours=1))
    graded = connection.execute(
        "select artifact_key, family, freshness, availability, limit_hours from mart.served_head "
        "where universe_id = %s",
        (f"{NEW_ID_PREFIX}2026-10-27",),
    ).fetchone()
    assert graded == (f"head:{NEW_KEY}", "daily", "fresh", "available", 72)


def test_the_tick_writes_nothing_for_the_real_record_and_leaves_the_historical_rows(monkeypatch, connection) -> None:
    """The real record names the four universes of the seed. Running the tick twice changes no row: not the
    four historical rows (same `xmin`), and no fifth row."""
    before = _head_rows(connection)
    assert [row[0] for row in before] == HISTORICAL_KEYS

    run_deployed_tick(monkeypatch, capture.run_topt_live_tick, connection=connection)
    run_deployed_tick(monkeypatch, capture.run_topt_live_tick, connection=connection)

    assert _head_rows(connection) == before


def test_a_row_of_the_registry_that_names_another_cadence_is_not_overwritten(monkeypatch, connection) -> None:
    """The writer fills a gap and never edits. An operator's change of a row stays."""
    connection.execute("update mart.served_artifact set family = 'weekly' where artifact_key = 'head:topt'")
    run_deployed_tick(monkeypatch, capture.run_topt_live_tick, connection=connection)
    row = connection.execute("select family from mart.served_artifact where artifact_key = 'head:topt'").fetchone()
    assert row == ("weekly",)


def test_the_names_of_a_tick_come_from_the_record_accessor() -> None:
    """One definition of the job, op and schedule names (#1522): the record accessor. The tick and the
    registry row both read it."""
    assert [tick.key for tick in capture.TICKS] == list(universes.keys())
    for tick in capture.TICKS:
        assert tick.job_name == universes.job_name(tick.key)
        assert tick.op_name == universes.op_name(tick.key)
        assert tick.schedule_name == universes.schedule_name(tick.key)
    assert universes.schedule_name("canary") == "canary_daily_schedule"
    assert universes.schedule_name("qqq") == "qqq_live_schedule"
    assert NEW_LANE.startswith("universe-list:")
