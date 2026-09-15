"""Tests for tools/deploy_freshness.py — #560.

Both environments served v0.0.19 for 15 days while main ran 18 commits ahead,
and nothing was red about it. The bound here is AGE rather than commit count on
purpose: a count measures how busy the repository has been, age measures how
long finished work has been invisible.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
_module = load_tool("deploy_freshness")
check_freshness = _module.check_freshness
STAGING_MAX_AGE_DAYS = _module.STAGING_MAX_AGE_DAYS
PRODUCTION_MAX_AGE_DAYS = _module.PRODUCTION_MAX_AGE_DAYS

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


def test_a_ref_git_could_read_as_an_option_never_reaches_git(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reported release arrives over HTTP; git must not be handed an option (review)."""
    reached_git = False

    def run(argv, capture_output=True, text=True, check=False):  # noqa: ANN001, ARG001
        nonlocal reached_git
        reached_git = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    for hostile in ("--upload-pack=touch /tmp/x", "-n", "v1 --all", "a" * 300):
        exit_code = check_freshness(URL, environment="production", http_get=_health(hostile), now=NOW, run=run)
        assert exit_code == 1, f"{hostile!r} must be refused"
        assert "not a usable release identifier" in capsys.readouterr().err
    assert not reached_git, "a hostile ref must be rejected before any git invocation"


def test_the_bound_is_per_environment_and_production_lags_by_design(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#819: every tag soaks staging and production moves only with
    `cut_release.sh --prod`, so the lag that is a defect on staging is the policy
    on production. The shape the production leg was red on daily (run
    34940255057: oldest unreleased 4d19h, limit 3d) is stale under staging's
    bound and fresh enough under production's — same commit, same age."""
    merged = (NOW - timedelta(days=4, hours=19)).isoformat()
    log = [f"{merged}\x1fthe one production is allowed to lag on"]

    staging = check_freshness(
        URL,
        environment="staging",
        http_get=_health("v0.0.51"),
        now=NOW,
        max_age_days=STAGING_MAX_AGE_DAYS,
        run=_git(log),
    )
    assert staging == 1
    assert "staging is stale" in capsys.readouterr().err

    production = check_freshness(
        URL,
        environment="production",
        http_get=_health("v0.0.51"),
        now=NOW,
        max_age_days=PRODUCTION_MAX_AGE_DAYS,
        run=_git(log),
    )
    assert production == 0
    out = capsys.readouterr().out
    assert "production is fresh enough" in out
    assert f"limit {PRODUCTION_MAX_AGE_DAYS}d" in out, "the bound in force must be the one the operator reads"


def test_the_production_bound_still_fires_on_the_15_day_gap(capsys: pytest.CaptureFixture[str]) -> None:
    """The wider bound must not reopen #560: 15 days is what went unnoticed, and
    production's bound has to sit below it or this check no longer guards the
    failure it was written for."""
    assert PRODUCTION_MAX_AGE_DAYS < 15
    oldest = (NOW - timedelta(days=15)).isoformat()
    exit_code = check_freshness(
        URL,
        environment="production",
        http_get=_health("v0.0.19"),
        now=NOW,
        max_age_days=PRODUCTION_MAX_AGE_DAYS,
        run=_git([f"{oldest}\x1fBump infra2-sdk pin"]),
    )
    assert exit_code == 1
    assert f"limit {PRODUCTION_MAX_AGE_DAYS}d" in capsys.readouterr().err


def test_the_cli_passes_the_bound_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """deploy-freshness.yml reaches the bound only through `--max-age-days`; a
    flag the parser accepted and dropped would leave both legs on the default
    with nothing red about it. Asserted through `main`, the deployed entry."""
    seen: dict[str, object] = {}

    def record(url: str, **kwargs: object) -> int:
        seen.update(kwargs, url=url)
        return 0

    monkeypatch.setattr(_module, "check_freshness", record)
    argv = [URL, "--environment", "production", "--max-age-days", str(PRODUCTION_MAX_AGE_DAYS)]
    assert _module.main(argv) == 0
    assert seen["max_age_days"] == PRODUCTION_MAX_AGE_DAYS
    assert seen["environment"] == "production"
    assert seen["url"] == URL
