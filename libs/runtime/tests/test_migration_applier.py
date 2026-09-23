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

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER = "db/apply_migrations.sh"

#: A shell loop over the migration glob — the shape all seven call sites had. Matches
#: `for f in db/migrations/*.sql db/roles.sql; do` and the `"$db_dir"/migrations/*.sql`
#: spelling alike, whether it is in YAML, a Makefile, a Dockerfile or a shell script.
SHELL_CHAIN_LOOP = re.compile(r"for\s+\w+\s+in\s+[^\n;]*migrations/\*\.sql")

#: The Python spelling: a file that globs the chain AND runs what it finds. Both halves
#: are required, because globbing the chain to READ it is legitimate and common —
#: tools/check_factor_contract.py, test_migration_chain.py and release_identity.py all
#: do it. Executing is what makes a second applier.
PYTHON_CHAIN_GLOB = re.compile(r"\.glob\(\s*[\"']\*\.sql[\"']\s*\)")
PYTHON_CHAIN_EXECUTES = (
    re.compile(r"[\"']psql[\"']"),
    re.compile(r"execute\(\s*[\w.]+\.read_text\(\)"),
)

# Known limit, stated rather than implied: a Python applier that reaches the files some
# other way (`os.listdir`, a hardcoded list, `open(p).read()` into a cursor) is not
# matched. The shell detector is the binding one — every call site outside the test
# suite is shell — and this pair covers the copies that actually existed.

#: Every one of the seven, verbatim from the tree before #984, plus the two Python
#: copies. The detectors are fed these so they can be seen to match something.
COPIES_THAT_EXISTED = (
    ("ci-python.yml", "          for f in db/migrations/*.sql db/roles.sql; do"),
    ("ci-db.yml", "          for f in db/migrations/*.sql db/roles.sql; do"),
    ("ci-web.yml", "            for f in db/migrations/*.sql db/roles.sql; do"),
    ("db/apply_migrations.sh", 'for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do'),
    ("Makefile", "\t\tfor f in db/migrations/*.sql db/roles.sql; do \\"),
    ("db/docker-init.sh", 'for migration in "$db_dir"/migrations/*.sql "$db_dir"/roles.sql; do'),
    ("setup_vps_ingest.sh", "for f in db/migrations/*.sql db/roles.sql; do"),
)
PYTHON_COPIES_THAT_EXISTED = (
    (
        "production_topt fixtures",
        'for migration in (*sorted((ROOT / "db/migrations").glob("*.sql")), ROOT / "db/roles.sql"):\n'
        '    subprocess.run(["psql", target_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration)])',
    ),
    (
        "test_entity_identity_store.fresh_databases",
        'for migration in sorted(MIGRATIONS.glob("*.sql")):\n    fresh.execute(migration.read_text())',
    ),
)

#: Read as text and scanned. `git ls-files` is the population — an untracked file cannot
#: reach CI, an image or another machine, so it cannot become a call site.
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".gz", ".lock"}


def _tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [REPO_ROOT / name for name in listing.split("\0") if name]


def _scannable() -> list[Path]:
    # This file quotes every pattern it hunts for, so it excludes itself by exact path —
    # the same technique, and the same reason, as the reintroduction sweep in
    # test_ci_workflows.py.
    myself = Path(__file__).resolve()
    scanned = []
    for path in _tracked_files():
        if path.suffix.lower() in _SKIP_SUFFIXES or not path.is_file():
            continue
        if path.resolve() == myself:
            continue
        scanned.append(path)
    return scanned


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def test_only_one_file_applies_the_migration_chain() -> None:
    """The assertion the whole issue reduces to."""
    offenders: list[str] = []
    scanned = 0
    for path in _scannable():
        body = _text(path)
        if body is None:
            continue
        scanned += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == APPLIER:
            continue
        if SHELL_CHAIN_LOOP.search(body):
            offenders.append(f"{relative}: a shell loop over db/migrations/*.sql")
        if path.suffix == ".py" and PYTHON_CHAIN_GLOB.search(body):
            if any(pattern.search(body) for pattern in PYTHON_CHAIN_EXECUTES):
                offenders.append(f"{relative}: globs the chain and executes what it finds")
    # A scan that reads nothing reports no offenders, which is the same output as a clean
    # tree. The repository has thousands of tracked text files.
    assert scanned > 500, f"the scan only read {scanned} files — it is not looking at the tree"
    assert not offenders, (
        "the migration chain is applied somewhere other than db/apply_migrations.sh:\n  "
        + "\n  ".join(offenders)
        + f"\n\nEvery call site runs {APPLIER}; it takes MIGRATIONS_DATABASE_URL/DATABASE_URL, "
        "TRUEALPHA_PSQL (when psql runs in a container) and TRUEALPHA_DB_DIR. A second copy is "
        "how the seven diverged (#984)."
    )


