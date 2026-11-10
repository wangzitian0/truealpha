# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; import subprocess; import time; import uuid; from collections.abc import Iterator; from pathlib import Path; import psycopg; import pytest; from psycopg import sql; from psycopg.conninfo import conninfo_to_dict, make_conninfo; from truealpha_runtime.testing import guard_db_write; REPO_ROOT = Path(__file__).resolve().parents[3]; DB_DIR = REPO_ROOT / "db"; RUNNER = DB_DIR / "apply_migrations.sh"; REPLAY_SEED = DB_DIR / "tests" / "contract_kind_replay_seed.sql"; APPLICATION_SCHEMAS = ("raw", "staging", "mart", "app", "ops", "dagster"); STRICT = {"TRUEALPHA_MIGRATION_LOCK_TIMEOUT": "1s", "TRUEALPHA_MIGRATION_LOCK_ATTEMPTS": "1"}
def _admin_url() -> str: parameters = conninfo_to_dict(guard_db_write()); return make_conninfo(**(parameters | {"dbname": "postgres"}))
@pytest.fixture
def empty_database() -> Iterator[str]:
    admin_url = _admin_url(); name = f"truealpha_bootlocks_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin: admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"): pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    parameters = conninfo_to_dict(admin_url)
    try: yield make_conninfo(**(parameters | {"dbname": name}))
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin: admin.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s", (name,)); admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))
def run_runner( database_url: str, db_dir: Path = DB_DIR, **knobs: str ) -> tuple[subprocess.CompletedProcess[str], float]: environment = { key: value for key, value in os.environ.items() if not key.startswith(("PG", "TRUEALPHA_MIGRATION_", "MIGRATION_")) }; environment.pop("MIGRATIONS_DATABASE_URL", None); environment |= {"DATABASE_URL": database_url, "TRUEALPHA_DB_DIR": str(db_dir), **knobs}; started = time.monotonic(); completed = subprocess.run( ["sh", str(RUNNER)], env=environment, capture_output=True, text=True, check=False, timeout=300 ); return completed, time.monotonic() - started
def _output(completed: subprocess.CompletedProcess[str]) -> str: return completed.stdout + completed.stderr
@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    admin_url = _admin_url(); name = f"truealpha_bootlocks_migrated_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin: admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"): pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    parameters = conninfo_to_dict(admin_url); db_url = make_conninfo(**(parameters | {"dbname": name}))
    try: completed, _ = run_runner(db_url); assert completed.returncode == 0, _output(completed)[-4000:]; seeded = subprocess.run( ["psql", "--no-password", db_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(REPLAY_SEED)], capture_output=True, text=True, check=False, ); assert seeded.returncode == 0, seeded.stdout + seeded.stderr; completed, _ = run_runner(db_url); assert completed.returncode == 0, _output(completed)[-4000:]; yield db_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin: admin.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s", (name,)); admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))
def _insert_contract_object(connection: psycopg.Connection) -> None: digest = uuid.uuid4().hex * 2; connection.execute( "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) values (%s, 'registry_snapshot', %s, '{}'::jsonb)", (f"registry-snapshot:{digest}", digest), )
def test_a_replay_does_not_wait_for_a_backfill_writing_contract_objects(migrated_database: str) -> None:
    with psycopg.connect(migrated_database) as backfill: _insert_contract_object(backfill); completed, elapsed = run_runner(migrated_database, **STRICT); backfill.rollback()
    assert completed.returncode == 0, _output(completed)[-4000:]; assert "LOCK TIMEOUT" not in _output(completed); assert elapsed < 60, f"replay took {elapsed:.1f}s"
def test_a_replay_does_not_wait_for_any_writer_or_reader(migrated_database: str) -> None:
    with psycopg.connect(migrated_database) as holder:
        relations = holder.execute( "\n            select format('%%I.%%I', n.nspname, c.relname), c.relkind\n              from pg_class as c\n              join pg_namespace as n on n.oid = c.relnamespace\n             where n.nspname = any(%s) and c.relkind in ('r', 'p', 'v', 'm')\n             order by 1\n            ", (list(APPLICATION_SCHEMAS),), ).fetchall(); tables = [name for name, kind in relations if kind in ("r", "p")]; views = [name for name, kind in relations if kind in ("v", "m")]; assert len(tables) > 50 and len(views) > 10, (len(tables), len(views)); _insert_contract_object(holder); holder.execute(sql.SQL("lock table {} in row exclusive mode").format(sql.SQL(", ").join(map(sql.SQL, tables))))
        for view in views: holder.execute(sql.SQL("select 1 from {} limit 0").format(sql.SQL(view)))
        completed, elapsed = run_runner(migrated_database, **STRICT); holder.rollback()
    assert completed.returncode == 0, _output(completed)[-4000:]; assert "LOCK TIMEOUT" not in _output(completed); assert elapsed < 60, f"replay took {elapsed:.1f}s"
