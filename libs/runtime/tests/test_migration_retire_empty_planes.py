"""The retire migration drops 15 empty tables and never hurts a boot (#1061).

The chain replays on every boot. Two rules follow.

* An old migration must not create a retired table. A later drop would then churn on every boot.
* The drop must never wait for a lock and must never stop a boot.

These tests run against a real Postgres. "Old shape" means a database that holds the 15
retired tables, their indexes, triggers, policies and trigger functions, as Staging and
Production do. `fixtures/retired_planes_old_shape.sql` restores that shape on top of the
current chain. It is a `pg_dump` of the chain of commit 4769e93.

Every test below has a mutation that turns it red. The mutations are listed in the PR.
"""

from __future__ import annotations

import os
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
from truealpha_runtime.testing import apply_migration_chain, load_tool

drift = load_tool("schema_drift")

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "migrations" / "20261006T1325_datahub_retire_empty_planes.sql"
OLD_SHAPE = Path(__file__).resolve().parent / "fixtures" / "retired_planes_old_shape.sql"
_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"

RETIRED_TABLES = (
    "app.private_research_objects",
    "app.publication_policies",
    "app.tenant_memberships",
    "raw.capture_checkpoints",
    "raw.recapture_plans",
    "staging.filing_documents",
    "staging.headcount_extraction_invocations",
    "staging.headcount_facts",
    "staging.mvp_corporate_actions",
    "staging.mvp_financial_facts",
    "staging.mvp_issuer_security_links",
    "staging.mvp_market_prices",
    "staging.mvp_security_listing_links",
    "staging.mvp_universe_memberships",
    "staging.normalized_records",
)

RETIRED_FUNCTIONS = (
    "staging.validate_normalized_raw_lineage()",
    "staging.validate_filing_document_projection()",
    "staging.validate_normalized_restatement()",
    "staging.validate_headcount_invocation()",
    "staging.validate_headcount_projection()",
    "staging.validate_mvp_projection()",
    "raw.validate_capture_checkpoint_address()",
    "raw.validate_recapture_plan_address()",
    "raw.enforce_capture_checkpoint_progress()",
    "raw.validate_checkpoint_obligation_refs()",
    "raw.validate_recapture_obligation_refs()",
    "raw.has_canonical_obligation_ids(text[], boolean)",
    "raw.has_canonical_text_json_array(jsonb, boolean)",
)

#: Tables that look dead and are not. The migration must leave every one of them.
LIVE_TABLES = (
    "app.tenants",
    "app.principals",
    "app.access_audit_events",
    "app.authorization_decision_grants",
    "app.authorization_decisions",
    "app.publication_policy_sets",
    "app.entitlement_grants",
    "app.grant_revocations",
    "app.publication_policy_entitlements",
    "app.publication_policy_set_seals",
    "staging.financial_facts",
    "staging.market_prices",
    "staging.analyst_rating_events",
    "raw.capture_runs",
)

#: Live tables that a retired table references. Dropping the retired table locks them.
LIVE_PARENTS = ("app.tenants", "app.principals", "raw.capture_runs")


def _named(database: str) -> str:
    base = urlsplit(os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL))
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(_named("postgres"), connect_timeout=5, autocommit=True) as admin:
        admin.execute(statement)


def _drop(database: str) -> None:
    _admin(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))


def _scratch_name(kind: str) -> str:
    return f"truealpha_retire_{kind}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


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
    """The current chain plus the 15 retired tables: the shape a deployed database has."""
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


def _existing(database_url: str, relations: tuple[str, ...]) -> set[str]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        return {name for name in relations if connection.execute("select to_regclass(%s)", (name,)).fetchone()[0]}


def _existing_functions(database_url: str, functions: tuple[str, ...]) -> set[str]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        return {name for name in functions if connection.execute("select to_regprocedure(%s)", (name,)).fetchone()[0]}


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


