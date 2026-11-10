"""The retire migration drops seven empty app access tables and never waits for a lock (#1240).

The seven tables are `app.access_audit_events`, `app.authorization_decisions`,
`app.authorization_decision_grants`, `app.grant_revocations`, `app.publication_policy_sets`,
`app.publication_policy_entitlements` and `app.publication_policy_set_seals`.
They held 0 rows in Production and Staging on 2026-10-12. No code reads or writes them.

The chain replays on every boot. Two rules follow.

* An old migration must not create a retired relation. A later drop would churn on every boot.
* The drop must never wait for a lock, and it must leave every table that holds a row.

These tests run against a real Postgres.
"Old shape" means a database that still holds the seven tables, their view and their six functions.
Staging and Production are in this shape.
`fixtures/retired_app_access_old_shape.sql` restores it on top of the current chain.
The fixture is a `pg_dump` of the chain of commit 344e681.

The migration warns and keeps a table that holds a row. It raises no error.
An error would stop the boot of every service, and every file after it.
`test_migration_retire_empty_planes.py` has the same rule for the first 15 retired tables.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, guard_db_write, load_tool

drift = load_tool("schema_drift")

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "migrations" / "20261012T1130_runtime_retire_app_access_tables.sql"
OLD_SHAPE = Path(__file__).resolve().parent / "fixtures" / "retired_app_access_old_shape.sql"

RETIRED_TABLES = (
    "app.access_audit_events",
    "app.authorization_decision_grants",
    "app.authorization_decisions",
    "app.grant_revocations",
    "app.publication_policy_entitlements",
    "app.publication_policy_set_seals",
    "app.publication_policy_sets",
)
RETIRED_VIEW = "app.access_audit_metadata"
RETIRED_FUNCTIONS = (
    "app.validate_access_audit_decision_tenant()",
    "app.validate_publication_policy_entitlement_insert()",
    "app.validate_publication_policy_set_seal()",
    "app.validate_authorization_decision_policy_set()",
    "app.validate_authorization_decision_grant()",
    "app.validate_authorization_decision_required_grants()",
)

#: Tables and a function that look related and are not retired. The migration must leave every one of them.
LIVE_TABLES = ("app.tenants", "app.principals", "app.entitlement_grants", "raw.capture_runs")
LIVE_FUNCTION = "app.reject_mutation()"
#: The live tables keep their append-only trigger. The retired tables shared the same trigger function.
LIVE_APPEND_ONLY_TRIGGERS = (
    "trg_tenants_append_only",
    "trg_principals_append_only",
    "trg_entitlement_grants_append_only",
)

#: Live tables that a retired table, or the retired view, reads. The migration locks them with NOWAIT.
LIVE_PARENTS = ("app.tenants", "app.entitlement_grants", "app.principals")

#: The run bound for a skip test. A skip takes about one second.
SKIP_BOUND_SECONDS = 30
#: A migration that waits for a lock waits this long. The wait outlasts the bound above on any machine.
WAIT_LOCK_TIMEOUT = "45s"

_HEX = "a" * 64
#: One valid row for each retired table. CHECK constraints still apply. Triggers and foreign keys do not.
_SEED_ROWS = {
    "app.access_audit_events": (
        "insert into app.access_audit_events (audit_event_id, decision_id, tenant_id, principal_id, event_kind, "
        f"occurred_at, recorded_at) values ('access-audit-event:{_HEX}', 'access-decision:{_HEX}', "
        "'tenant:retire-probe', 'principal:retire-probe', 'access_allowed', '2026-10-01T00:00:00Z', "
        "'2026-10-01T00:00:00Z')"
    ),
    "app.authorization_decisions": (
        "insert into app.authorization_decisions (decision_id, tenant_id, principal_id, action, resource_id, "
        f"publication_policy_id, decision, reason_code, decided_at, recorded_at) values ('access-decision:{_HEX}', "
        "'tenant:retire-probe', 'principal:retire-probe', 'read_content', 'document:retire-probe', "
        "'publication-policy-set:retire-probe', 'allow', null, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')"
    ),
    "app.authorization_decision_grants": (
        "insert into app.authorization_decision_grants (decision_id, grant_id, recorded_at) values "
        f"('access-decision:{_HEX}', 'grant:retire-probe', '2026-10-01T00:00:00Z')"
    ),
    "app.grant_revocations": (
        "insert into app.grant_revocations (revocation_id, tenant_id, grant_id, revoked_at, reason_code, "
        "recorded_at) values ('revocation:retire-probe', 'tenant:retire-probe', 'grant:retire-probe', "
        "'2026-10-01T00:00:00Z', 'delegation_revoked', '2026-10-01T00:00:00Z')"
    ),
    "app.publication_policy_sets": (
        "insert into app.publication_policy_sets (publication_policy_set_id, content_sha256, release_manifest_id, "
        f"recorded_at) values ('publication-policy-set:retire-probe', '{_HEX}', 'release-manifest:retire-probe', "
        "'2026-10-01T00:00:00Z')"
    ),
    "app.publication_policy_entitlements": (
        "insert into app.publication_policy_entitlements (publication_policy_rule_id, publication_policy_set_id, "
        "publication_class_id, entitlement_id, recorded_at) values ('rule:retire-probe', "
        "'publication-policy-set:retire-probe', 'class:retire-probe', 'entitlement:retire-probe', "
        "'2026-10-01T00:00:00Z')"
    ),
    "app.publication_policy_set_seals": (
        "insert into app.publication_policy_set_seals (publication_policy_set_id, content_sha256, sealed_at, "
        f"recorded_at) values ('publication-policy-set:retire-probe', '{_HEX}', '2026-10-01T00:00:00Z', "
        "'2026-10-01T00:00:00Z')"
    ),
}

#: The view reads four tables. A table cannot drop while the view exists, so the view and the four tables
#: stay together while one of the four holds a row.
_VIEW_TABLES = {
    "app.access_audit_events",
    "app.authorization_decisions",
    "app.authorization_decision_grants",
    "app.publication_policy_sets",
}
#: What stays after one row sits in the key table, and whether the view stays.
#: A parent of a kept table cannot drop: the foreign key blocks it. The sets table is the parent of two tables.
_AFTER_A_ROW = {
    "app.access_audit_events": (_VIEW_TABLES, True),
    "app.authorization_decisions": (_VIEW_TABLES, True),
    "app.authorization_decision_grants": (_VIEW_TABLES, True),
    "app.publication_policy_sets": (_VIEW_TABLES, True),
    "app.grant_revocations": ({"app.grant_revocations"}, False),
    "app.publication_policy_entitlements": (
        {"app.publication_policy_entitlements", "app.publication_policy_sets"},
        False,
    ),
    "app.publication_policy_set_seals": ({"app.publication_policy_set_seals", "app.publication_policy_sets"}, False),
}


def _named(database: str) -> str:
    base = urlsplit(guard_db_write())
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(_named("postgres"), connect_timeout=5, autocommit=True) as admin:
        admin.execute(statement)


def _drop(database: str) -> None:
    _admin(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))


def _scratch_name(kind: str) -> str:
    return f"truealpha_retire_access_{kind}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Fresh:
    """The current chain on an empty database, and what that run printed."""

    name: str
    output: str


@pytest.fixture(scope="module")
def fresh_chain() -> Iterator[Fresh]:
    name = _scratch_name("fresh")
    try:
        _admin(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield Fresh(name, apply_migration_chain(_named(name)))
    finally:
        _drop(name)


@pytest.fixture(scope="module")
def old_shape_template(fresh_chain: Fresh) -> Iterator[str]:
    """The current chain plus the seven retired tables: the shape a deployed database has."""
    name = _scratch_name("template")
    _admin(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(fresh_chain.name)))
    try:
        completed = subprocess.run(
            ["psql", "--no-password", _named(name), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(OLD_SHAPE)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        yield name
    finally:
        _drop(name)


def _clone(source: str) -> Iterator[str]:
    name = _scratch_name("clone")
    _admin(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(source)))
    try:
        yield _named(name)
    finally:
        _drop(name)


@pytest.fixture
def old_shape(old_shape_template: str) -> Iterator[str]:
    yield from _clone(old_shape_template)


@pytest.fixture
def migrated(fresh_chain: Fresh) -> Iterator[str]:
    yield from _clone(fresh_chain.name)


def _existing(database_url: str, relations: tuple[str, ...] | list[str]) -> set[str]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        return {name for name in relations if connection.execute("select to_regclass(%s)", (name,)).fetchone()[0]}


def _existing_functions(database_url: str, functions: tuple[str, ...]) -> set[str]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        return {name for name in functions if connection.execute("select to_regprocedure(%s)", (name,)).fetchone()[0]}


def _level_prefix(level: str) -> str:
    """The text that psql prints before a message. The applier adds the SQLSTATE: `WARNING:  01000: `."""
    return rf"{level}:\s+(?:[0-9A-Z]{{5}}:\s+)?"


def _logged(output: str, level: str, message: str) -> bool:
    """True when psql printed `message` at `level`. A line of another level does not match."""
    return re.search(_level_prefix(level) + re.escape(message), output) is not None


@dataclass(frozen=True)
class Run:
    returncode: int
    output: str
    elapsed: float


def _run_file(database_url: str, *extra: str, lock_timeout: str = "10s") -> Run:
    """The retire migration alone, as `db/apply_migrations.sh` runs a file, with a lock timeout."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment["PGOPTIONS"] = f"-c lock_timeout={lock_timeout} -c statement_timeout=60s"
    started = time.monotonic()
    completed = subprocess.run(
        ["psql", "--no-password", database_url, "-X", "-v", "ON_ERROR_STOP=1", *extra, "-f", str(MIGRATION)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return Run(completed.returncode, completed.stdout + completed.stderr, time.monotonic() - started)


def _run_file_and_count_lock_waits(database_url: str, *, lock_timeout: str = "10s") -> tuple[Run, int]:
    """Run the migration and poll pg_locks for a relation lock request that waits.

    A wait is a lock request that is not granted. It queues every later reader behind it.
    The count is the number of polls that saw one. A correct migration never waits, so it is 0.
    The count does not depend on how fast the machine is. A waiting request lasts a full lock timeout.
    """
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment["PGOPTIONS"] = f"-c lock_timeout={lock_timeout} -c statement_timeout=60s"
    started = time.monotonic()
    process = subprocess.Popen(
        ["psql", "--no-password", database_url, "-X", "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    waits = 0
    with psycopg.connect(database_url, autocommit=True) as watcher:
        # pg_locks spans every database of the server. The CI Postgres runs one test at a time.
        while process.poll() is None:
            waits += watcher.execute(
                "select count(*) from pg_locks where not granted and locktype = 'relation'"
            ).fetchone()[0]
            if time.monotonic() - started > 60:
                process.kill()
                break
            time.sleep(0.02)
    output, _ = process.communicate()
    return Run(process.returncode, output, time.monotonic() - started), waits


def _application_locks(database_url: str) -> str:
    """Run the migration in one transaction and list the relation locks it still holds."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    completed = subprocess.run(
        [
            "psql", "--no-password", database_url, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "--single-transaction", "-f", str(MIGRATION),
            "-c",
            "select coalesce(string_agg(format('%s %s', c.oid::regclass, l.mode), ', ' order by c.oid::regclass::text), '') "
            "from pg_locks as l join pg_class as c on c.oid = l.relation "
            "join pg_namespace as n on n.oid = c.relnamespace "
            "where l.pid = pg_backend_pid() and l.locktype = 'relation' "
            "and n.nspname in ('app', 'raw', 'staging', 'mart', 'ops')",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout.strip()


def _seed(database_url: str, table: str) -> None:
    """Insert one valid row into a retired table."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        # The foreign keys and triggers of the retired tables do not fire. CHECK constraints still apply.
        connection.execute("set session_replication_role = replica")
        connection.execute(_SEED_ROWS[table])


def _row_count(database_url: str, table: str) -> int:
    with psycopg.connect(database_url) as connection:
        return connection.execute(sql.SQL("select count(*) from {}").format(sql.SQL(table))).fetchone()[0]


# --- the chain never creates a retired relation -----------------------------------------


def test_a_fresh_chain_creates_none_of_the_retired_relations(fresh_chain: Fresh) -> None:
    """A create statement left in an old file would be dropped again on every boot.

    The end state alone cannot show that: the drop hides the create. So the run's own output
    must hold no retire notice either.
    """
    url = _named(fresh_chain.name)
    assert _existing(url, RETIRED_TABLES) == set()
    assert _existing(url, [RETIRED_VIEW]) == set()
    assert _existing_functions(url, RETIRED_FUNCTIONS) == set()
    assert "retired table" not in fresh_chain.output, fresh_chain.output[-3000:]
    assert "retired view" not in fresh_chain.output, fresh_chain.output[-3000:]
    assert "retired function" not in fresh_chain.output, fresh_chain.output[-3000:]
    # The chain ran. It made the tables that must stay, and the trigger function that they share.
    assert _existing(url, LIVE_TABLES) == set(LIVE_TABLES)
    assert _existing_functions(url, (LIVE_FUNCTION,)) == {LIVE_FUNCTION}


@pytest.mark.parametrize("table", RETIRED_TABLES)
def test_a_migrated_database_has_none_of_the_retired_tables(migrated: str, table: str) -> None:
    """This test is red when any later migration, or a revived old one, creates the table again."""
    assert _existing(migrated, [table]) == set(), f"{table} exists in a migrated database"


def test_the_live_tables_keep_their_append_only_triggers(migrated: str) -> None:
    with psycopg.connect(migrated, autocommit=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "select tgname from pg_trigger where not tgisinternal and tgname = any(%s)",
                (list(LIVE_APPEND_ONLY_TRIGGERS),),
            )
        }
    assert names == set(LIVE_APPEND_ONLY_TRIGGERS)


def test_a_replay_over_a_clean_database_is_silent_and_takes_no_lock(migrated: str) -> None:
    assert _application_locks(migrated) == ""
    run = _run_file(migrated)
    assert run.returncode == 0, run.output
    assert "retired" not in run.output, run.output


def test_the_first_run_locks_the_live_tables_that_a_retired_object_reads(old_shape: str) -> None:
    """The positive control of the test above, and the reason the migration locks parents.

    Dropping a table with a foreign key takes ACCESS EXCLUSIVE on the table it references.
    LOCK on the view takes ACCESS EXCLUSIVE on the tables that the view reads.
    If the probe could not see locks, the empty result above would prove nothing.
    """
    held = _application_locks(old_shape)
    for parent in LIVE_PARENTS:
        assert f"{parent} AccessExclusiveLock" in held, held


# --- the drop ----------------------------------------------------------------------------


def test_the_chain_drops_every_retired_object_from_an_old_shape_database(old_shape: str, fresh_chain: Fresh) -> None:
    assert _existing(old_shape, RETIRED_TABLES) == set(RETIRED_TABLES), "the fixture must restore the old shape"
    assert _existing(old_shape, [RETIRED_VIEW]) == {RETIRED_VIEW}
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set(RETIRED_FUNCTIONS)

    output = apply_migration_chain(old_shape)

    assert _existing(old_shape, RETIRED_TABLES) == set()
    assert _existing(old_shape, [RETIRED_VIEW]) == set()
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set()
    for table in RETIRED_TABLES:
        assert f"retired table {table} dropped" in output, output[-3000:]
    assert f"retired view {RETIRED_VIEW} dropped" in output, output[-3000:]
    for function in RETIRED_FUNCTIONS:
        assert f"retired function {function} dropped" in output, output[-3000:]
    assert "WARNING" not in output, output[-3000:]
    # A deployed database ends where a fresh one starts: no drift, no orphan trigger function.
    report, counts = drift.compare(old_shape, _named(fresh_chain.name))
    assert report == []
    assert counts["relations"] > drift.MINIMUM_REFERENCE_RELATIONS, counts


def test_a_second_replay_after_the_drop_changes_nothing(old_shape: str) -> None:
    apply_migration_chain(old_shape)
    run = _run_file(old_shape)
    assert run.returncode == 0, run.output
    assert "retired" not in run.output, run.output
    assert _existing(old_shape, RETIRED_TABLES) == set()


def test_each_table_drops_in_its_own_transaction(old_shape: str) -> None:
    """A lock lasts to the end of its transaction. One transaction for all tables would hold every lock to the end.

    An event trigger logs the transaction id of each table drop. Seven tables need seven ids.
    """
    with psycopg.connect(old_shape, autocommit=True) as admin:
        admin.execute("create table public.retire_drop_log (table_name text, xact_id text)")
        admin.execute(
            "create function public.retire_drop_logger() returns event_trigger language plpgsql as $$ begin "
            "insert into public.retire_drop_log select format('%s.%s', schema_name, object_name), "
            "pg_current_xact_id()::text from pg_event_trigger_dropped_objects() where object_type = 'table'; "
            "end $$"
        )
        admin.execute("create event trigger retire_drop_probe on sql_drop execute function public.retire_drop_logger()")

    run = _run_file(old_shape)

    assert run.returncode == 0, run.output
    with psycopg.connect(old_shape) as connection:
        logged = connection.execute("select table_name, xact_id from public.retire_drop_log").fetchall()
    assert {name for name, _ in logged} == set(RETIRED_TABLES)
    assert len({xact_id for _, xact_id in logged}) == len(RETIRED_TABLES), logged


# --- a row stops the drop ----------------------------------------------------------------


@pytest.mark.parametrize("table", RETIRED_TABLES)
def test_a_table_with_a_row_stays_and_a_warning_names_it(old_shape: str, table: str) -> None:
    """The migration refuses to drop a table that holds a row. It does not fail the boot."""
    _seed(old_shape, table)
    remaining, view_stays = _AFTER_A_ROW[table]

    output = apply_migration_chain(old_shape)  # raises when the boot would fail

    assert _logged(output, "WARNING", f"retired table {table} holds at least 1 row(s); it stays"), output[-3000:]
    assert _existing(old_shape, RETIRED_TABLES) == remaining, output[-3000:]
    assert _existing(old_shape, [RETIRED_VIEW]) == ({RETIRED_VIEW} if view_stays else set())
    if view_stays:
        assert re.search(rf"WARNING:.*retired view {re.escape(RETIRED_VIEW)} stays: .* holds at least 1 row", output)
    assert _row_count(old_shape, table) == 1
    # The next boot tries again, warns again and still does not fail.
    again = _run_file(old_shape)
    assert again.returncode == 0, again.output
    assert _logged(again.output, "WARNING", f"retired table {table} holds at least 1 row(s); it stays")
    assert _existing(old_shape, RETIRED_TABLES) == remaining
    assert _row_count(old_shape, table) == 1


def test_a_retained_audit_plane_keeps_its_functions_and_still_accepts_an_insert(old_shape: str) -> None:
    """The catalog tracks a trigger. A function stays while a kept table has a trigger that calls it.

    A row in the decisions table keeps the view and four tables. Their triggers keep four functions.
    The two functions of the dropped entitlement and seal tables go. The kept plane still works.
    """
    _seed(old_shape, "app.authorization_decisions")

    run = _run_file(old_shape)

    assert run.returncode == 0, run.output
    assert _existing(old_shape, RETIRED_TABLES) == _VIEW_TABLES
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == {
        "app.validate_access_audit_decision_tenant()",
        "app.validate_authorization_decision_policy_set()",
        "app.validate_authorization_decision_grant()",
        "app.validate_authorization_decision_required_grants()",
    }
    other = "b" * 64
    with psycopg.connect(old_shape) as connection:
        # The insert triggers run to their end. A dropped function would fail here with "does not exist".
        connection.execute(
            "insert into app.authorization_decisions (decision_id, tenant_id, principal_id, action, resource_id, "
            "publication_policy_id, decision, reason_code, decided_at, recorded_at) values "
            f"('access-decision:{other}', 'tenant:retire-probe', 'principal:retire-probe', 'read_content', "
            "'document:retire-probe', 'publication-policy-set:retire-probe', 'allow', null, "
            "'2026-10-02T00:00:00Z', '2026-10-02T00:00:00Z')"
        )
        connection.execute(
            "insert into app.access_audit_events (audit_event_id, decision_id, tenant_id, principal_id, event_kind, "
            f"occurred_at, recorded_at) values ('access-audit-event:{other}', 'access-decision:{other}', "
            "'tenant:retire-probe', 'principal:retire-probe', 'access_allowed', '2026-10-02T00:00:00Z', "
            "'2026-10-02T00:00:00Z')"
        )
        # The view still runs. Its administrator filter returns no row without a session setting.
        assert connection.execute("select count(*) from app.access_audit_metadata").fetchone() == (0,)
        connection.rollback()


# --- locks -------------------------------------------------------------------------------


def test_a_table_in_use_is_skipped_without_waiting_and_dropped_on_the_next_replay(old_shape: str) -> None:
    """A reader holds ACCESS SHARE. A lock request that waits would queue every later reader behind it."""
    with psycopg.connect(old_shape) as reader:
        reader.execute("select 1 from app.publication_policy_set_seals limit 1")  # the transaction stays open
        run, waits = _run_file_and_count_lock_waits(old_shape, lock_timeout=WAIT_LOCK_TIMEOUT)
        reader.rollback()

    assert run.returncode == 0, run.output
    assert waits == 0, f"the migration queued a lock request for {waits} polls"
    assert run.elapsed < SKIP_BOUND_SECONDS, f"{run.elapsed:.1f}s"
    # A skipped table is a WARNING, so a table that is always busy shows in the boot log.
    assert re.search(
        r"WARNING:.*retired table app\.publication_policy_set_seals is in use; the next boot tries again", run.output
    ), run.output
    assert _existing(old_shape, RETIRED_TABLES) == {"app.publication_policy_set_seals", "app.publication_policy_sets"}

    after = _run_file(old_shape)  # the next boot, with no reader
    assert after.returncode == 0, after.output

    assert _existing(old_shape, RETIRED_TABLES) == set()
    assert _existing(old_shape, [RETIRED_VIEW]) == set()
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set()


#: A reader of one live table stops the objects that need a lock on it. The rest drop at once.
_BLOCKED_BY_A_PARENT = {
    # Only grant_revocations has a foreign key to tenants.
    "app.tenants": ({"app.grant_revocations"}, False),
    # grant_revocations and authorization_decision_grants point at entitlement_grants.
    # The decisions table stays too: authorization_decision_grants keeps a foreign key into it.
    "app.entitlement_grants": (
        {"app.grant_revocations", "app.authorization_decision_grants", "app.authorization_decisions"},
        False,
    ),
    # The view reads principals. The view stays, and so do the four tables that it reads.
    "app.principals": (
        {
            "app.access_audit_events",
            "app.authorization_decision_grants",
            "app.authorization_decisions",
            "app.publication_policy_sets",
        },
        True,
    ),
}


@pytest.mark.parametrize("parent", LIVE_PARENTS)
def test_a_live_parent_in_use_stops_the_drop_without_waiting(old_shape: str, parent: str) -> None:
    """Dropping a child takes ACCESS EXCLUSIVE on its parent. Login reads app.tenants and app.principals."""
    with psycopg.connect(old_shape) as reader:
        reader.execute(sql.SQL("select 1 from {} limit 1").format(sql.SQL(parent)))
        run, waits = _run_file_and_count_lock_waits(old_shape, lock_timeout=WAIT_LOCK_TIMEOUT)
        reader.rollback()

    assert run.returncode == 0, run.output
    assert waits == 0, f"the migration queued a lock request on {parent} for {waits} polls"
    assert run.elapsed < SKIP_BOUND_SECONDS, f"{run.elapsed:.1f}s"
    # The skip is a WARNING, so a parent that is always busy shows in the boot log.
    assert re.search(r"WARNING:.*retired (table|view) .* is in use; the next boot tries again", run.output), run.output
    remaining, view_stays = _BLOCKED_BY_A_PARENT[parent]
    assert _existing(old_shape, RETIRED_TABLES) == remaining, run.output
    assert _existing(old_shape, [RETIRED_VIEW]) == ({RETIRED_VIEW} if view_stays else set()), run.output
    after = _run_file(old_shape)
    assert after.returncode == 0, after.output
    assert _existing(old_shape, RETIRED_TABLES) == set()
    assert _existing(old_shape, [RETIRED_VIEW]) == set()


# --- a live function that names a retired function keeps it ----------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "begin perform APP.VALIDATE_AUTHORIZATION_DECISION_GRANT(); return true; end;", id="upper-case call"
        ),
        pytest.param(
            "begin return true; end; -- once app.validate_authorization_decision_grant", id="mention in a comment"
        ),
    ],
)
def test_a_live_function_that_names_a_retired_function_keeps_it(old_shape: str, body: str) -> None:
    """The catalog does not track a call in a body. SQL names are case-insensitive, so the match must be too."""
    function = "app.validate_authorization_decision_grant()"
    with psycopg.connect(old_shape, autocommit=True) as admin:
        admin.execute(f"create function app.live_probe_caller() returns boolean language plpgsql as $${body}$$")

    run = _run_file(old_shape)

    assert run.returncode == 0, run.output
    assert _existing(old_shape, RETIRED_TABLES) == set(), run.output
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == {function}, run.output
    assert _logged(run.output, "NOTICE", f"retired function {function} stays: the body of another function names it")