CHAIN_SEED_TABLES = frozenset( { "mart.environment_identity", "mart.freshness_limit", "mart.served_artifact", "staging.entity_alias_schemes", "staging.entity_kinds", "staging.entity_relation_types", "staging.issuer_cik_predecessors", } ); TICK_DECLARATION = '\ninsert into mart.environment_identity (singleton, environment, declared_at)\nvalues (true, %s, clock_timestamp())\non conflict (singleton) do update set\n    environment = excluded.environment,\n    declared_at = excluded.declared_at\n'
def _write_every_row_again(holder: psycopg.Connection) -> dict[str, int]:
    tables = holder.execute( "\n        select format('%%I.%%I', n.nspname, c.relname),\n               (select quote_ident(a.attname)\n                  from pg_attribute as a\n                 where a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped\n                   and a.attgenerated = '' and a.attidentity = ''\n                 order by a.attnum\n                 limit 1)\n          from pg_class as c\n          join pg_namespace as n on n.oid = c.relnamespace\n         where n.nspname = any(%s) and c.relkind in ('r', 'p') and not c.relispartition\n         order by 1\n        ", (list(APPLICATION_SCHEMAS),), ).fetchall(); holder.execute("set local session_replication_role = replica"); written: dict[str, int] = {}
    for table, column in tables:
        assert column is not None, f"{table} has no column that an update can name"; rows = holder.execute( sql.SQL("update {table} set {column} = {column}").format(table=sql.SQL(table), column=sql.SQL(column)) ).rowcount
        if rows > 0: written[table] = rows
    return written
def test_a_replay_does_not_wait_for_an_open_transaction_that_wrote_every_row(migrated_database: str) -> None:
    with psycopg.connect(migrated_database) as holder: written = _write_every_row_again(holder); assert CHAIN_SEED_TABLES <= written.keys(), sorted(CHAIN_SEED_TABLES - written.keys()); completed, elapsed = run_runner(migrated_database, **STRICT); holder.rollback()
    assert completed.returncode == 0, _output(completed)[-4000:]; assert "LOCK TIMEOUT" not in _output(completed); assert elapsed < 60, f"replay took {elapsed:.1f}s"
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_a_replay_does_not_wait_for_a_tick_that_declared_its_environment( migrated_database: str, environment: str ) -> None:
    with psycopg.connect(migrated_database) as tick: tick.execute(TICK_DECLARATION, (environment,)); completed, elapsed = run_runner(migrated_database, **STRICT); tick.rollback()
    assert completed.returncode == 0, _output(completed)[-4000:]; assert "LOCK TIMEOUT" not in _output(completed); assert elapsed < 60, f"replay took {elapsed:.1f}s"
def _chain(tmp_path: Path, *migrations: tuple[str, str]) -> Path:
    (tmp_path / "migrations").mkdir()
    for name, body in migrations: (tmp_path / "migrations" / name).write_text(body)
    (tmp_path / "roles.sql").write_text("select 1;\n"); return tmp_path
