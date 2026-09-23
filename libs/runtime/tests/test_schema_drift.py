"""The drift check has to go red, so here it is going red (#984).

`tools/schema_drift.py` exists because nothing compared a live database against the
declared chain, and a cache that cannot tell it is stale is not a cache. A check only
ever seen green is the same thing one step later, so every test below that asserts
"clean" has a sibling that deliberately breaks the schema and asserts the check names
what broke — including the exact shape that cost 27 `UndefinedColumn` failures on a
developer machine: `staging.market_prices_daily` without `trading_date`.

Cost control, because these tests run in CI: the chain is applied ONCE, to a template
database, and every case gets a `create database ... template` copy of it. A clone is a
file copy; re-running 73 migrations per test would not be.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, load_tool

drift = load_tool("schema_drift")

_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"
RESET = Path(__file__).resolve().parents[3] / "db" / "reset_database.sh"

#: The table and column #939 reshaped, and the exact drift #984 was opened for.
DRIFTED_TABLE = "staging.market_prices_daily"
DRIFTED_COLUMN = "trading_date"


def _named(database: str) -> str:
    """The configured server, naming a different database — as a URI, because that is
    what this repository's DATABASE_URL contract is (RuntimeSettings.validate_database_url)
    and what db/reset_database.sh takes."""
    base = urlsplit(os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL))
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


def _admin_url() -> str:
    return _named("postgres")


def _create(statement: sql.Composed) -> None:
    with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin:
        admin.execute(statement)


def _drop(database: str) -> None:
    with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin:
        admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))


@pytest.fixture(scope="module")
def chain_template() -> Iterator[str]:
    """One database with the declared chain applied, used as a template by everything below."""
    name = f"truealpha_drift_template_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        _create(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        apply_migration_chain(_named(name))
        yield name
    finally:
        _drop(name)


@pytest.fixture
def clone(chain_template: str) -> Iterator[str]:
    """A throwaway copy of the migrated template. Function scope: tests break it on purpose."""
    name = f"truealpha_drift_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _create(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(chain_template)))
    try:
        yield _named(name)
    finally:
        _drop(name)


@pytest.fixture
def reference(chain_template: str) -> Iterator[str]:
    """A second copy, standing in for the database the tool builds from the chain itself."""
    name = f"truealpha_drift_ref_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _create(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(chain_template)))
    try:
        yield _named(name)
    finally:
        _drop(name)


def _execute(database_url: str, statement: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(statement)


# --- green ---------------------------------------------------------------------------


def test_a_database_that_ran_the_chain_matches_the_chain(clone: str, reference: str) -> None:
    report, counts = drift.compare(clone, reference)
    assert report == []
    # And it compared something. A clean report over an empty catalog is the failure
    # mode this whole file exists to refuse.
    assert counts["relations"] > drift.MINIMUM_REFERENCE_RELATIONS, counts
    assert counts["columns"] > 500 and counts["constraints"] > 100, counts


def test_the_cli_exits_zero_on_a_matching_database(clone: str, reference: str) -> None:
    assert drift.main(["--database-url", clone, "--reference-database-url", reference]) == 0


# --- red -----------------------------------------------------------------------------


def test_the_missing_column_that_started_this_is_named(clone: str, reference: str) -> None:
    """#939's reshaping of staging.market_prices_daily, reproduced: the developer's
    database kept the older shape because `create table if not exists` did nothing."""
    _execute(clone, f"alter table {DRIFTED_TABLE} drop column {DRIFTED_COLUMN}")
    report, _ = drift.compare(clone, reference)
    assert any(f"{DRIFTED_TABLE}.{DRIFTED_COLUMN}" in line and "MISSING" in line for line in report), report
    assert drift.main(["--database-url", clone, "--reference-database-url", reference]) == 1


def test_a_missing_table_is_named(clone: str, reference: str) -> None:
    _execute(clone, f"drop table {DRIFTED_TABLE} cascade")
    report, _ = drift.compare(clone, reference)
    assert any(line.startswith("relations: MISSING") and DRIFTED_TABLE in line for line in report), report


def test_a_relation_the_chain_never_declared_is_named(clone: str) -> None:
    """Drift in the other direction: an object left behind by a migration that was
    reverted. `create ... if not exists` will never remove it, and until now nothing
    looked."""
    _execute(clone, "create table staging.left_behind_by_a_reverted_migration (id int)")
    with drift.reference_database(clone) as reference_url:
        report, _ = drift.compare(clone, reference_url)
    assert any("left_behind_by_a_reverted_migration" in line and "EXTRA" in line for line in report), report


def test_a_changed_column_type_is_named(clone: str, reference: str) -> None:
    _execute(clone, f"alter table {DRIFTED_TABLE} alter column symbol type varchar(8)")
    report, _ = drift.compare(clone, reference)
    assert any(f"{DRIFTED_TABLE}.symbol" in line and "DIFFERS" in line for line in report), report


# --- the check cannot pass by comparing against nothing --------------------------------


def test_comparing_against_an_unmigrated_database_fails_rather_than_reporting_clean(clone: str) -> None:
    """If the reference never ran the chain, every relation in the target reads as
    "extra" and an empty target reads as a perfect match. Neither is an answer."""
    name = f"truealpha_drift_empty_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _create(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        with pytest.raises(RuntimeError, match="did not reach it"):
            drift.compare(clone, _named(name))
        assert drift.main(["--database-url", clone, "--reference-database-url", _named(name)]) == 2
    finally:
        _drop(name)


def test_the_tool_builds_its_own_reference_and_leaves_the_target_alone(clone: str) -> None:
    """The default path: no reference handed in, so it applies the chain to a database of
    its own. And the target is only read — the relation count it had going in is the one
    it has coming out."""
    before = _relation_count(clone)
    assert drift.main(["--database-url", clone]) == 0
    assert _relation_count(clone) == before


def _relation_count(database_url: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "select count(*) from pg_class as c join pg_namespace as n on n.oid = c.relnamespace "
            "where n.nspname in ('raw', 'staging', 'mart', 'app')"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_the_reset_turns_the_drift_check_from_red_to_green(clone: str, reference: str) -> None:
    """#984's acceptance, as a check that runs again rather than a walk someone did.

    The drift is the real one: `staging.market_prices_daily` in its superseded shape,
    which `create table if not exists` will never repair. Replay is asserted NOT to fix
    it, because the Makefile claimed for months that it would.
    """
    _execute(clone, f"alter table {DRIFTED_TABLE} drop column {DRIFTED_COLUMN}")
    assert drift.compare(clone, reference)[0], "the deliberate drift did not take"

    # Replay, the thing the Makefile called safe and cheap for months. It does not
    # repair this: `create table if not exists` sees the table and does nothing, so the
    # column never comes back. (Here it also exits non-zero, on the index that names the
    # dropped column — a loud failure rather than a quiet one, but still not a repair,
    # and it abandons every migration after that file.)
    apply_migration_chain(clone, check=False)
    assert drift.compare(clone, reference)[0], (
        "replaying the chain repaired a superseded relation — if that is now true, the "
        "Makefile, db/apply_migrations.sh and tools/schema_drift.py all say otherwise"
    )

    completed = subprocess.run(
        ["sh", str(RESET)],
        env=os.environ | {"DATABASE_URL": clone, "MIGRATIONS_DATABASE_URL": clone},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert drift.compare(clone, reference)[0] == []


def test_the_containerised_psql_transport_lands_the_whole_chain(chain_template: str) -> None:
    """The applier's OTHER transport, proved by its result rather than its exit code.

    The Makefile's compose branch and the VPS bootstrap run psql inside a container
    (`TRUEALPHA_PSQL`), where the repository path does not exist, so each file arrives on
    stdin. psql stops reading standard input entirely once -c or -f is given, so the
    first version of that branch — `psql -c "set ..." < file` — ran the two SETs, ignored
    every migration and exited 0. Nothing noticed, because the two databases it was tried
    against had already been migrated by the other transport.

    So this applies the chain to an EMPTY database through that branch and diffs the
    result against one the normal transport built. A transport that applies nothing
    cannot pass it.
    """
    name = f"truealpha_drift_stdin_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _create(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        apply_migration_chain(_named(name), psql_command="psql")
        report, counts = drift.compare(_named(name), _named(chain_template))
        assert report == []
        assert counts["relations"] > drift.MINIMUM_REFERENCE_RELATIONS, counts
    finally:
        _drop(name)


# --- it does not create a database on somebody else's cluster -------------------------

#: Reserved by RFC 2606, so it never resolves and no connection is ever attempted by the
#: tests below that get PAST the guard — what they assert is that the guard stopped
#: being the thing in the way.
REMOTE_URL = "postgresql://postgres:hunter2@db.example.invalid:5432/truealpha"


def test_building_a_reference_on_a_remote_server_is_refused(monkeypatch) -> None:
    """The #990 review finding. Omitting --reference-database-url means CREATE DATABASE
    and then DROP DATABASE ... WITH (FORCE) on whatever server --database-url names. The
    target being read-only does not cover that: the reference is not the target.

    Nothing connects — the refusal happens before the admin connection, which is also
    why this test needs no Postgres.
    """
    monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="not a local server") as raised:
        with drift.reference_database(REMOTE_URL):
            pass  # pragma: no cover - the guard raises before the body runs
    message = str(raised.value)
    assert "hunter2" not in message, "the refusal names the target, so it must not carry the password"
    assert "--reference-database-url" in message and drift.REMOTE_REFERENCE_ENV in message, (
        "a refusal has to say both ways forward, or the only way forward is editing the tool"
    )


def test_the_cli_refuses_the_same_way_and_says_so(monkeypatch, capsys) -> None:
    monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False)
    assert drift.main(["--database-url", REMOTE_URL]) == 2
    stderr = capsys.readouterr().err
    assert "not a local server" in stderr
    assert "--reference-database-url" in stderr and drift.REMOTE_REFERENCE_ENV in stderr
    assert "hunter2" not in stderr


def test_the_override_is_a_real_way_forward(monkeypatch) -> None:
    """The escape hatch has to open. With it set the guard is no longer what stops the
    run — the unreachable host is, which is the failure a deliberate remote check would
    get on a host that existed."""
    monkeypatch.setenv(drift.REMOTE_REFERENCE_ENV, "1")
    with pytest.raises(psycopg.Error) as raised:
        with drift.reference_database(REMOTE_URL):
            pass  # pragma: no cover - the connection raises before the body runs
    assert "not a local server" not in str(raised.value)


def test_a_supplied_reference_is_the_other_way_forward(monkeypatch, clone: str) -> None:
    """--reference-database-url creates nothing anywhere, so a remote target needs no
    override at all: the reference is local and the remote database is only read."""
    monkeypatch.delenv(drift.REMOTE_REFERENCE_ENV, raising=False)
    with pytest.raises(psycopg.Error) as raised:
        drift.compare(REMOTE_URL, clone)
    assert "not a local server" not in str(raised.value)