def test_the_detectors_recognise_every_copy_that_existed_before() -> None:
    """A detector nobody has seen match is not a detector.

    These are the exact lines this change deleted. If a refactor makes the patterns
    stricter than the thing they hunt, this fails before the sweep above quietly stops
    catching anything.
    """
    for where, line in COPIES_THAT_EXISTED:
        assert SHELL_CHAIN_LOOP.search(line), f"the shell detector no longer matches the copy from {where}: {line!r}"
    for where, body in PYTHON_COPIES_THAT_EXISTED:
        assert PYTHON_CHAIN_GLOB.search(body) and any(p.search(body) for p in PYTHON_CHAIN_EXECUTES), (
            f"the Python detector no longer matches the copy from {where}"
        )


def test_the_detectors_leave_readers_alone() -> None:
    """The other half of a usable detector: it must not fire on the files that read the
    chain without applying it, or the sweep gets an allowlist and the allowlist gets a
    new entry every time someone is in a hurry."""
    readers = (
        "tools/check_factor_contract.py",
        "libs/runtime/tests/test_migration_chain.py",
        "libs/runtime/tests/test_migration_vocabularies.py",
        "apps/data-engine/src/data_engine/release_identity.py",
        "apps/data-engine/tests/test_release_identity.py",
        "apps/data-engine/tests/test_entity_identity_store.py",
    )
    for relative in readers:
        path = REPO_ROOT / relative
        assert path.exists(), f"{relative} is gone; this test's premise moved"
        body = path.read_text(encoding="utf-8")
        assert PYTHON_CHAIN_GLOB.search(body), f"{relative} no longer globs the chain — drop it from this list"
        assert not any(pattern.search(body) for pattern in PYTHON_CHAIN_EXECUTES), (
            f"{relative} now executes what it globs, so it is a second applier or the detector is wrong"
        )


# --- every call site resolves to it ------------------------------------------------

#: The call sites that are not CI. The eight CI steps are asserted in
#: test_ci_workflows.py, which owns every workflow-shape assertion and resolves steps by
#: name rather than by splitting a file (#583) — `MIGRATION_CHAIN_STEPS` there, plus a
#: sweep for a CI step that reaches the migration files by any other route.
#:
#: Paired with the tree sweep above — which says no file applies the chain itself —
#: "this file names the applier" is enough to say it delegates to it.
FILE_CALL_SITES = {
    "Makefile": (APPLIER, "db/reset_database.sh"),
    "db/docker-init.sh": ("apply_migrations.sh",),
    "db/reset_database.sh": ("apply_migrations.sh",),
    "apps/data-engine/scripts/setup_vps_ingest.sh": (APPLIER,),
    "apps/llm-service/Dockerfile": (APPLIER,),
    "libs/runtime/src/truealpha_runtime/testing.py": ("apply_migrations.sh",),
}


@pytest.mark.parametrize(("relative", "expected"), sorted(FILE_CALL_SITES.items()))
def test_the_other_call_sites_run_the_one_applier(relative: str, expected: tuple[str, ...]) -> None:
    body = (REPO_ROOT / relative).read_text(encoding="utf-8")
    for needle in expected:
        assert needle in body, f"{relative} no longer reaches {needle}"


# --- what the one applier guarantees ------------------------------------------------


def test_the_applier_ends_with_roles_and_stops_on_the_first_error() -> None:
    """The two properties every copy was supposed to have and not all of them did."""
    body = (REPO_ROOT / APPLIER).read_text(encoding="utf-8")
    assert SHELL_CHAIN_LOOP.search(body), "the applier no longer loops over the chain"
    assert 'migrations/*.sql "$db_dir"/roles.sql' in body, (
        "roles.sql must be the last file of the chain: the migrations create the relations and "
        "roles.sql carries the permanent grants over them (ci-db learned this as a failing "
        "governed-research-access contract)"
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
