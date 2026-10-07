"""test_deploy_provenance.py
TDD Suite: Verify deploy provenance guard in tools/doctor.py.

Asserts that --verify-deploy-ref <target> fails closed when the target ref
does not contain the current branch commits, preventing deploying outdated releases
or deploying from unmerged branches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

doctor = load_tool("doctor")


@pytest.fixture
def isolated_git_repo(tmp_path: Path) -> Path:
    """Create an isolated deterministic git repo with multiple branches."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    def run_git(*args: str) -> str:
        res = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    run_git("init", "-b", "main")
    (repo / "f1.txt").write_text("v1\n", encoding="utf-8")
    run_git("add", "f1.txt")
    run_git("commit", "-m", "commit 1 (base)")
    run_git("tag", "v0.0.1")

    # Branch feature: advance HEAD
    run_git("checkout", "-b", "feature")
    (repo / "f2.txt").write_text("v2\n", encoding="utf-8")
    run_git("add", "f2.txt")
    run_git("commit", "-m", "commit 2 (feature HEAD)")

    # Branch release: advance from feature (contains feature HEAD)
    run_git("checkout", "-b", "release")
    (repo / "f3.txt").write_text("v3\n", encoding="utf-8")
    run_git("add", "f3.txt")
    run_git("commit", "-m", "commit 3 (release)")
    run_git("tag", "v0.0.2")

    # Switch back to feature (HEAD is commit 2)
    run_git("checkout", "feature")
    return repo


def test_deploy_provenance_accepts_ancestor_release(isolated_git_repo: Path) -> None:
    # v0.0.2 is commit 3 which was built on top of feature (commit 2), so HEAD is an ancestor
    assert doctor.check_deploy_provenance("v0.0.2", cwd=isolated_git_repo) is True
    assert doctor.check_deploy_provenance("HEAD", cwd=isolated_git_repo) is True


def test_deploy_provenance_rejects_older_tag_where_ref_resolves(isolated_git_repo: Path) -> None:
    # v0.0.1 resolves cleanly in git, but does NOT contain commit 2 (feature HEAD).
    # This directly exercises the `is_ancestor is False` branch.
    assert doctor.check_deploy_provenance("v0.0.1", cwd=isolated_git_repo) is False


def test_deploy_provenance_rejects_invalid_ref(isolated_git_repo: Path) -> None:
    # Exercises CalledProcessError branch
    assert doctor.check_deploy_provenance("non-existent-ref-xyz", cwd=isolated_git_repo) is False
