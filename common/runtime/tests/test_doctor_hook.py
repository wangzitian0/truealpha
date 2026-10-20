"""`make doctor` reports a clone without the per-commit hook (#1412 T0).

Before this, no clone had a hook (`.git/hooks` held only samples): `make install` ran
`uvx pre-commit install 2>/dev/null || true`, which failed silently, and nothing else
installed it. The tier that stops a secret or a trading-API call at commit time did
not run anywhere, and no check said so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

doctor = load_tool("doctor")

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(cwd: Path, *args: str) -> str:
    empty = cwd.parent / "gitconfig"
    empty.touch()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone that holds the tracked hook and has never run `make install`."""
    empty = tmp_path / "gitconfig"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "clone"
    hook = root / doctor.HOOKS_DIR / "pre-commit"
    hook.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / doctor.HOOKS_DIR / "pre-commit", hook)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_the_tracked_hook_checks_staged_files_through_the_pre_commit_config() -> None:
    hook = REPO_ROOT / doctor.HOOKS_DIR / "pre-commit"
    assert hook.is_file() and os.access(hook, os.X_OK), f"{hook} is missing or not executable"
    command = [line for line in hook.read_text(encoding="utf-8").splitlines() if line.startswith("exec ")]
    assert command == ['exec uvx --from "pre-commit==4.2.0" pre-commit run --hook-stage pre-commit'], (
        "the hook must run the pre-commit config on the staged files only: `--all-files` or a file "
        "list would make every commit pay for the whole tree"
    )


def test_doctor_fails_on_a_clone_without_the_hook(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert doctor.check_git_hook(clone) is False
    # Through the entry point that `make doctor` runs: every other check passes here, so
    # the exit code depends on the hook alone.
    monkeypatch.setattr(doctor, "ROOT", clone)
    for name in ("check_python_version", "check_uv", "check_bun_and_web", "check_local_services"):
        monkeypatch.setattr(doctor, name, lambda: True)
    assert doctor.main([]) == 1
    assert doctor.install_git_hook(clone) is True
    assert doctor.main([]) == 0


def test_one_install_covers_every_worktree_of_the_clone(clone: Path) -> None:
    worktree = clone.parent / "worktree"
    _git(clone, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    assert doctor.check_git_hook(worktree) is False
    assert doctor.install_git_hook(clone) is True
    assert doctor.check_git_hook(worktree) is True
    # The setting is relative, so the worktree runs its own checked-out copy of the hook.
    hooks = _git(worktree, "rev-parse", "--git-path", "hooks").strip()
    assert hooks == doctor.HOOKS_DIR


def test_a_hook_path_that_names_another_directory_fails(clone: Path) -> None:
    _git(clone, "config", "core.hooksPath", ".git/hooks")
    assert doctor.check_git_hook(clone) is False


def test_make_install_installs_the_hook() -> None:
    recipe = subprocess.run(["make", "-n", "install"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    assert "tools/doctor.py --install-hooks" in recipe, recipe
    assert "|| true" not in recipe, "an install step that cannot fail hides a missing hook"
