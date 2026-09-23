"""Harness helpers for the suites and the repository tools that reach real infrastructure.

`skip_or_fail` is the gate for integration suites that need the live runtime: CI
provisions real Postgres + MinIO precisely so those tests RUN there — a silently-skipped
suite reads as green while covering nothing — so CI sets TRUEALPHA_REQUIRE_RUNTIME=1,
turning an unreachable runtime into a hard failure; locally (no env var) the same call
skips cleanly.

`load_tool` and `apply_migration_chain` reach repository paths (`tools/`, `db/`) rather
than installed packages, which is why they live here and not behind the runtime
boundary in `truealpha_runtime.__init__`.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REQUIRE_RUNTIME_ENV = "TRUEALPHA_REQUIRE_RUNTIME"
REPO_ROOT = Path(__file__).resolve().parents[4]
TOOLS = REPO_ROOT / "tools"
DB_DIR = REPO_ROOT / "db"


def load_tool(name: str) -> ModuleType:
    """Import a `tools/<name>.py` script as a module.

    The scripts are executables, not a package, so reaching them needs
    `spec_from_file_location`. Nine test files each carried their own six-line
    copy of that bootstrap — four of them added during the session about
    deleting duplication. One copy, here, where the other test-only helper
    already lives.
    """
    path = TOOLS / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"no tool named {name!r} in {TOOLS}")
    spec = importlib.util.spec_from_file_location(f"truealpha_tool_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses in the tool can resolve their own
    # module during class creation — omitting this raises a confusing
    # AttributeError from dataclasses._is_type.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def skip_or_fail(reason: str) -> None:
    import pytest

    if os.environ.get(REQUIRE_RUNTIME_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        pytest.fail(f"{REQUIRE_RUNTIME_ENV} is set but: {reason}", pytrace=False)
    pytest.skip(reason)


def apply_migration_chain(
    database_url: str,
    *,
    db_dir: Path = DB_DIR,
    timeout: float = 300,
    check: bool = True,
    psql_command: str | None = None,
) -> str:
    """Bring `database_url` to the declared schema, through THE applier (#984).

    `db/apply_migrations.sh` is the single implementation of "apply db/migrations/*.sql
    in glob order, then db/roles.sql, stopping on the first error". A test that needs a
    migrated database of its own calls this instead of writing the loop again. Four of
    them used to carry their own copy, and the copies had already diverged: three ran
    one `psql -f` per file, and the fourth pushed each file through psycopg and never
    applied roles.sql at all -- so a fixture whose docstring said "migrated from
    scratch" produced a schema no other environment has.

    Raises AssertionError carrying psql's own output, which is what a caller wants to
    read when a migration fails: the file, the line and the SQLSTATE. `check=False`
    returns that output instead of raising, for the one caller that replays the chain
    over a database it has deliberately broken and is asking what replay does to it.
    """
    runner = db_dir / "apply_migrations.sh"
    environment = dict(os.environ)
    # The caller named a target; an inherited admin DSN or a psql-runs-elsewhere command
    # from the surrounding shell must not redirect it somewhere else.
    environment.pop("MIGRATIONS_DATABASE_URL", None)
    environment.pop("TRUEALPHA_PSQL", None)
    environment |= {"DATABASE_URL": database_url, "TRUEALPHA_DB_DIR": str(db_dir)}
    # `psql_command` selects the applier's other transport: psql running somewhere the
    # repository path does not exist (a container), so each file arrives on stdin.
    if psql_command is not None:
        environment["TRUEALPHA_PSQL"] = psql_command
    completed = subprocess.run(
        ["sh", str(runner)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"{runner} exited {completed.returncode}:\n{(completed.stdout + completed.stderr)[-8000:]}"
        )
    return completed.stdout + completed.stderr
