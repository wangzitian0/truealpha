# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; import subprocess; import uuid; from collections.abc import Iterator; from pathlib import Path; from urllib.parse import urlsplit, urlunsplit; import psycopg; import pytest; from psycopg import sql; from truealpha_runtime.testing import apply_migration_chain, guard_db_write, load_tool; drift = load_tool("schema_drift"); RESET = Path(__file__).resolve().parents[3] / "db" / "reset_database.sh"; DRIFTED_TABLE = "staging.market_prices_daily"; DRIFTED_COLUMN = "trading_date"
def _named(database: str) -> str: base = urlsplit(guard_db_write()); return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))
def _admin_url() -> str: return _named("postgres")
def _create(statement: sql.Composed) -> None:
    with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin: admin.execute(statement)
def _drop(database: str) -> None:
    with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin: admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))
@pytest.fixture(scope="module")
def chain_template() -> Iterator[str]:
    name = f"truealpha_drift_template_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try: _create(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"): pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try: apply_migration_chain(_named(name)); yield name
    finally: _drop(name)
@pytest.fixture
def clone(chain_template: str) -> Iterator[str]:
    name = f"truealpha_drift_{os.getpid()}_{uuid.uuid4().hex[:8]}"; _create(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(chain_template)))
    try: yield _named(name)
    finally: _drop(name)
@pytest.fixture
def reference(chain_template: str) -> Iterator[str]:
    name = f"truealpha_drift_ref_{os.getpid()}_{uuid.uuid4().hex[:8]}"; _create(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(chain_template)))
    try: yield _named(name)
    finally: _drop(name)