def _table(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection: connection.execute("create table public.boot_lock_probe (id int)")
NEEDS_A_STRONG_LOCK = "alter table public.boot_lock_probe add column if not exists note text;\n"
def test_a_lock_timeout_is_retried_with_backoff_and_then_fails_loudly(empty_database: str, tmp_path: Path) -> None:
    _table(empty_database); db_dir = _chain( tmp_path, ("0001_first.sql", "select 1;\n"), ("0002_needs_lock.sql", NEEDS_A_STRONG_LOCK), ("0003_never_reached.sql", "create table public.never_reached (id int);\n"), )
    with psycopg.connect(empty_database) as holder: holder.execute("insert into public.boot_lock_probe values (1)"); completed, elapsed = run_runner( empty_database, db_dir, TRUEALPHA_MIGRATION_LOCK_TIMEOUT="1s", TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="3", TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS="1", ); holder.rollback()
    output = _output(completed); assert completed.returncode != 0; assert 4 <= elapsed < 30, f"{elapsed:.1f}s"; assert output.count("LOCK TIMEOUT") == 3, output; assert "0002_needs_lock.sql at line 1, attempt 3/3" in output; assert "public.boot_lock_probe(RowExclusive)" in output, output; assert "FAILED" in output and "0002_needs_lock.sql" in output; assert "0003_never_reached.sql" not in output
    with psycopg.connect(empty_database) as check: assert check.execute("select to_regclass('public.never_reached')").fetchone() == (None,)
def test_a_lock_released_between_attempts_lets_the_boot_finish(empty_database: str, tmp_path: Path) -> None:
    _table(empty_database); db_dir = _chain(tmp_path, ("0001_needs_lock.sql", NEEDS_A_STRONG_LOCK)); environment = { key: value for key, value in os.environ.items() if not key.startswith(("PG", "TRUEALPHA_MIGRATION_")) and key != "MIGRATIONS_DATABASE_URL" } | { "DATABASE_URL": empty_database, "TRUEALPHA_DB_DIR": str(db_dir), "TRUEALPHA_MIGRATION_LOCK_TIMEOUT": "1s", "TRUEALPHA_MIGRATION_LOCK_ATTEMPTS": "5", "TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS": "1", }
    with psycopg.connect(empty_database) as holder:
        holder.execute("insert into public.boot_lock_probe values (1)"); runner = subprocess.Popen( ["sh", str(RUNNER)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
        try: time.sleep(1.5); holder.rollback(); output, _ = runner.communicate(timeout=60)
        finally:
            if runner.poll() is None: runner.kill()
    assert runner.returncode == 0, output; assert "LOCK TIMEOUT" in output and "retrying" in output
    with psycopg.connect(empty_database) as check: columns = check.execute( "select attname from pg_attribute where attrelid = 'public.boot_lock_probe'::regclass and attnum > 0" ).fetchall()
    assert ("note",) in columns
def test_the_lock_budget_caps_the_total_wait(empty_database: str, tmp_path: Path) -> None:
    _table(empty_database); db_dir = _chain(tmp_path, ("0001_needs_lock.sql", NEEDS_A_STRONG_LOCK))
    with psycopg.connect(empty_database) as holder: holder.execute("insert into public.boot_lock_probe values (1)"); completed, elapsed = run_runner( empty_database, db_dir, TRUEALPHA_MIGRATION_LOCK_TIMEOUT="1s", TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="50", TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS="1", TRUEALPHA_MIGRATION_LOCK_BUDGET_SECONDS="5", ); holder.rollback()
    assert completed.returncode != 0; assert elapsed < 15, f"{elapsed:.1f}s"; assert "attempt 50/50" not in _output(completed); assert "FAILED" in _output(completed)
def test_a_statement_timeout_is_not_retried(empty_database: str, tmp_path: Path) -> None: db_dir = _chain(tmp_path, ("0001_slow.sql", "select pg_sleep(5);\n")); completed, elapsed = run_runner( empty_database, db_dir, TRUEALPHA_MIGRATION_STATEMENT_TIMEOUT="1s", TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="3" ); output = _output(completed); assert completed.returncode != 0; assert "57014" in output and "not a lock timeout; no retry" in output; assert "LOCK TIMEOUT" not in output; assert elapsed < 4, f"{elapsed:.1f}s"
def test_a_sql_error_fails_the_boot_without_a_retry(empty_database: str, tmp_path: Path) -> None: db_dir = _chain( tmp_path, ("0001_broken.sql", "select * from public.does_not_exist;\n"), ("0002_after.sql", "create table public.after_broken (id int);\n"), ); completed, _ = run_runner(empty_database, db_dir); output = _output(completed); assert completed.returncode != 0; assert "not a lock timeout; no retry" in output; assert "0002_after.sql" not in output
def test_the_session_carries_both_timeouts(empty_database: str, tmp_path: Path) -> None: db_dir = _chain( tmp_path, ( "0001_show.sql", "select 'lock_timeout=' || current_setting('lock_timeout'), 'statement_timeout=' || current_setting('statement_timeout');\n", ), ); completed, _ = run_runner(empty_database, db_dir); assert completed.returncode == 0, _output(completed); assert "lock_timeout=5s" in completed.stdout and "statement_timeout=1min" in completed.stdout, completed.stdout
def test_non_numeric_knobs_are_refused(empty_database: str, tmp_path: Path) -> None: db_dir = _chain(tmp_path, ("0001_noop.sql", "select 1;\n")); completed, _ = run_runner(empty_database, db_dir, TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="three"); assert completed.returncode != 0; assert "whole numbers" in completed.stderr; completed, _ = run_runner(empty_database, db_dir, TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="0"); assert completed.returncode != 0; assert "at least 1" in completed.stderr
