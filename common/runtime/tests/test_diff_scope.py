"""Local checks run only what the diff reaches (#1412 T1).

The default local command was whole-repo work: `make test` ran every pytest file in
one process, `make smoke` ran mypy over every package and every bun test, and the
scoped path was opt-in. These tests pin the new default from two sides:

- the selector, on throwaway repositories whose import graph is known: a change to
  module M selects exactly the tests that import M, directly or through other modules;
- the real entry points (`make test`, `make typecheck`, `make smoke`, `make check`,
  `tools/prepush.sh`), run in a throwaway worktree of this repository with one changed
  module: each selects that module's tests, and only `FULL=1` runs the whole suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from truealpha_tools import diff_scope
from truealpha_tools.diff_scope import select

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(cwd: Path, *args: str) -> str:
    empty = cwd.parent / "gitconfig"
    empty.touch()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout


FIXTURE: dict[str, str] = {
    "pyproject.toml": """
[tool.uv.workspace]
members = ["pkg/alpha", "pkg/beta", "common/runtime"]

[tool.pytest.ini_options]
testpaths = ["pkg/alpha/tests", "pkg/beta/tests", "common/runtime/tests"]

[tool.mypy]
files = ["pkg/alpha/src", "pkg/beta/src", "common/runtime/src"]
""",
    "pkg/alpha/src/alpha/__init__.py": "from alpha.m import VALUE\n",
    "pkg/alpha/src/alpha/m.py": "VALUE = 1\n",
    "pkg/alpha/src/alpha/n.py": "from alpha.m import VALUE\n\nDOUBLE = VALUE * 2\n",
    "pkg/alpha/src/alpha/o.py": '# Reads "run.sh" in a comment only, which is not a use.\nOTHER = 3\n',
    "pkg/alpha/src/alpha/loader.py": 'from pathlib import Path\n\nCONTRACT = Path(__file__).parent / "limits.json"\n',
    "pkg/alpha/src/alpha/limits.json": "{}\n",
    "pkg/alpha/tests/conftest.py": "",
    "pkg/alpha/tests/test_m.py": "import alpha.m\n\ndef test_m():\n    assert alpha.m.VALUE == 1\n",
    "pkg/alpha/tests/test_n.py": "from alpha import n\n\ndef test_n():\n    assert n.DOUBLE == 2\n",
    "pkg/alpha/tests/test_o.py": "import alpha.o\n\ndef test_o():\n    assert alpha.o.OTHER == 3\n",
    "pkg/alpha/tests/test_package.py": "from alpha import VALUE\n\ndef test_package():\n    assert VALUE == 1\n",
    "pkg/alpha/tests/test_loader.py": "from alpha.loader import CONTRACT\n\ndef test_loader():\n    assert CONTRACT\n",
    "pkg/alpha/tests/support/seed.py": "ROWS = 2\n",
    "pkg/alpha/tests/test_seeded.py": (
        "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent / 'support'))\n"
        "from seed import ROWS  # noqa: E402\n\ndef test_seeded():\n    assert ROWS == 2\n"
    ),
    "pkg/alpha/data/table.yaml": "a: 1\n",
    "pkg/beta/src/beta/__init__.py": "",
    "pkg/beta/src/beta/uses_n.py": "from alpha.n import DOUBLE\n\nTRIPLE = DOUBLE + 1\n",
    "pkg/beta/tests/test_beta.py": (
        "import beta.uses_n\n\nSCRIPT = 'tools/run.sh'\n\ndef test_beta():\n    assert beta.uses_n.TRIPLE == 3\n"
    ),
    "common/runtime/src/truealpha_tools/__init__.py": "",
    "common/runtime/src/truealpha_tools/probe.py": "ANSWER = 42\n",
    "common/runtime/tests/test_probe.py": (
        "from truealpha_runtime.testing import load_tool\n\nprobe = load_tool('probe')\n\n"
        "def test_probe():\n    assert probe.ANSWER == 42\n"
    ),
    "common/runtime/tests/test_ci_workflows.py": "def test_workflows():\n    pass\n",
    "common/runtime/tests/test_migration_chain.py": "def test_chain():\n    pass\n",
    "common/runtime/tests/test_reads_migrations.py": (
        "from pathlib import Path\n\nMIGRATIONS = Path('db') / 'migrations'\n\ndef test_reads():\n    pass\n"
    ),
    "tools/run.sh": "#!/usr/bin/env bash\necho run\n",
    "db/migrations/0001_init.sql": "create table t (id int);\n",
    f"{diff_scope.WORKFLOW_DIR}ci.yml": "name: ci\n",
    "docs/guide.md": "# guide\n",
    "apps/app-web/package.json": "{}\n",
    "apps/app-web/src/lib/a.ts": "export const a = 1;\n",
    "apps/app-web/src/lib/b.ts": 'import { a } from "./a";\nexport const b = a + 1;\n',
    "apps/app-web/src/lib/c.ts": "export const c = 3;\n",
    "apps/app-web/tests/b.test.ts": 'import { b } from "../src/lib/b";\nif (b !== 2) throw new Error("b");\n',
    "apps/app-web/tests/c.test.ts": 'import { c } from "@/lib/c";\nif (c !== 3) throw new Error("c");\n',
}

A_TESTS = {
    "pkg/alpha/tests/test_m.py",
    "pkg/alpha/tests/test_n.py",
    "pkg/alpha/tests/test_o.py",
    "pkg/alpha/tests/test_package.py",
    "pkg/alpha/tests/test_loader.py",
    "pkg/alpha/tests/test_seeded.py",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative, text in FIXTURE.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "tag", "base")
    return root


def _edit(root: Path, relative: str, text: str = "\n# changed\n") -> None:
    with (root / relative).open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_a_change_to_module_m_selects_exactly_the_tests_that_import_m(repo: Path) -> None:
    _edit(repo, "pkg/alpha/src/alpha/m.py")
    selection = select(repo, "base")
    assert set(selection.pytest) == {
        "pkg/alpha/tests/test_m.py",  # imports alpha.m
        "pkg/alpha/tests/test_n.py",  # alpha.n imports alpha.m
        "pkg/alpha/tests/test_package.py",  # alpha/__init__ re-exports VALUE from alpha.m
        "pkg/beta/tests/test_beta.py",  # beta.uses_n -> alpha.n -> alpha.m, across packages
    }
    assert set(selection.mypy) == {
        "pkg/alpha/src/alpha/m.py",
        "pkg/alpha/src/alpha/n.py",
        "pkg/alpha/src/alpha/__init__.py",
        "pkg/beta/src/beta/uses_n.py",
    }
    assert selection.lint == ["pkg/alpha/src/alpha/m.py"]
    assert selection.bun == [] and selection.tsc is False and selection.fallbacks == []


def test_a_sibling_change_does_not_select_every_importer_of_the_package(repo: Path) -> None:
    """`import alpha.o` runs `alpha/__init__.py`, which imports alpha.m. A change to alpha.m
    must still not select test_o: test_o reads nothing that alpha.m defines."""
    _edit(repo, "pkg/alpha/src/alpha/m.py")
    assert "pkg/alpha/tests/test_o.py" not in select(repo, "base").pytest


def test_a_change_to_a_package_init_selects_every_importer_below_it(repo: Path) -> None:
    _edit(repo, "pkg/alpha/src/alpha/__init__.py")
    selection = set(select(repo, "base").pytest)
    assert {"pkg/alpha/tests/test_o.py", "pkg/alpha/tests/test_m.py", "pkg/beta/tests/test_beta.py"} <= selection


def test_an_unrelated_module_selects_only_its_own_test(repo: Path) -> None:
    _edit(repo, "pkg/alpha/src/alpha/o.py")
    assert select(repo, "base").pytest == ["pkg/alpha/tests/test_o.py"]


def test_a_deleted_module_selects_the_tests_that_still_import_it(repo: Path) -> None:
    (repo / "pkg/alpha/src/alpha/o.py").unlink()
    selection = select(repo, "base")
    assert selection.pytest == ["pkg/alpha/tests/test_o.py"]
    assert selection.lint == []


def test_a_helper_on_sys_path_reaches_the_test_that_imports_it_by_bare_name(repo: Path) -> None:
    _edit(repo, "pkg/alpha/tests/support/seed.py")
    assert select(repo, "base").pytest == ["pkg/alpha/tests/test_seeded.py"]


def test_load_tool_reaches_the_tool_module(repo: Path) -> None:
    _edit(repo, "common/runtime/src/truealpha_tools/probe.py")
    assert select(repo, "base").pytest == ["common/runtime/tests/test_probe.py"]


def test_a_changed_conftest_selects_every_test_under_it(repo: Path) -> None:
    _edit(repo, "pkg/alpha/tests/conftest.py")
    assert set(select(repo, "base").pytest) == A_TESTS


def test_a_named_file_reaches_the_code_that_names_it_and_a_comment_does_not(repo: Path) -> None:
    _edit(repo, "tools/run.sh", "echo again\n")
    # test_beta names "tools/run.sh" in a string. alpha/o.py names "run.sh" only in a comment.
    assert select(repo, "base").pytest == ["pkg/beta/tests/test_beta.py"]
    _git(repo, "checkout", "-q", "--", "tools/run.sh")
    (repo / "pkg/alpha/src/alpha/limits.json").write_text('{"a": 1}\n', encoding="utf-8")
    # alpha/loader.py loads limits.json, so the tests that import alpha.loader are selected.
    assert select(repo, "base").pytest == ["pkg/alpha/tests/test_loader.py"]


def test_workflow_migration_and_document_rules(repo: Path) -> None:
    workflow = f"{diff_scope.WORKFLOW_DIR}ci.yml"
    _edit(repo, workflow)
    assert select(repo, "base").pytest == ["common/runtime/tests/test_ci_workflows.py"]
    _git(repo, "checkout", "-q", "--", workflow)
    _edit(repo, "db/migrations/0001_init.sql", "-- changed\n")
    assert select(repo, "base").pytest == [
        "common/runtime/tests/test_migration_chain.py",
        "common/runtime/tests/test_reads_migrations.py",  # names the db/migrations directory
    ]
    _git(repo, "checkout", "-q", "--", "db/migrations/0001_init.sql")
    _edit(repo, "docs/guide.md")
    selection = select(repo, "base")
    assert selection.pytest == [] and selection.bun == [] and selection.fallbacks == []


def test_an_unmapped_file_inside_a_package_falls_back_to_the_package_and_says_so(repo: Path) -> None:
    _edit(repo, "pkg/alpha/data/table.yaml", "b: 2\n")
    selection = select(repo, "base")
    assert set(selection.pytest) == A_TESTS
    assert selection.fallbacks == [
        {"path": "pkg/alpha/data/table.yaml", "scope": "pkg/alpha", "reason": "no rule maps this file"}
    ]


def test_the_lockfile_selects_every_python_test(repo: Path) -> None:
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    selection = select(repo, "base")
    assert selection.pytest == selection.pytest_all and len(selection.pytest_all) == 11


def test_a_web_change_selects_the_bun_tests_that_import_it(repo: Path) -> None:
    _edit(repo, "apps/app-web/src/lib/a.ts", "export const z = 0;\n")
    selection = select(repo, "base")
    assert selection.bun == ["apps/app-web/tests/b.test.ts"]  # b.test -> lib/b -> lib/a
    assert selection.tsc is True and selection.pytest == []
    _git(repo, "checkout", "-q", "--", "apps/app-web/src/lib/a.ts")
    _edit(repo, "apps/app-web/src/lib/c.ts", "export const y = 0;\n")
    assert select(repo, "base").bun == ["apps/app-web/tests/c.test.ts"]  # through the @/ alias
    _git(repo, "checkout", "-q", "--", "apps/app-web/src/lib/c.ts")
    (repo / "apps/app-web/package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    assert select(repo, "base").bun == ["apps/app-web/tests/b.test.ts", "apps/app-web/tests/c.test.ts"]


def test_each_run_records_the_selected_and_the_skipped_tests(repo: Path) -> None:
    _edit(repo, "pkg/alpha/src/alpha/o.py")
    exit_code = diff_scope.run(repo, ["test"], full=False, base="base", dry_run=True, entry="unit test")
    assert exit_code == 0
    record_file = repo / ".git" / "truealpha-diff-scope" / "runs.jsonl"
    records = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["schema"] == "diff-scope-run.v1" and record["entry"] == "unit test"
    assert record["pytest"]["selected"] == ["pkg/alpha/tests/test_o.py"]
    assert len(record["pytest"]["skipped"]) == 10 and "pkg/alpha/tests/test_m.py" in record["pytest"]["skipped"]
    assert record["changed"] == ["pkg/alpha/src/alpha/o.py"]
    assert record["head"] == _git(repo, "rev-parse", "HEAD").strip()
    assert {"wall_seconds", "child_cpu_seconds", "exit"} <= record.keys()


def test_full_runs_the_whole_repository_and_the_default_does_not(repo: Path) -> None:
    _edit(repo, "pkg/alpha/src/alpha/o.py")
    selection = select(repo, "base")
    scoped = diff_scope.plan(selection, ["lint", "typecheck", "test"])
    full = diff_scope.plan(None, ["lint", "typecheck", "test"], web_tests_all=selection.bun_all)
    assert [c.argv for c in scoped] == [
        ("uv", "run", "ruff", "check", "pkg/alpha/src/alpha/o.py"),
        ("uv", "run", "ruff", "format", "--check", "pkg/alpha/src/alpha/o.py"),
        ("uv", "run", "mypy", "pkg/alpha/src/alpha/o.py"),
        ("uv", "run", "pytest", "pkg/alpha/tests/test_o.py"),
    ]
    assert ("uv", "run", "pytest") in [c.argv for c in full]
    assert ("uv", "run", "mypy") in [c.argv for c in full]
    assert ("uv", "run", "ruff", "check", "apps", "common", "tools") in [c.argv for c in full]
    assert sum(c.argv[:2] == ("bun", "run") and c.argv[2].endswith(".test.ts") for c in full) == 2


# --------------------------------------------------------------------------- real entry points


def _bash4() -> str:
    for candidate in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        probe = subprocess.run(
            [candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout.strip()) >= 4:
            return candidate
    pytest.skip("no bash >= 4 available; tools/prepush.sh requires one")


@pytest.fixture(scope="module")
def worktree(tmp_path_factory: pytest.TempPathFactory) -> object:
    """A throwaway worktree of this repository at HEAD with one changed module."""
    if os.environ.get("TRUEALPHA_PREPUSH"):
        pytest.skip("already inside a prepush run; a nested prepush run recurses")
    if shutil.which("make") is None:
        pytest.skip("make is not installed")
    tree = tmp_path_factory.mktemp("diff-scope") / "tree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), "HEAD"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    with (tree / "common/runtime/src/truealpha_tools/walk_evidence.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# diff-scope entry point probe\n")
    yield tree
    subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO_ROOT, capture_output=True, check=False)


ENTRY_POINTS = ("test", "typecheck", "smoke", "check")

#: What an outer `make ... FULL=1` hands to every process below it. CI runs pytest under
#: `make test-python FULL=1` (#1412 L1): a nested `make` reads FULL=1 back from MAKEFLAGS,
#: and the diff scope reads FULL and BASE from the environment.
INHERITED = ("FULL", "BASE", "MAKEFLAGS", "MAKEOVERRIDES", "MFLAGS", "MAKELEVEL", "DIFF_SCOPE_DRY_RUN")


def _entry_env(**values: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED}
    return {**env, "DIFF_SCOPE_DRY_RUN": "1", **values}


@pytest.mark.parametrize("target", ENTRY_POINTS)
def test_every_make_entry_point_runs_only_what_the_diff_reaches(worktree: Path, target: str) -> None:
    # Precondition first: on a Makefile without the diff scope, `make test` would start the
    # whole suite for real. `make -n` prints the recipe without running it.
    recipe = subprocess.run(["make", "-n", target], cwd=worktree, capture_output=True, text=True, check=True).stdout
    assert "tools/diff_scope.py" in recipe, f"`make {target}` does not go through the diff scope:\n{recipe}"
    env = _entry_env(BASE="HEAD")
    result = subprocess.run(["make", "-s", target], cwd=worktree, capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    planned = [line for line in result.stdout.splitlines() if line.startswith("== [")]
    if target in ("test", "smoke", "check"):
        pytest_lines = [line for line in planned if "uv run pytest" in line]
        assert len(pytest_lines) == 1, planned
        assert "common/runtime/tests/test_walk_evidence.py" in pytest_lines[0]
        assert not pytest_lines[0].rstrip().endswith("uv run pytest"), "the default ran the whole suite"
    if target in ("typecheck", "smoke", "check"):
        mypy_lines = [line for line in planned if "uv run mypy" in line]
        assert len(mypy_lines) == 1 and "truealpha_tools/walk_evidence.py" in mypy_lines[0], planned
        assert not mypy_lines[0].rstrip().endswith("uv run mypy"), "the default type-checked every package"
    assert not any("bun run" in line for line in planned), "a Python change started bun work"


def test_full_1_runs_the_whole_suite(worktree: Path) -> None:
    env = _entry_env()
    result = subprocess.run(
        ["make", "-s", "check", "FULL=1"], cwd=worktree, capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    planned = [line.split("  (in")[0].rstrip() for line in result.stdout.splitlines() if line.startswith("== [")]
    assert "== [test] uv run pytest" in planned
    assert "== [typecheck] uv run mypy" in planned
    assert "== [lint] uv run ruff check apps common tools" in planned


def test_prepush_runs_only_what_the_diff_reaches(worktree: Path) -> None:
    script = (worktree / "tools/prepush.sh").read_text(encoding="utf-8")
    assert "tools/diff_scope.py" in script, "prepush does not go through the diff scope; running it would start suites"
    env = _entry_env()
    result = subprocess.run(
        [_bash4(), "tools/prepush.sh", "HEAD"], cwd=worktree, capture_output=True, text=True, env=env, check=False
    )
    planned = [line for line in result.stdout.splitlines() if line.startswith("== [")]
    pytest_lines = [line for line in planned if "uv run pytest" in line]
    assert len(pytest_lines) == 1 and "common/runtime/tests/test_walk_evidence.py" in pytest_lines[0], result.stdout
    assert "uv run pytest common/runtime/tests " not in pytest_lines[0] + " ", "prepush ran the whole package"


def test_mypy_on_a_subset_resolves_sibling_packages_from_source(repo: Path) -> None:
    """L3 (#1412) found `make typecheck` reporting `factors.*` as untyped: given a subset of
    files, mypy resolved the other first-party packages from site-packages. The plan names
    every source root on MYPYPATH, and a real mypy run on the fixture proves it."""
    # beta.uses_n imports alpha.n, and no file of the alpha package is in the selection.
    _edit(repo, "pkg/beta/src/beta/uses_n.py")
    selection = select(repo, "base")
    [command] = diff_scope.plan(selection, ["typecheck"])
    assert command.argv[3:] == ("pkg/beta/src/beta/uses_n.py",)
    roots = ["pkg/alpha/src", "pkg/beta/src", "common/runtime/src"]
    assert dict(command.env) == {"MYPYPATH": os.pathsep.join(roots)}
    mypy = [sys.executable, "-m", "mypy", "--no-incremental", *command.argv[3:]]
    without = subprocess.run(mypy, cwd=repo, capture_output=True, text=True, check=False)
    assert without.returncode != 0 and "alpha" in without.stdout, without.stdout
    env = {**os.environ, **dict(command.env)}
    with_path = subprocess.run(mypy, cwd=repo, capture_output=True, text=True, check=False, env=env)
    assert with_path.returncode == 0, with_path.stdout


def test_a_web_step_without_node_modules_names_the_missing_install(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(repo, "apps/app-web/src/lib/c.ts", "export const y = 0;\n")
    exit_code = diff_scope.run(repo, ["test"], full=False, base="base", dry_run=False, entry="t")
    assert exit_code == 2
    assert "apps/app-web/node_modules is missing in this worktree: run `make install`" in capsys.readouterr().err


def test_the_entry_points_ignore_an_outer_make_full_run(worktree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI runs these tests under `make test-python FULL=1` (#1412 L1). The outer FULL=1 must
    not turn the scoped run under test into a whole-repository run."""
    monkeypatch.setenv("FULL", "1")
    monkeypatch.setenv("MAKEFLAGS", " -- FULL=1")
    monkeypatch.setenv("MAKEOVERRIDES", "FULL=1")
    result = subprocess.run(
        ["make", "-s", "test"], cwd=worktree, capture_output=True, text=True, env=_entry_env(BASE="HEAD"), check=False
    )
    planned = [line for line in result.stdout.splitlines() if line.startswith("== [")]
    assert "diff-scope: FULL=1" not in result.stdout, result.stdout
    assert any("test_walk_evidence.py" in line for line in planned), planned


def test_a_dry_run_prints_every_selected_file(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The entry-point tests read the plan from a dry run. #1431 made one change reach more
    than ten test files, the printed command was cut after ten, and main went red."""
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert diff_scope.run(repo, ["test"], full=False, base="base", dry_run=True, entry="t") == 0
    [line] = [x for x in capsys.readouterr().out.splitlines() if "uv run pytest" in x]
    selected = select(repo, "base").pytest
    assert len(selected) > 10 and all(f" {path}" in line for path in selected), line
