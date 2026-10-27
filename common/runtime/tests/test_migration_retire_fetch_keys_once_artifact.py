"""Step 6 of #1521: the chain no longer holds the registry row `fetch-keys-once`, and deletes it where a database does.

The row named the schedule of the retired nightly check `fetch_keys_once`. The chain replays on every boot, so three
rules follow.

* The seed of `20261006T1020` must not insert the row. A later delete would then undo that insert at each boot.
* The delete must act only when the row exists, so a replay on a settled database asks for no row lock.
* The delete must remove that one row and no other row.

These tests run against a real Postgres. A database that holds the row is the shape of staging and production before
this release.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
DELETE_FILE = MIGRATIONS / "20261027T1300_datahub_retire_fetch_keys_once_artifact.sql"
SEED_FILE = MIGRATIONS / "20261006T1020_datahub_served_head_freshness.sql"
RETIRED = "fetch-keys-once"


@pytest.fixture
def database() -> Iterator[str]:
    """A database that the migration chain built from scratch."""
    with isolated_test_database("retire_fk_once", template=None) as database:
        yield str(database)


def _holds_row(url: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            "select exists (select 1 from mart.served_artifact where artifact_key = %s)", (RETIRED,)
        )
        found = row.fetchone()
    assert found is not None
    return bool(found[0])


def _hold_old_row(url: str) -> None:
    """The row as the seed of `20261006T1020` wrote it before this release."""
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "insert into mart.served_artifact (artifact_key, lane, schedule_name, family, served_to, wired) "
            "values (%s, 'quality', 'fetch_keys_once_schedule', 'daily', 'operators', false)",
            (RETIRED,),
        )
    assert _holds_row(url)


def _other_rows(url: str) -> list[str]:
    with psycopg.connect(url) as connection:
        return [
            str(name)
            for (name,) in connection.execute(
                "select artifact_key from mart.served_artifact where artifact_key <> %s order by 1", (RETIRED,)
            ).fetchall()
        ]


def test_a_fresh_chain_never_holds_the_row(database: str) -> None:
    assert not _holds_row(database)
    assert "job-run-health" in _other_rows(database), "the rows beside it stay"


def test_a_second_boot_does_not_insert_it_again(database: str) -> None:
    """The old seed inserted the row at each boot, so a delete-only file was undone at the next one."""
    apply_migration_chain(database)
    apply_migration_chain(database)
    assert not _holds_row(database)


def test_a_database_that_holds_the_row_loses_it_and_only_it(database: str) -> None:
    before = _other_rows(database)
    _hold_old_row(database)
    apply_migration_chain(database)
    assert not _holds_row(database)
    assert _other_rows(database) == before
    # The next boot finds nothing to delete and fails on nothing.
    apply_migration_chain(database)
    assert not _holds_row(database)
    assert _other_rows(database) == before


def _run_file_and_read_locks(url: str) -> set[str]:
    """Run the migration file in one transaction. Return the lock modes this session holds on the registry."""
    text = DELETE_FILE.read_text(encoding="utf-8")
    with psycopg.connect(url) as connection:
        connection.execute(text)
        modes = {
            str(mode)
            for (mode,) in connection.execute(
                "select mode from pg_locks where pid = pg_backend_pid() and locktype = 'relation' "
                "and relation = 'mart.served_artifact'::regclass"
            ).fetchall()
        }
        connection.rollback()
    return modes


def test_a_replay_on_a_settled_table_takes_no_write_lock(database: str) -> None:
    """The guard: with no row to delete the file only reads. A plain `delete` would take ROW EXCLUSIVE."""
    assert _run_file_and_read_locks(database) == {"AccessShareLock"}


def test_a_replay_that_finds_the_row_deletes_it_with_a_row_lock(database: str) -> None:
    """The control of the test above: the same helper sees a write lock when there is a row to delete."""
    _hold_old_row(database)
    modes = _run_file_and_read_locks(database)
    assert "RowExclusiveLock" in modes
    # The helper rolled back, so the row is still there until the chain runs.
    assert _holds_row(database)


def test_the_boot_under_ordinary_locks_deletes_the_row_and_does_not_wait(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot-lock regime of test_migration_boot_locks, in the state that test never sees: production boots with
    the row present, while ticks and readers hold row-exclusive locks on the tables."""
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_TIMEOUT", "1s")
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_ATTEMPTS", "1")
    _hold_old_row(database)
    with psycopg.connect(database) as holder:
        tables = [
            name
            for (name,) in holder.execute(
                "select format('%%I.%%I', n.nspname, c.relname) from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = any(%s) and c.relkind in ('r', 'p') order by 1",
                (["raw", "staging", "mart", "app", "ops", "dagster"],),
            ).fetchall()
        ]
        holder.execute(sql.SQL("lock table {} in row exclusive mode").format(sql.SQL(", ").join(map(sql.SQL, tables))))
        output = apply_migration_chain(database)  # raises when a boot would fail
        holder.rollback()
    assert not re.search(r"LOCK TIMEOUT", output)
    assert not _holds_row(database)


def test_the_seed_of_the_old_file_does_not_name_the_row() -> None:
    """The file text: the seed lists no row with this key, so no replay inserts it."""
    assert f"('{RETIRED}'" not in SEED_FILE.read_text(encoding="utf-8")