def _seed_tenant_membership(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("insert into app.tenants (tenant_id) values ('tenant:retire-probe')")
        connection.execute(
            "insert into app.principals (principal_id, tenant_id, principal_kind) "
            "values ('principal:retire-probe', 'tenant:retire-probe', 'member')"
        )
        connection.execute(
            "insert into app.tenant_memberships "
            "(membership_event_id, tenant_id, principal_id, membership_state, effective_at, recorded_at) "
            "values ('membership:retire-probe', 'tenant:retire-probe', 'principal:retire-probe', 'granted', "
            "'2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')"
        )


# --- the chain never creates a retired relation -----------------------------------------


def test_a_fresh_chain_creates_none_of_the_retired_relations(fresh_chain: Fresh) -> None:
    """A create statement left in an old file would be dropped again on every boot.

    The end state alone cannot show that: the drop hides the create. So the run's own output
    must hold no retire notice either.
    """
    url = _named(fresh_chain.name)
    assert _existing(url, RETIRED_TABLES) == set()
    assert _existing_functions(url, RETIRED_FUNCTIONS) == set()
    assert "retired table" not in fresh_chain.output, fresh_chain.output[-3000:]
    assert "retired function" not in fresh_chain.output, fresh_chain.output[-3000:]
    # The chain ran, and it made the tables that must stay.
    assert _existing(url, LIVE_TABLES) == set(LIVE_TABLES)


def test_a_replay_over_a_clean_database_is_silent_and_takes_no_lock(migrated: str) -> None:
    assert _application_locks(migrated) == ""
    run = _run_file(migrated)
    assert run.returncode == 0, run.output
    assert "retired" not in run.output, run.output


def test_the_first_run_locks_the_live_tables_that_a_retired_table_references(old_shape: str) -> None:
    """The positive control of the test above, and the reason the migration locks parents.

    Dropping a table with a foreign key takes ACCESS EXCLUSIVE on the table it references.
    If the probe could not see locks, the empty result above would prove nothing.
    """
    held = _application_locks(old_shape)
    for parent in LIVE_PARENTS:
        assert f"{parent} AccessExclusiveLock" in held, held


# --- the drop ----------------------------------------------------------------------------


def test_the_chain_drops_every_retired_relation_from_an_old_shape_database(old_shape: str, fresh_chain: Fresh) -> None:
    assert _existing(old_shape, RETIRED_TABLES) == set(RETIRED_TABLES), "the fixture must restore the old shape"
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set(RETIRED_FUNCTIONS)

    output = apply_migration_chain(old_shape)

    assert _existing(old_shape, RETIRED_TABLES) == set()
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set()
    for table in RETIRED_TABLES:
        assert f"retired table {table} dropped" in output, output[-3000:]
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


def test_a_table_with_a_row_stays_and_a_warning_names_it(old_shape: str) -> None:
    _seed_tenant_membership(old_shape)

    output = apply_migration_chain(old_shape)  # raises when the boot would fail

    assert "retired table app.tenant_memberships holds at least 1 row(s); it stays" in output, output[-3000:]
    assert _existing(old_shape, RETIRED_TABLES) == {"app.tenant_memberships"}
    with psycopg.connect(old_shape) as connection:
        assert connection.execute("select count(*) from app.tenant_memberships").fetchone() == (1,)
    # The next boot tries again, warns again and still does not fail.
    again = _run_file(old_shape)
    assert again.returncode == 0, again.output
    assert "app.tenant_memberships holds at least 1 row(s)" in again.output
    assert _existing(old_shape, RETIRED_TABLES) == {"app.tenant_memberships"}


# --- locks -------------------------------------------------------------------------------


def test_a_table_in_use_is_skipped_without_waiting_and_dropped_on_the_next_replay(old_shape: str) -> None:
    """A reader holds ACCESS SHARE. A lock request that waits would queue every later reader behind it."""
    with psycopg.connect(old_shape) as reader:
        reader.execute("select 1 from staging.mvp_market_prices limit 1")  # the transaction stays open
        run = _run_file(old_shape, lock_timeout="10s")
        reader.rollback()

    assert run.returncode == 0, run.output
    assert run.elapsed < 5, f"the migration waited for a lock: {run.elapsed:.1f}s"
    assert "retired table staging.mvp_market_prices is in use; the next boot tries again" in run.output
    # normalized_records stays while a child table stays. The migration names it and goes on.
    assert "retired table staging.normalized_records stays" in run.output
    assert "SQLSTATE 2BP01" in run.output
    assert _existing(old_shape, RETIRED_TABLES) == {"staging.mvp_market_prices", "staging.normalized_records"}
    # A trigger keeps its function. The three functions of those two tables stay.
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == {
        "staging.validate_mvp_projection()",
        "staging.validate_normalized_raw_lineage()",
        "staging.validate_normalized_restatement()",
    }

    after = _run_file(old_shape)  # the next boot, with no reader
    assert after.returncode == 0, after.output

    assert _existing(old_shape, RETIRED_TABLES) == set()
    assert _existing_functions(old_shape, RETIRED_FUNCTIONS) == set()


@pytest.mark.parametrize("parent", LIVE_PARENTS)
def test_a_live_parent_in_use_stops_the_drop_without_waiting(old_shape: str, parent: str) -> None:
    """Dropping a child takes ACCESS EXCLUSIVE on its parent. Login reads app.tenants and app.principals."""
    with psycopg.connect(old_shape) as reader:
        reader.execute(sql.SQL("select 1 from {} limit 1").format(sql.SQL(parent)))
        run = _run_file(old_shape, lock_timeout="10s")
        reader.rollback()

    assert run.returncode == 0, run.output
    assert run.elapsed < 5, f"the migration waited for a lock on {parent}: {run.elapsed:.1f}s"
    children = {
        "app.tenants": {"app.tenant_memberships", "app.private_research_objects"},
        "app.principals": {"app.tenant_memberships", "app.private_research_objects"},
        "raw.capture_runs": {"raw.capture_checkpoints"},
    }[parent]
    assert _existing(old_shape, RETIRED_TABLES) == children
    after = _run_file(old_shape)
    assert after.returncode == 0, after.output
    assert _existing(old_shape, RETIRED_TABLES) == set()


# --- the drop never reaches a live object -------------------------------------------------


def test_a_dependent_object_stops_the_drop_and_the_boot_survives(old_shape: str) -> None:
    """A live table with a foreign key into a retired table. CASCADE would remove that key."""
    with psycopg.connect(old_shape, autocommit=True) as connection:
        connection.execute(
            "create table staging.live_probe ("
            "id text primary key, record_id text references staging.normalized_records (normalized_record_id))"
        )
        connection.execute(
            "create view staging.live_probe_view as select normalized_record_id from staging.filing_documents"
        )

    run = _run_file(old_shape)

    assert run.returncode == 0, run.output
    assert "retired table staging.normalized_records stays" in run.output
    assert "retired table staging.filing_documents stays" in run.output
    assert _existing(old_shape, RETIRED_TABLES) == {"staging.normalized_records", "staging.filing_documents"}
    with psycopg.connect(old_shape) as connection:
        key = connection.execute(
            "select count(*) from pg_constraint where conrelid = 'staging.live_probe'::regclass and contype = 'f'"
        ).fetchone()
        assert key == (1,)
        assert connection.execute("select to_regclass('staging.live_probe_view')").fetchone()[0] is not None


def test_the_live_tables_that_look_dead_survive(old_shape: str) -> None:
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


def test_the_first_boot_under_ordinary_locks_skips_the_tables_and_does_not_fail(
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
        started = time.monotonic()
        output = apply_migration_chain(old_shape)  # raises when a boot would fail
        elapsed = time.monotonic() - started
        holder.rollback()

    assert "LOCK TIMEOUT" not in output
    assert elapsed < 60, f"replay took {elapsed:.1f}s"
    assert _existing(old_shape, RETIRED_TABLES) == set(RETIRED_TABLES)  # every table was in use: all skipped
    after = _run_file(old_shape)
    assert after.returncode == 0, after.output
    assert _existing(old_shape, RETIRED_TABLES) == set()