# --- the drop never reaches a live object -------------------------------------------------


def test_a_dependent_object_stops_the_drop_and_the_boot_survives(old_shape: str) -> None:
    """A live table with a foreign key into a retired table. CASCADE would remove that key."""
    with psycopg.connect(old_shape, autocommit=True) as connection:
        connection.execute(
            "create table app.live_probe ("
            "id text primary key, decision_id text references app.authorization_decisions (decision_id))"
        )
        connection.execute(
            "create view app.live_probe_view as select publication_policy_set_id from app.publication_policy_sets"
        )

    run = _run_file(old_shape)

    assert run.returncode == 0, run.output
    assert _logged(run.output, "WARNING", "retired table app.authorization_decisions stays"), run.output
    assert _logged(run.output, "WARNING", "retired table app.publication_policy_sets stays"), run.output
    assert _existing(old_shape, RETIRED_TABLES) == {"app.authorization_decisions", "app.publication_policy_sets"}
    with psycopg.connect(old_shape) as connection:
        key = connection.execute(
            "select count(*) from pg_constraint where conrelid = 'app.live_probe'::regclass and contype = 'f'"
        ).fetchone()
        assert key == (1,)
        assert connection.execute("select to_regclass('app.live_probe_view')").fetchone()[0] is not None


def test_the_live_tables_that_look_related_survive(old_shape: str) -> None:
    with psycopg.connect(old_shape, autocommit=True) as connection:
        connection.execute("insert into app.tenants (tenant_id) values ('tenant:keep')")
    before = _existing(old_shape, LIVE_TABLES)
    assert before == set(LIVE_TABLES)

    run = _run_file(old_shape)

    assert run.returncode == 0, run.output
    assert _existing(old_shape, LIVE_TABLES) == set(LIVE_TABLES)
    with psycopg.connect(old_shape) as connection:
        assert connection.execute("select count(*) from app.tenants").fetchone() == (1,)
    assert _existing(old_shape, RETIRED_TABLES) == set()


