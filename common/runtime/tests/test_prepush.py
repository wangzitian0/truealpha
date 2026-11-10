# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; import subprocess; from pathlib import Path; import pytest; import yaml; REPO_ROOT = Path(__file__).resolve().parents[3]; PREPUSH = REPO_ROOT / "tools" / "prepush.sh"
def bash4() -> str:
    for candidate in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        probe = subprocess.run( [candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False )
        if probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout.strip()) >= 4: return candidate
    pytest.skip("no bash >= 4 available; tools/prepush.sh requires one and says so")
def run_prepush(*arguments: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]: return subprocess.run( [bash4(), str(PREPUSH), *arguments], capture_output=True, text=True, cwd=cwd, check=False, )
def test_an_empty_change_set_is_an_error_not_a_pass(tmp_path: Path) -> None:
    tree = tmp_path / "clean"; subprocess.run( ["git", "worktree", "add", "--detach", str(tree), "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT, check=True, )
    try: result = subprocess.run(["bash", str(PREPUSH), "HEAD"], capture_output=True, text=True, cwd=tree, check=False); assert result.returncode != 0, "prepush reported success over an empty change set"; assert "nothing changed" in result.stdout + result.stderr
    finally: subprocess.run( ["git", "worktree", "remove", "--force", str(tree)], capture_output=True, cwd=REPO_ROOT, check=False )
def test_untracked_files_are_examined(tmp_path: Path) -> None:
    if os.environ.get("TRUEALPHA_PREPUSH"): pytest.skip("already inside a prepush run — running it again here recurses without bound")
    tree = tmp_path / "untracked"; subprocess.run( ["git", "worktree", "add", "--detach", str(tree), "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT, check=True, )
    try:
        probe = tree / "tools" / "prepush_probe_delete_me.sh"
        probe.write_text("#!/usr/bin/env bash\nif [ 1 = 1 ]; then\n", encoding="utf-8")
        result = run_prepush("HEAD", cwd=tree); combined = result.stdout + result.stderr; assert "prepush_probe_delete_me.sh" in combined, ( 'an untracked file was not examined — `git ls-files --others` is gone and every brand-new tool now skips its own checks' ); assert "FAIL" in combined, "the untracked file's broken syntax was examined but not reported"
    finally: subprocess.run( ["git", "worktree", "remove", "--force", str(tree)], capture_output=True, cwd=REPO_ROOT, check=False )
def test_it_refuses_a_bash_too_old_to_run_it() -> None: script = PREPUSH.read_text(encoding="utf-8"); assert 'BASH_VERSINFO[0]:-0}" -lt 4' in script, "the bash-version gate is gone"; assert "brew install bash" in script, "the version failure no longer says how to fix it"
def test_it_names_its_own_preconditions_instead_of_failing_cryptically() -> None: script = PREPUSH.read_text(encoding="utf-8"); assert "not inside a git working tree" in script, "the work-tree precondition is unchecked again"; assert 'run "yaml parses: $path" uv run python' in script, ( "the YAML check is back on the system interpreter, which may lack PyYAML and will report FAIL on a valid file" ); phony = next( line for line in (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines() if line.startswith(".PHONY") ); assert " prepush" in phony, "make prepush is not .PHONY — a file of that name would silence it"
def test_it_runs_the_same_fast_stdlib_gates_as_ci_python_gates() -> None:
    script = PREPUSH.read_text(encoding="utf-8"); workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci-python.yml").read_text(encoding="utf-8")); steps = workflow["jobs"]["gates"]["steps"]; stdlib_gates = [ step["run"].strip() for step in steps if isinstance(step.get("run"), str) and step["run"].strip().startswith("python3 tools/") ]; assert len(stdlib_gates) >= 5, f"expected >= 5 stdlib gates in ci-python.yml gates job, found {stdlib_gates}"
    for gate in stdlib_gates: assert gate in script, f"prepush.sh is missing fast stdlib gate {gate!r} from ci-python.yml"
