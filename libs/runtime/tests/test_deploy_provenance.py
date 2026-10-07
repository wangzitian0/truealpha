"""test_deploy_provenance.py
TDD Suite: Verify deploy provenance guard in tools/doctor.py.

Asserts that --verify-deploy-ref <target> fails closed when the target ref
does not contain the current branch commits, preventing deploying outdated releases
or deploying from unmerged branches.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import doctor  # noqa: E402


def test_deploy_provenance_accepts_head() -> None:
    assert doctor.check_deploy_provenance("HEAD") is True


def test_deploy_provenance_rejects_older_tag() -> None:
    # v0.0.109 does not contain current commits on main
    assert doctor.check_deploy_provenance("v0.0.109") is False


def test_deploy_provenance_rejects_invalid_ref() -> None:
    assert doctor.check_deploy_provenance("non-existent-ref-xyz") is False
