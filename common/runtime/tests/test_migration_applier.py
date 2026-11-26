"""There is one migration applier, and this is what fails when a second one appears (#984).

"Apply `db/migrations/*.sql` in glob order, then `db/roles.sql`, stopping on the first
error" was written seven times: three CI workflows, the llm-service image's boot command,
the Makefile, the compose initdb hook and the VPS bootstrap script — plus four more
copies inside the test suite that nobody counted. They had drifted. The CI copies bounded
no lock and set no statement timeout; one passed `-q` and the rest did not; only one read
`MIGRATIONS_DATABASE_URL`; and the entity-identity fixture pushed each file through
psycopg and never applied `db/roles.sql` at all, so a fixture whose docstring said
"migrated from scratch" produced a schema no environment has.

None of that was visible as a defect, because each copy was locally correct. It became
visible as 27 `UndefinedColumn` failures on a developer machine whose database had been
wrong for days while CI stayed green — there was no single definition of "bring a
database to the declared schema", so there was nothing for local to align to.

So the detectors below are the point of this file, not the inventory: a reintroduced
loop goes red here whatever file it lands in. What keeps them honest is that they are
fed the exact lines this change deleted — a detector nobody has seen match is not a
detector.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.gate

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER = "db/apply_migrations.sh"


# --- what the one applier guarantees ------------------------------------------------


def test_the_applier_ends_with_roles_and_stops_on_the_first_error() -> None:
    """The two properties every copy must hold."""
    body = (REPO_ROOT / APPLIER).read_text(encoding="utf-8")
    assert 'migrations/*.sql "$db_dir"/roles.sql' in body, (
        "roles.sql must be the last file of the chain: the migrations create the relations and "
        "roles.sql carries the permanent grants over them"
    )
    assert "ON_ERROR_STOP=1" in body, "a failed statement must abort the run, not be skipped"


def test_the_applier_refuses_to_report_success_over_an_empty_chain(tmp_path: Path) -> None:
    """Green-while-empty, at the one place every environment's schema comes from: a
    db_dir with no migrations in it must fail, not print a duration and exit 0."""
    (tmp_path / "migrations").mkdir()
    (tmp_path / "roles.sql").write_text("select 1;\n")
    completed = subprocess.run(
        ["sh", str(REPO_ROOT / APPLIER)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TRUEALPHA_DB_DIR": str(tmp_path),
            # Never reached: the emptiness is caught before anything connects.
            "DATABASE_URL": "postgresql://localhost:1/unreachable",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode != 0, completed.stdout
    assert "no .sql files" in completed.stdout + completed.stderr


# --- the repair path's red lines ----------------------------------------------------

RESET = "db/reset_database.sh"

#: (target, the refusal it must produce). Every one of these exits before anything
#: connects, so running them costs nothing and touches nothing.
TARGETS_THE_RESET_MUST_REFUSE = (
    ("postgresql://postgres:hunter2@db.example.invalid:5432/truealpha", "is not a local target"),
    ("postgresql://postgres@127.0.0.1:5432/postgres", "maintenance database"),
    ("postgresql://postgres@127.0.0.1:5432/two words", "not a plain identifier"),
    # #1056: on the operator's machine an SSH tunnel to the VPS reads as the loopback.
    # Production's Postgres is published on the VPS loopback at :15433 and staging's at
    # :15432 (AGENTS.md), so the port, not the host, says which server this is.
    ("postgresql://postgres@127.0.0.1:15433/truealpha", "is not a local target"),
    ("postgresql://postgres@localhost:15432/truealpha", "is not a local target"),
    ("host=127.0.0.1 dbname=truealpha", "must be a postgresql:// URI"),
    ("", "takes no default target"),
)


@pytest.mark.parametrize(("target", "refusal"), TARGETS_THE_RESET_MUST_REFUSE)
def test_the_reset_refuses_what_it_must_never_drop(target: str, refusal: str) -> None:
    """`db-reset` drops a database. A shared dev server, a staging host, the maintenance
    database itself — each has to be a refusal rather than a habit, and `--force`-style
    escape hatches have to be spelled out (TRUEALPHA_ALLOW_REMOTE_RESET=1)."""
    completed = subprocess.run(
        ["sh", str(REPO_ROOT / RESET)],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "DATABASE_URL": target},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode != 0, completed.stdout
    assert refusal in completed.stderr, completed.stderr


def test_the_reset_never_echoes_the_password_it_was_given() -> None:
    """It names the target it is about to drop, which means it prints a DSN, which means
    a credential reaches a terminal and a CI log unless the printing is deliberate."""
    completed = subprocess.run(
        ["sh", str(REPO_ROOT / RESET)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "DATABASE_URL": "postgresql://postgres:hunter2@db.example.invalid:5432/truealpha",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert "hunter2" not in completed.stdout + completed.stderr, completed.stderr
    assert "db.example.invalid" in completed.stderr, "it must still say WHICH database it refused"


# --- one definition of "is this server local" ---------------------------------------

LOCAL_TARGET = "db/local_target.sh"

#: The two commands that write to a Postgres SERVER rather than only to the database
#: they were pointed at: reset drops and recreates the target, and the drift check
#: creates a reference database beside it and force-drops it again (#990 review). They
#: must agree on which hosts are this machine, and a second copy of that list is the
#: defect class #984 exists to remove — the seven appliers differed by less.
SERVER_WRITERS = ("db/reset_database.sh", "common/runtime/src/truealpha_tools/schema_drift.py")

#: (dsn, locality). Every spelling the two guards have to get right, including the two
#: that are local without looking like it: an empty host is libpq's default Unix socket
#: (how the compose container's own psql connects) and an explicit path is a socket
#: directory.
LOCALITY_CASES = (
    ("postgresql://postgres:secret@localhost:5432/truealpha", "local"),
    ("postgresql://postgres@127.0.0.1:5432/truealpha", "local"),
    ("postgresql://user@[::1]:5432/db", "local"),
    ("postgresql:///truealpha?user=postgres", "local"),
    ("postgresql://postgres@/truealpha?host=/var/run/postgresql", "local"),
    ("postgres://u@db.example.invalid:15433/truealpha?sslmode=require", "remote"),
    # #1056: the production port is never local, whatever host spelling reaches it.
    # Production Postgres is `truealpha-postgres` on the VPS loopback :15433 and staging is
    # :15432; an SSH tunnel makes both read as the operator's own loopback.
    ("postgresql://postgres@127.0.0.1:15433/truealpha", "remote"),
    ("postgresql://postgres:secret@localhost:15433/truealpha", "remote"),
    ("postgresql://user@[::1]:15433/db", "remote"),
    ("postgresql://postgres@127.0.0.1:15432/truealpha", "remote"),
    # The same port in every other place libpq reads one. Each of these reaches the
    # production server, so each one has to be remote.
    ("postgresql://postgres@127.0.0.1/truealpha?port=15433", "remote"),
    ("postgresql://postgres@127.0.0.1/truealpha?sslmode=disable&port=15433&x=y", "remote"),
    ("postgresql://postgres@127.0.0.1:5432,127.0.0.1:15433/truealpha", "remote"),
    ("postgresql://postgres@127.0.0.1/truealpha?port=5432,15433", "remote"),
    ("postgresql://postgres@127.0.0.1:015433/truealpha", "remote"),
    ("postgresql://postgres@/truealpha?host=/var/run/postgresql&port=15433", "remote"),
    # Percent-encoded digits are decoded by libpq, so `1%35433` is 15433. A port that is
    # not plain digits cannot be proven different, so it fails closed.
    ("postgresql://postgres@127.0.0.1/truealpha?port=1%35433", "remote"),
    # The negative controls: a neighbouring port, the local compose port, and a port-shaped
    # string that is not the `port` parameter. Without these the rule could be "everything
    # on the loopback is remote" and the cases above would still pass.
    ("postgresql://postgres@127.0.0.1:15434/truealpha", "local"),
    ("postgresql://postgres@127.0.0.1:5433/truealpha", "local"),
    ("postgresql://postgres@127.0.0.1:5432/truealpha?port=5432", "local"),
    ("postgresql://postgres@127.0.0.1:5432/truealpha?application_name=port=15433", "local"),
    ("postgresql://u:p@truealpha-postgres-staging:5432/truealpha", "remote"),
    ("postgresql://u@10.0.0.7/truealpha", "remote"),
    # A hostname that resolves to the loopback today is still another machine: this has
    # to be decidable without a DNS lookup, because a drop-database guard cannot rest on
    # what a resolver happened to answer.
    ("postgresql://u@localhost.evil.example/truealpha", "remote"),
)


def _classify(dsn: str, **environment: str) -> subprocess.CompletedProcess[str]:
    # A clean environment: libpq reads PGPORT when a DSN names no port, so the operator's
    # own PGPORT must not decide what these tests expect.
    return subprocess.run(
        ["sh", str(REPO_ROOT / LOCAL_TARGET)],
        input=dsn,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **environment},
    )


@pytest.mark.parametrize(("dsn", "locality"), LOCALITY_CASES)
def test_the_shared_classifier_says_which_server_this_is(dsn: str, locality: str) -> None:
    completed = _classify(dsn)
    assert completed.returncode == 0, completed.stderr
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout))
    assert parsed["ta_locality"] == locality, completed.stdout


@pytest.mark.parametrize(
    ("dsn", "environment", "locality"),
    [
        # libpq applies PGPORT when the DSN names no port, so a tunnel can hide in the
        # environment instead of the string.
        ("postgresql://postgres@127.0.0.1/truealpha", {"PGPORT": "15433"}, "remote"),
        ("postgresql://postgres@127.0.0.1/truealpha", {"PGPORT": "0015432"}, "remote"),
        # An explicit port in the DSN wins over PGPORT, so it decides alone.
        ("postgresql://postgres@127.0.0.1:5432/truealpha", {"PGPORT": "15433"}, "local"),
        ("postgresql://postgres@127.0.0.1/truealpha", {"PGPORT": "5432"}, "local"),
        ("postgresql://postgres@127.0.0.1/truealpha", {}, "local"),
    ],
)
def test_the_shared_classifier_reads_the_port_libpq_would_use_from_pgport(
    dsn: str, environment: dict[str, str], locality: str
) -> None:
    completed = _classify(dsn, **environment)
    assert completed.returncode == 0, completed.stderr
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout))
    assert parsed["ta_locality"] == locality, (dsn, environment, completed.stdout)


def test_the_shared_classifier_redacts_the_password_it_was_given() -> None:
    """Both callers print `ta_redacted` when they refuse, so the refusal reaches a
    terminal and a CI log. What it must never carry is the credential."""
    completed = _classify("postgresql://postgres:hunter2@db.example.invalid:5432/truealpha")
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout))
    assert "hunter2" not in parsed["ta_redacted"]
    assert parsed["ta_redacted"] == "postgresql://postgres@db.example.invalid:5432/truealpha"


def test_the_shared_classifier_refuses_a_conninfo_without_quoting_it_back() -> None:
    """A key=value conninfo cannot be rewritten to name the maintenance database, so it
    is refused — and the refusal does not echo the input, because `password=` is in it."""
    completed = _classify("host=db.example.invalid dbname=truealpha password=hunter2")
    assert completed.returncode == 2
    assert "postgresql://" in completed.stderr
    assert "hunter2" not in completed.stderr + completed.stdout


def test_a_value_cannot_escape_the_quoting_its_callers_eval() -> None:
    """db/reset_database.sh runs `eval` on this output. A URI cannot legally contain a
    bare apostrophe, which is exactly why the escaping has to hold for one."""
    completed = _classify("postgresql://a'b:pw@db.example.invalid/x")
    assert completed.returncode == 0, completed.stderr
    parsed = dict(token.split("=", 1) for token in shlex.split(completed.stdout))
    assert parsed["ta_authority"] == "a'b:pw@db.example.invalid"
    assert parsed["ta_locality"] == "remote"
