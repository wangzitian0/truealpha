# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import re; from collections.abc import Iterator; import psycopg; import pytest; from psycopg import sql; from truealpha_runtime.testing import apply_migration_chain, isolated_test_database; FUNCTION = "mart.is_head_scheduled(text,text)"; OLD_DEFINITION = '\ncreate or replace function mart.is_head_scheduled(p_universe_id text, p_environment text)\nreturns boolean language sql immutable as $$ select true $$\n'
@pytest.fixture
def database() -> Iterator[str]:
    with isolated_test_database("retire_head_fn", template=None) as database: yield str(database)
def _holds_function(url: str) -> bool:
    with psycopg.connect(url) as connection: row = connection.execute("select to_regprocedure(%s) is not null", (FUNCTION,)).fetchone()
    assert row is not None; return bool(row[0])
def _hold_old_function(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection: connection.execute(OLD_DEFINITION)
    assert _holds_function(url)
def test_a_fresh_chain_never_holds_the_function(database: str) -> None: assert not _holds_function(database)
def test_a_second_boot_does_not_create_it_again(database: str) -> None: apply_migration_chain(database); apply_migration_chain(database); assert not _holds_function(database)
def test_a_database_that_holds_the_function_loses_it_after_the_chain(database: str) -> None: _hold_old_function(database); apply_migration_chain(database); assert not _holds_function(database); apply_migration_chain(database); assert not _holds_function(database)
def test_the_grant_of_the_edited_file_stays(database: str) -> None:
    with psycopg.connect(database) as connection: granted = connection.execute( "select has_table_privilege('app_ops_reader', 'mart.nightly_verdicts', 'SELECT')" ).fetchone()
    assert granted == (True,)
def test_the_boot_under_ordinary_locks_drops_the_function_and_does_not_wait( database: str, monkeypatch: pytest.MonkeyPatch ) -> None:
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_TIMEOUT", "1s"); monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_ATTEMPTS", "1"); _hold_old_function(database)
    with psycopg.connect(database) as holder: tables = [ name for (name,) in holder.execute( "select format('%%I.%%I', n.nspname, c.relname) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = any(%s) and c.relkind in ('r', 'p') order by 1", (["raw", "staging", "mart", "app", "ops", "dagster"],), ).fetchall() ]; holder.execute(sql.SQL("lock table {} in row exclusive mode").format(sql.SQL(", ").join(map(sql.SQL, tables)))); output = apply_migration_chain(database); holder.rollback()
    assert not re.search(r"LOCK TIMEOUT", output); assert not _holds_function(database)