# --- the first boot, under the locks ordinary work holds ----------------------------------


def test_the_first_boot_under_ordinary_locks_skips_the_objects_and_does_not_fail(
    old_shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot-lock regime of test_migration_boot_locks, in the one state that test never sees.

    That test replays a database where the chain made no retired table. Production boots with them.
    """
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_TIMEOUT", "1s")
    monkeypatch.setenv("TRUEALPHA_MIGRATION_LOCK_ATTEMPTS", "1")
    with psycopg.connect(old_shape) as holder:
        relations = holder.execute(
            "select format('%%I.%%I', n.nspname, c.relname), c.relkind from pg_class as c "
            "join pg_namespace as n on n.oid = c.relnamespace "
            "where n.nspname = any(%s) and c.relkind in ('r', 'p', 'v', 'm') order by 1",
            (["raw", "staging", "mart", "app", "ops", "dagster"],),
        ).fetchall()
        tables = [name for name, kind in relations if kind in ("r", "p")]
        views = [name for name, kind in relations if kind in ("v", "m")]
        holder.execute(sql.SQL("lock table {} in row exclusive mode").format(sql.SQL(", ").join(map(sql.SQL, tables))))
        for view in views:
            holder.execute(sql.SQL("select 1 from {} limit 0").format(sql.SQL(view)))
        output = apply_migration_chain(old_shape)  # raises when a boot would fail
        holder.rollback()

    assert "LOCK TIMEOUT" not in output
    # Every table was in use. Each one is skipped, and each skip is a WARNING.
    skipped = re.findall(_level_prefix("WARNING") + r"retired table (\S+) is in use; the next boot tries again", output)
    assert sorted(skipped) == sorted(RETIRED_TABLES), output[-3000:]
    assert _logged(output, "WARNING", f"retired view {RETIRED_VIEW} is in use; the next boot tries again"), output[
        -3000:
    ]
    assert _existing(old_shape, RETIRED_TABLES) == set(RETIRED_TABLES)
    assert _existing(old_shape, [RETIRED_VIEW]) == {RETIRED_VIEW}
    after = _run_file(old_shape)
    assert after.returncode == 0, after.output
    assert _existing(old_shape, RETIRED_TABLES) == set()
    assert _existing(old_shape, [RETIRED_VIEW]) == set()
