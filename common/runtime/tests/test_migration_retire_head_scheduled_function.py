"""Release N+1 of #1522: the chain no longer holds `mart.is_head_scheduled`, and drops it where a database does.

The function was a SQL copy of the schedule of the universes. Release N moved every reader to the predicate that
`output_invariants` writes from the record, so no process of release N calls it. The chain replays on every boot,
so two rules follow.

* An old migration must not create the function. A later drop would then churn on every boot.
* The drop must be guarded and never wait for a lock, so a replay on a settled database is a no-op.

These tests run against a real Postgres. A database that holds the function is the shape of staging and production
before this release.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database

FUNCTION = "mart.is_head_scheduled(text,text)"
#: What release N-1 and release N created at every boot: the body is not the point, the signature is.
OLD_DEFINITION = """
create or replace function mart.is_head_scheduled(p_universe_id text, p_environment text)
returns boolean language sql immutable as $$ select true $$
"""


@pytest.fixture
def database() -> Iterator[str]:
    """A database that the migration chain built from scratch."""
    with isolated_test_database("retire_head_fn", template=None) as database:
        yield str(database)


def _holds_function(url: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute("select to_regprocedure(%s) is not null", (FUNCTION,)).fetchone()
    assert row is not None
    return bool(row[0])


def _hold_old_function(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(OLD_DEFINITION)
    assert _holds_function(url)


def test_a_fresh_chain_never_holds_the_function(database: str) -> None:
    assert not _holds_function(database)


def test_a_second_boot_does_not_create_it_again(database: str) -> None:
    """The old file used to re-create the function at each boot, so a drop-only file was undone at the next one."""
    apply_migration_chain(database)
    apply_migration_chain(database)
    assert not _holds_function(database)


def test_a_database_that_holds_the_function_loses_it_after_the_chain(database: str) -> None:
    _hold_old_function(database)
    apply_migration_chain(database)
    assert not _holds_function(database)
    # The next boot finds nothing to drop and fails on nothing.
    apply_migration_chain(database)
    assert not _holds_function(database)


def test_the_grant_of_the_edited_file_stays(database: str) -> None:
    """`20261020T0700` keeps its grant on `mart.nightly_verdicts`; only the function and its comment left."""
    with psycopg.connect(database) as connection:
        granted = connection.execute(
            "select has_table_privilege('app_ops_reader', 'mart.nightly_verdicts', 'SELECT')"
        ).fetchone()
    assert granted == (True,)


def test_the_boot_under_ordinary_locks_drops_the_function_and_does_not_wait(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot-lock regime of test_migration_boot_locks, in the state that test never sees: production boots
    with the function present, while ticks and readers hold row-exclusive locks on the tables."""
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_TIMEOUT", "1s")
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_ATTEMPTS", "1")
    _hold_old_function(database)
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
    assert not _holds_function(database)
