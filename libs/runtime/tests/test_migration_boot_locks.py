"""A boot never waits on a lock (2026-09-17 production incident).

`db/apply_migrations.sh` replays every migration on every llm-service boot. The v0.0.81
production boot spent 108 s of its 132 s inside `0017_contract_objects.sql`: a
`standard_backfill_pipeline` run held ROW EXCLUSIVE on `staging.contract_objects` in a
long transaction, and 0017 re-ran `alter column ... set default` (ACCESS EXCLUSIVE),
`create index if not exists` (SHARE, taken before the name is looked up) and
`drop trigger` + `create trigger` (SHARE ROW EXCLUSIVE) whether or not anything had to
change. The healthcheck gave up, deploy_v2's in-service check failed the rollout, and
the API was down until the backfill committed.

Two properties are asserted here, against a real Postgres:

* a replay of the whole chain completes while other sessions hold the locks ordinary
  work holds — ROW EXCLUSIVE on every table (an open transaction that inserted a row, for
  the incident's table) and ACCESS SHARE on every view (a reader) — because every
  migration only takes a lock when the catalog says there is something to change, and
* the runner bounds every wait: a lock timeout is retried from the file's first
  statement a bounded number of times and then fails loudly, naming the file and the
  transactions that held the locks; a statement timeout or a SQL error is not retried.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_DIR = REPO_ROOT / "db"
RUNNER = DB_DIR / "apply_migrations.sh"
REPLAY_SEED = DB_DIR / "tests" / "contract_kind_replay_seed.sql"
_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"

#: Application schemas the migrations own; a lock anywhere in them can stall a boot.
APPLICATION_SCHEMAS = ("raw", "staging", "mart", "app", "ops", "dagster")

#: The replay under contention runs with this lock_timeout and no retry, so a single
#: statement that still needs a conflicting lock fails the test at once and names itself.
STRICT = {"TRUEALPHA_MIGRATION_LOCK_TIMEOUT": "1s", "TRUEALPHA_MIGRATION_LOCK_ATTEMPTS": "1"}


def _admin_url() -> str:
    parameters = conninfo_to_dict(os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL))
    return make_conninfo(**(parameters | {"dbname": "postgres"}))


@pytest.fixture
def empty_database() -> Iterator[str]:
    """A database of its own, so holding locks and replaying the chain touch nothing shared."""
    admin_url = _admin_url()
    name = f"truealpha_bootlocks_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    parameters = conninfo_to_dict(admin_url)
    try:
        yield make_conninfo(**(parameters | {"dbname": name}))
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s", (name,))
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))


def run_runner(
    database_url: str, db_dir: Path = DB_DIR, **knobs: str
) -> tuple[subprocess.CompletedProcess[str], float]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("PG", "TRUEALPHA_MIGRATION_"))
    }
    environment.pop("MIGRATIONS_DATABASE_URL", None)
    environment |= {"DATABASE_URL": database_url, "TRUEALPHA_DB_DIR": str(db_dir), **knobs}
    started = time.monotonic()
    completed = subprocess.run(
        ["sh", str(RUNNER)], env=environment, capture_output=True, text=True, check=False, timeout=300
    )
    return completed, time.monotonic() - started


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


@pytest.fixture
def migrated_database(empty_database: str) -> str:
    """The chain applied, rows seeded, and the chain applied again — the state every
    deployed database is in when the next boot replays it."""
    completed, _ = run_runner(empty_database)
    assert completed.returncode == 0, _output(completed)[-4000:]
    seeded = subprocess.run(
        ["psql", "--no-password", empty_database, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(REPLAY_SEED)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    completed, _ = run_runner(empty_database)
    assert completed.returncode == 0, _output(completed)[-4000:]
    return empty_database


def _insert_contract_object(connection: psycopg.Connection) -> None:
    digest = uuid.uuid4().hex * 2  # 64 hex characters
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, 'registry_snapshot', %s, '{}'::jsonb)",
        (f"registry-snapshot:{digest}", digest),
    )


def test_a_replay_does_not_wait_for_a_backfill_writing_contract_objects(migrated_database: str) -> None:
    """The incident, exactly: an open transaction that inserted into staging.contract_objects."""
    with psycopg.connect(migrated_database) as backfill:
        _insert_contract_object(backfill)  # transaction left open: ROW EXCLUSIVE is held
        completed, elapsed = run_runner(migrated_database, **STRICT)
        backfill.rollback()
    assert completed.returncode == 0, _output(completed)[-4000:]
    assert "LOCK TIMEOUT" not in _output(completed)
    # A clean replay is ~15 s on production and ~3 s here; the incident took 108 s in 0017.
    assert elapsed < 60, f"replay took {elapsed:.1f}s"


def test_a_replay_does_not_wait_for_any_writer_or_reader(migrated_database: str) -> None:
    """Every table written and every view read by an open transaction: the replay is a read."""
    with psycopg.connect(migrated_database) as holder:
        relations = holder.execute(
            """
            select format('%%I.%%I', n.nspname, c.relname), c.relkind
              from pg_class as c
              join pg_namespace as n on n.oid = c.relnamespace
             where n.nspname = any(%s) and c.relkind in ('r', 'p', 'v', 'm')
             order by 1
            """,
            (list(APPLICATION_SCHEMAS),),
        ).fetchall()
        tables = [name for name, kind in relations if kind in ("r", "p")]
        views = [name for name, kind in relations if kind in ("v", "m")]
        assert len(tables) > 50 and len(views) > 10, (len(tables), len(views))
        _insert_contract_object(holder)
        holder.execute(sql.SQL("lock table {} in row exclusive mode").format(sql.SQL(", ").join(map(sql.SQL, tables))))
        for view in views:
            holder.execute(sql.SQL("select 1 from {} limit 0").format(sql.SQL(view)))
        completed, elapsed = run_runner(migrated_database, **STRICT)
        holder.rollback()
    assert completed.returncode == 0, _output(completed)[-4000:]
    assert "LOCK TIMEOUT" not in _output(completed)
    assert elapsed < 60, f"replay took {elapsed:.1f}s"


# --- the runner's own bounds, over a two-file chain ---------------------------------------


def _chain(tmp_path: Path, *migrations: tuple[str, str]) -> Path:
    (tmp_path / "migrations").mkdir()
    for name, body in migrations:
        (tmp_path / "migrations" / name).write_text(body)
    (tmp_path / "roles.sql").write_text("select 1;\n")
    return tmp_path


def _table(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("create table public.boot_lock_probe (id int)")


NEEDS_A_STRONG_LOCK = "alter table public.boot_lock_probe add column if not exists note text;\n"


def test_a_lock_timeout_is_retried_with_backoff_and_then_fails_loudly(empty_database: str, tmp_path: Path) -> None:
    _table(empty_database)
    db_dir = _chain(
        tmp_path,
        ("0001_first.sql", "select 1;\n"),
        ("0002_needs_lock.sql", NEEDS_A_STRONG_LOCK),
        ("0003_never_reached.sql", "create table public.never_reached (id int);\n"),
    )
    with psycopg.connect(empty_database) as holder:
        holder.execute("insert into public.boot_lock_probe values (1)")
        completed, elapsed = run_runner(
            empty_database,
            db_dir,
            TRUEALPHA_MIGRATION_LOCK_TIMEOUT="1s",
            TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="3",
            TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS="1",
        )
        holder.rollback()
    output = _output(completed)
    assert completed.returncode != 0
    # Three one-second attempts and 1 s + 2 s of backoff: bounded, not a hang.
    assert 4 <= elapsed < 30, f"{elapsed:.1f}s"
    assert output.count("LOCK TIMEOUT") == 3, output
    assert "0002_needs_lock.sql at line 1, attempt 3/3" in output
    # ... and it says which transaction held which lock.
    assert "public.boot_lock_probe(RowExclusive)" in output, output
    assert "FAILED" in output and "0002_needs_lock.sql" in output
    assert "0003_never_reached.sql" not in output
    with psycopg.connect(empty_database) as check:
        assert check.execute("select to_regclass('public.never_reached')").fetchone() == (None,)


def test_a_lock_released_between_attempts_lets_the_boot_finish(empty_database: str, tmp_path: Path) -> None:
    _table(empty_database)
    db_dir = _chain(tmp_path, ("0001_needs_lock.sql", NEEDS_A_STRONG_LOCK))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PG", "TRUEALPHA_MIGRATION_")) and key != "MIGRATIONS_DATABASE_URL"
    } | {
        "DATABASE_URL": empty_database,
        "TRUEALPHA_DB_DIR": str(db_dir),
        "TRUEALPHA_MIGRATION_LOCK_TIMEOUT": "1s",
        "TRUEALPHA_MIGRATION_LOCK_ATTEMPTS": "5",
        "TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS": "1",
    }
    with psycopg.connect(empty_database) as holder:
        holder.execute("insert into public.boot_lock_probe values (1)")
        runner = subprocess.Popen(
            ["sh", str(RUNNER)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        try:
            time.sleep(1.5)  # the first attempt has timed out by now; commit before the retry
            holder.rollback()
            output, _ = runner.communicate(timeout=60)
        finally:
            if runner.poll() is None:
                runner.kill()
    assert runner.returncode == 0, output
    assert "LOCK TIMEOUT" in output and "retrying" in output
    with psycopg.connect(empty_database) as check:
        columns = check.execute(
            "select attname from pg_attribute where attrelid = 'public.boot_lock_probe'::regclass and attnum > 0"
        ).fetchall()
    assert ("note",) in columns


def test_the_lock_budget_caps_the_total_wait(empty_database: str, tmp_path: Path) -> None:
    _table(empty_database)
    db_dir = _chain(tmp_path, ("0001_needs_lock.sql", NEEDS_A_STRONG_LOCK))
    with psycopg.connect(empty_database) as holder:
        holder.execute("insert into public.boot_lock_probe values (1)")
        completed, elapsed = run_runner(
            empty_database,
            db_dir,
            TRUEALPHA_MIGRATION_LOCK_TIMEOUT="1s",
            TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="50",
            TRUEALPHA_MIGRATION_LOCK_BACKOFF_SECONDS="1",
            TRUEALPHA_MIGRATION_LOCK_BUDGET_SECONDS="5",
        )
        holder.rollback()
    assert completed.returncode != 0
    assert elapsed < 15, f"{elapsed:.1f}s"
    assert "attempt 50/50" not in _output(completed)
    assert "FAILED" in _output(completed)


def test_a_statement_timeout_is_not_retried(empty_database: str, tmp_path: Path) -> None:
    db_dir = _chain(tmp_path, ("0001_slow.sql", "select pg_sleep(5);\n"))
    completed, elapsed = run_runner(
        empty_database, db_dir, TRUEALPHA_MIGRATION_STATEMENT_TIMEOUT="1s", TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="3"
    )
    output = _output(completed)
    assert completed.returncode != 0
    assert "57014" in output and "not a lock timeout; no retry" in output
    assert "LOCK TIMEOUT" not in output
    assert elapsed < 4, f"{elapsed:.1f}s"


def test_a_sql_error_fails_the_boot_without_a_retry(empty_database: str, tmp_path: Path) -> None:
    db_dir = _chain(
        tmp_path,
        ("0001_broken.sql", "select * from public.does_not_exist;\n"),
        ("0002_after.sql", "create table public.after_broken (id int);\n"),
    )
    completed, _ = run_runner(empty_database, db_dir)
    output = _output(completed)
    assert completed.returncode != 0
    assert "not a lock timeout; no retry" in output
    assert "0002_after.sql" not in output


def test_the_session_carries_both_timeouts(empty_database: str, tmp_path: Path) -> None:
    db_dir = _chain(
        tmp_path,
        (
            "0001_show.sql",
            "select 'lock_timeout=' || current_setting('lock_timeout'), "
            "'statement_timeout=' || current_setting('statement_timeout');\n",
        ),
    )
    completed, _ = run_runner(empty_database, db_dir)
    assert completed.returncode == 0, _output(completed)
    assert "lock_timeout=5s" in completed.stdout and "statement_timeout=1min" in completed.stdout, completed.stdout


def test_non_numeric_knobs_are_refused(empty_database: str, tmp_path: Path) -> None:
    db_dir = _chain(tmp_path, ("0001_noop.sql", "select 1;\n"))
    completed, _ = run_runner(empty_database, db_dir, TRUEALPHA_MIGRATION_LOCK_ATTEMPTS="three")
    assert completed.returncode != 0
    assert "whole numbers" in completed.stderr