def _execute(database_url: str, statement: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection: connection.execute(statement)
def test_a_database_that_ran_the_chain_matches_the_chain(clone: str, reference: str) -> None: report, counts = drift.compare(clone, reference); assert report == []; assert counts["relations"] > drift.MINIMUM_REFERENCE_RELATIONS, counts; assert counts["columns"] > 500 and counts["constraints"] > 100, counts
def test_the_cli_exits_zero_on_a_matching_database(clone: str, reference: str) -> None: assert drift.main(["--database-url", clone, "--reference-database-url", reference]) == 0
def test_the_missing_column_that_started_this_is_named(clone: str, reference: str) -> None: _execute(clone, f"alter table {DRIFTED_TABLE} drop column {DRIFTED_COLUMN}"); report, _ = drift.compare(clone, reference); assert any(f"{DRIFTED_TABLE}.{DRIFTED_COLUMN}" in line and "MISSING" in line for line in report), report; assert drift.main(["--database-url", clone, "--reference-database-url", reference]) == 1
def test_a_missing_table_is_named(clone: str, reference: str) -> None: _execute(clone, f"drop table {DRIFTED_TABLE} cascade"); report, _ = drift.compare(clone, reference); assert any(line.startswith("relations: MISSING") and DRIFTED_TABLE in line for line in report), report
def test_a_relation_the_chain_never_declared_is_named(clone: str) -> None:
    _execute(clone, "create table staging.left_behind_by_a_reverted_migration (id int)")
    with drift.reference_database(clone) as reference_url: report, _ = drift.compare(clone, reference_url)
    assert any("left_behind_by_a_reverted_migration" in line and "EXTRA" in line for line in report), report
def test_a_changed_column_type_is_named(clone: str, reference: str) -> None: _execute(clone, f"alter table {DRIFTED_TABLE} alter column symbol type varchar(8)"); report, _ = drift.compare(clone, reference); assert any(f"{DRIFTED_TABLE}.symbol" in line and "DIFFERS" in line for line in report), report
def test_comparing_against_an_unmigrated_database_fails_rather_than_reporting_clean(clone: str) -> None:
    name = f"truealpha_drift_empty_{os.getpid()}_{uuid.uuid4().hex[:8]}"; _create(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        with pytest.raises(RuntimeError, match="did not reach it"): drift.compare(clone, _named(name))
        assert drift.main(["--database-url", clone, "--reference-database-url", _named(name)]) == 2
    finally: _drop(name)
def test_the_tool_builds_its_own_reference_and_leaves_the_target_alone(clone: str) -> None: before = _relation_count(clone); assert drift.main(["--database-url", clone]) == 0; assert _relation_count(clone) == before
def _relation_count(database_url: str) -> int:
    with psycopg.connect(database_url) as connection: row = connection.execute( "select count(*) from pg_class as c join pg_namespace as n on n.oid = c.relnamespace where n.nspname in ('raw', 'staging', 'mart', 'app')" ).fetchone()
    assert row is not None; return int(row[0])
def test_the_reset_turns_the_drift_check_from_red_to_green(clone: str, reference: str) -> None: _execute(clone, f"alter table {DRIFTED_TABLE} drop column {DRIFTED_COLUMN}"); assert drift.compare(clone, reference)[0], "the deliberate drift did not take"; apply_migration_chain(clone, check=False); assert drift.compare(clone, reference)[0], ( 'replaying the chain repaired a superseded relation — if that is now true, the Makefile, db/apply_migrations.sh and tools/schema_drift.py all say otherwise' ); completed = subprocess.run( ["sh", str(RESET)], env=os.environ | {"DATABASE_URL": clone, "MIGRATIONS_DATABASE_URL": clone}, capture_output=True, text=True, check=False, timeout=600, ); assert completed.returncode == 0, completed.stdout + completed.stderr; assert drift.compare(clone, reference)[0] == []
def test_the_containerised_psql_transport_lands_the_whole_chain(chain_template: str) -> None:
    name = f"truealpha_drift_stdin_{os.getpid()}_{uuid.uuid4().hex[:8]}"; _create(sql.SQL("create database {}").format(sql.Identifier(name)))
    try: apply_migration_chain(_named(name), psql_command="psql"); report, counts = drift.compare(_named(name), _named(chain_template)); assert report == []; assert counts["relations"] > drift.MINIMUM_REFERENCE_RELATIONS, counts
    finally: _drop(name)
REMOTE_URL = "postgresql://postgres:hunter2@db.example.invalid:5432/truealpha"
def test_building_a_reference_on_a_remote_server_is_refused(monkeypatch) -> None:
    monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="not a local server") as raised:
        with drift.reference_database(REMOTE_URL):
            pass  # pragma: no cover - the guard raises before the body runs
    message = str(raised.value); assert "hunter2" not in message, "the refusal names the target, so it must not carry the password"; assert "--reference-database-url" in message and drift.REMOTE_REFERENCE_ENV in message, ( "a refusal has to say both ways forward, or the only way forward is editing the tool" )
def test_building_a_reference_beside_a_tunnelled_production_server_is_refused(monkeypatch) -> None:
    monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False); monkeypatch.delenv("PGPORT", raising=False)
    with pytest.raises(RuntimeError, match="not a local server") as raised:
        with drift.reference_database("postgresql://postgres:hunter2@127.0.0.1:15433/truealpha"):
            pass  # pragma: no cover - the guard raises before the body runs
    assert "hunter2" not in str(raised.value)
def test_the_cli_refuses_the_same_way_and_says_so(monkeypatch, capsys) -> None: monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False); assert drift.main(["--database-url", REMOTE_URL]) == 2; stderr = capsys.readouterr().err; assert "not a local server" in stderr; assert "--reference-database-url" in stderr and drift.REMOTE_REFERENCE_ENV in stderr; assert "hunter2" not in stderr
def test_the_override_is_a_real_way_forward(monkeypatch) -> None:
    monkeypatch.setenv(drift.REMOTE_REFERENCE_ENV, "1")
    with pytest.raises(psycopg.Error) as raised:
        with drift.reference_database(REMOTE_URL):
            pass  # pragma: no cover - the connection raises before the body runs
    assert "not a local server" not in str(raised.value)
def test_a_supplied_reference_is_the_other_way_forward(monkeypatch, clone: str) -> None:
    monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False)
    with pytest.raises(psycopg.Error) as raised: drift.compare(REMOTE_URL, clone)
    assert "not a local server" not in str(raised.value)
