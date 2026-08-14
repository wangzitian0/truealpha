"""Tests for tools/deploy_freshness.py — #560.

Both environments served v0.0.19 for 15 days while main ran 18 commits ahead,
and nothing was red about it. The bound here is AGE rather than commit count on
purpose: a count measures how busy the repository has been, age measures how
long finished work has been invisible.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools/deploy_freshness.py"
SPEC = importlib.util.spec_from_file_location("truealpha_deploy_freshness", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _module
SPEC.loader.exec_module(_module)
check_freshness = _module.check_freshness

URL = "https://truealpha.club/api/health"
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _health(git_sha: str):
    def http_get(url: str) -> tuple[int, str]:
        return 200, json.dumps({"status": "ok", "git_sha": git_sha})

    return http_get


def _git(log_lines: list[str], *, resolves: bool = True):
    """Fake `git`: `rev-parse` proves the ref exists, `log` yields %cI\\x1f%s."""

    def run(argv, capture_output=True, text=True, check=False):  # noqa: ANN001, ARG001
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0 if resolves else 128, "cafe123\n" if resolves else "", "")
        return subprocess.CompletedProcess(argv, 0, "\n".join(log_lines), "")

    return run


def test_current_environment_passes(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_freshness(URL, environment="production", http_get=_health("v0.0.19"), now=NOW, run=_git([]))
    assert exit_code == 0
    assert "is current" in capsys.readouterr().out


def test_recent_unreleased_work_is_not_stale(capsys: pytest.CaptureFixture[str]) -> None:
    """Ten commits merged this morning are not the failure this guards."""
    fresh = (NOW - timedelta(hours=6)).isoformat()
    exit_code = check_freshness(
        URL,
        environment="production",
        http_get=_health("v0.0.19"),
        now=NOW,
        run=_git([f"{fresh}\x1fa recent merge" for _ in range(10)]),
    )
    assert exit_code == 0
    assert "fresh enough" in capsys.readouterr().out


def test_one_old_commit_is_stale(capsys: pytest.CaptureFixture[str]) -> None:
    """One commit merged last week IS. Age, not count, is the bound."""
    old = (NOW - timedelta(days=7)).isoformat()
    exit_code = check_freshness(
        URL,
        environment="production",
        http_get=_health("v0.0.19"),
        now=NOW,
        run=_git([f"{old}\x1fthe one that has been invisible"]),
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "production is stale" in stderr
    assert "v0.0.19" in stderr, "the operator must learn WHICH release is deployed"
    assert "7d" in stderr, "and HOW far behind, without opening a shell"
    assert "the one that has been invisible" in stderr


def test_the_real_15_day_gap_would_have_fired(capsys: pytest.CaptureFixture[str]) -> None:
    """The exact condition that went unnoticed: v0.0.19, 18 commits, 15 days."""
    oldest = (NOW - timedelta(days=15)).isoformat()
    recent = (NOW - timedelta(hours=2)).isoformat()
    exit_code = check_freshness(
        URL,
        environment="production",
        http_get=_health("v0.0.19"),
        now=NOW,
        run=_git([f"{oldest}\x1fBump infra2-sdk pin"] + [f"{recent}\x1flater" for _ in range(17)]),
    )
    assert exit_code == 1
    assert "18 commit(s)" in capsys.readouterr().err


def test_an_environment_that_reports_no_release_identity_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`GIT_COMMIT_SHA` defaults to "unknown"; freshness is then unjudgeable."""
    exit_code = check_freshness(URL, environment="staging", http_get=_health("unknown"), now=NOW, run=_git([]))
    assert exit_code == 1
    assert "does not report a release identity" in capsys.readouterr().err


def test_an_unreachable_environment_fails(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_freshness(URL, environment="staging", http_get=lambda _url: (503, "down"), now=NOW, run=_git([]))
    assert exit_code == 1
    assert "HTTP 503" in capsys.readouterr().err


def test_a_ref_this_checkout_cannot_resolve_fails(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_freshness(
        URL,
        environment="production",
        http_get=_health("v9.9.9"),
        now=NOW,
        run=_git([], resolves=False),
    )
    assert exit_code == 1
    assert "not a commit here" in capsys.readouterr().err
