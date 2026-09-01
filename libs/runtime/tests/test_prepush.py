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
- the heavy suites are named and refused, never run — the laptop tier is
  <= 60 s by design (A4 budget table), and a pre-push check that boots
  data-engine will be abandoned within a day.
"""

from __future__ import annotations

import os
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


def run_prepush(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash4(), str(PREPUSH), *arguments],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_an_empty_change_set_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """The no-op-edit shape: nothing to check must never read as 'clean'.

    Run in a throwaway worktree at HEAD rather than gating on the caller's tree
    being clean. A skip-if-dirty version passed vacuously under
    tools/redprove.sh — whose whole method is editing a file, which makes the
    tree dirty — so the guard reported INERT while being perfectly fine. The
    empty path exits before any `uv run`, so a bare worktree is enough.
    """
    tree = tmp_path / "clean"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        result = subprocess.run(["bash", str(PREPUSH), "HEAD"], capture_output=True, text=True, cwd=tree, check=False)
        assert result.returncode != 0, "prepush reported success over an empty change set"
        assert "nothing changed" in result.stdout + result.stderr
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tree)], capture_output=True, cwd=REPO_ROOT, check=False
        )


def test_untracked_files_are_examined() -> None:
    """A new file is invisible to `git diff` until added — and a new file is
    exactly what most needs a syntax check."""
    if os.environ.get("TRUEALPHA_PREPUSH"):
        pytest.skip("already inside a prepush run — running it again here recurses without bound")
    probe = REPO_ROOT / "tools" / "prepush_probe_delete_me.sh"
    probe.write_text("#!/usr/bin/env bash\nif [ 1 = 1 ]; then\n", encoding="utf-8")  # unterminated `if`
    try:
        result = run_prepush("HEAD")
        combined = result.stdout + result.stderr
        assert "prepush_probe_delete_me.sh" in combined, (
            "an untracked file was not examined — `git ls-files --others` is gone and every "
            "brand-new tool now skips its own checks"
        )
        assert "FAIL" in combined, "the untracked file's broken syntax was examined but not reported"
    finally:
        probe.unlink(missing_ok=True)
    assert not probe.exists()


def test_the_heavy_suites_are_named_and_refused() -> None:
    """A laptop tier that boots data-engine (168 s sharded across three CI
    lanes) stops being run, and then nothing runs before a push."""
    script = PREPUSH.read_text(encoding="utf-8")
    heavy_branch = "apps/data-engine/*|apps/app-web/*"
    assert heavy_branch in script, "the heavy-suite branch is gone — prepush may now run them on the laptop"
    refusal = script.split(heavy_branch, 1)[1].split(";;", 1)[0]
    assert "SKIPPED+=" in refusal and "continue" in refusal, (
        "data-engine/app-web changes no longer route to the skip list, so the laptop tier will "
        "try to run a CI-tier suite"
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
    - `make prepush` must be .PHONY, or a file named `prepush` silences it.
    """
    script = PREPUSH.read_text(encoding="utf-8")
    assert "not inside a git working tree" in script, "the work-tree precondition is unchecked again"
    assert 'run "yaml parses: $path" uv run python' in script, (
        "the YAML check is back on the system interpreter, which may lack PyYAML and will report FAIL on a valid file"
    )
    phony = next(
        line for line in (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines() if line.startswith(".PHONY")
    )
    assert " prepush" in phony, "make prepush is not .PHONY — a file of that name would silence it"
