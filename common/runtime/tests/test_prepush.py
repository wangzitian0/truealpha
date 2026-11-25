"""The pre-push tier must not quietly check nothing — A4 A1 (#673).

`tools/prepush.sh` exists to attack round count: every actionable review
finding across the last three sessions fell into one of the six categories it
prints, and two were caught by reading that list against a diff before pushing.

A checker that silently examines an empty set is worse than none — it converts
"I did not look" into "it passed". Three properties keep that from happening,
all of them learned the hard way:

- an empty change set is an ERROR, not a pass: ~6 scripted edits in the 08-28
  sprint matched nothing after a formatter pass and looked exactly like a
  clean run (the same lesson `tools/redprove.sh` bakes in as exit 3);
- untracked files are examined: a brand-new tool is invisible to every
  `git diff` until it is added, and dogfooding caught the script skipping
  ITSELF for that reason;
- the script checks the changed files only, never the whole repository: the
  Local pre cell is 30 s (#1803).
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PREPUSH = REPO_ROOT / "tools" / "prepush.sh"


def bash4() -> str:
    """A bash >= 4, or a skip. `bash` on PATH is 3.2 on a stock macOS, where
    the script correctly refuses to run — so a test that hard-codes `bash`
    would exercise the version gate instead of the property it names (review)."""
    for candidate in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        probe = subprocess.run(
            [candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout.strip()) >= 4:
            return candidate
    pytest.skip("no bash >= 4 available; tools/prepush.sh requires one and says so")


def run_prepush(*arguments: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash4(), str(PREPUSH), *arguments],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


def test_an_empty_change_set_exits_cleanly_with_skip_notice(tmp_path: Path) -> None:
    """When zero files changed vs BASE, prepush prints a clear notice and exits 0 cleanly."""
    tree = tmp_path / "clean"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        result = run_prepush("HEAD", cwd=tree)
        assert result.returncode == 0, f"prepush failed on empty change set: {result.stdout + result.stderr}"
        assert "nothing changed" in result.stdout + result.stderr
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tree)], capture_output=True, cwd=REPO_ROOT, check=False
        )


def test_untracked_files_are_examined(tmp_path: Path) -> None:
    """A new file is invisible to `git diff` until added — and a new file is
    exactly what most needs a syntax check. Run in a throwaway worktree so a
    dirty caller tree does not trigger whole-suite diff-scope tests."""
    if os.environ.get("TRUEALPHA_PREPUSH"):
        pytest.skip("already inside a prepush run — running it again here recurses without bound")
    tree = tmp_path / "untracked"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        probe = tree / "tools" / "prepush_probe_delete_me.sh"
        probe.write_text("#!/usr/bin/env bash\nif [ 1 = 1 ]; then\n", encoding="utf-8")  # unterminated `if`
        result = run_prepush("HEAD", cwd=tree)
        combined = result.stdout + result.stderr
        assert "prepush_probe_delete_me.sh" in combined, (
            "an untracked file was not examined — `git ls-files --others` is gone and every "
            "brand-new tool now skips its own checks"
        )
        assert "FAIL" in combined, "the untracked file's broken syntax was examined but not reported"
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tree)], capture_output=True, cwd=REPO_ROOT, check=False
        )


def test_it_refuses_a_bash_too_old_to_run_it() -> None:
    """macOS ships bash 3.2 as /bin/bash; mapfile and `declare -A` are 4+. A
    cryptic failure here means the check stops being run at all."""
    script = PREPUSH.read_text(encoding="utf-8")
    assert 'BASH_VERSINFO[0]:-0}" -lt 4' in script, "the bash-version gate is gone"
    assert "brew install bash" in script, "the version failure no longer says how to fix it"


def test_it_names_its_own_preconditions_instead_of_failing_cryptically() -> None:
    """Review on #702, three of the four findings — each one turns a valid run
    into a confusing failure, which is how a pre-push check stops being used:

    - outside a git work tree, an unchecked `cd "$(git rev-parse ...)"` under
      `set +e` cds nowhere and then diffs whatever is there;
    - the YAML check must use uv's interpreter, since PyYAML is a workspace
      dependency and a bare `python3` may not have it — reporting FAIL on a
      perfectly valid workflow;
    - `make check` must be .PHONY and run the script, or the one pre-PR command checks nothing.
    """
    script = PREPUSH.read_text(encoding="utf-8")
    assert "not inside a git working tree" in script, "the work-tree precondition is unchecked again"
    assert 'run "yaml parses: $path" uv run python' in script, (
        "the YAML check is back on the system interpreter, which may lack PyYAML and will report FAIL on a valid file"
    )
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    phony = next(line for line in makefile.splitlines() if line.startswith(".PHONY"))
    assert " check" in phony.split(":", 1)[1], "make check is not .PHONY — a file of that name would silence it"
    assert "check:\n\t@tools/prepush.sh $(BASE)\n" in makefile, "make check does not run tools/prepush.sh"


def test_the_local_entries_never_run_the_whole_repository() -> None:
    """#1803: an empty FILES ran about 3,150 tests, and the script ran ruff and mypy on
    every package. The whole-repository runs are CI's."""
    script = PREPUSH.read_text(encoding="utf-8")
    assert "uv run mypy" not in script, "prepush.sh runs whole-repository mypy again"
    assert "ruff check apps common tools" not in script, "prepush.sh runs whole-repository ruff again"
    # `FILES=` on the command line: CI runs this test inside `make test-python FILES=<shard>`,
    # and make exports that value to a child make.
    # Its own process group: if the refusal is gone, the kill must also reach the pytest
    # that make started.
    refused = subprocess.Popen(
        ["make", "test-python", "FILES="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        start_new_session=True,
    )
    try:
        _, stderr = refused.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(refused.pid, signal.SIGKILL)
        pytest.fail("make test-python with no FILES ran instead of refusing")
    assert refused.returncode != 0, "make test-python with no FILES ran instead of refusing"
    assert "set FILES=" in stderr
