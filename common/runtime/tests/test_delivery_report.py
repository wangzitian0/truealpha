"""Tests for tools/delivery_report.py: the delivery report of #1412 R2 and R3.

The report is a dashboard, not a gate (ADR A8). These tests use fixture API payloads.
They make no network call.

The streak tests are the R2 acceptance mechanics. `gh run list` shows only the last
attempt of a run. A deploy that failed and then passed on a rerun is one failure and one
success, so the streak reads every attempt.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
dr = load_tool("delivery_report")

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(
    run_id: int,
    title: str,
    started: str,
    conclusion: str | None = "success",
    *,
    attempt: int = 1,
    created: str | None = None,
    updated: str | None = None,
    status: str = "completed",
) -> dict:
    return {
        "id": run_id,
        "display_title": title,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "created_at": created or started,
        "run_started_at": started,
        "updated_at": updated or started,
    }


class FakeGitHub:
    """Answers the paths the report reads from fixtures. An unknown path is an unreadable input."""

    def __init__(
        self,
        *,
        deploy_runs: list[dict] | None = None,
        attempts: dict[tuple[int, int], dict] | None = None,
        responses: dict[str, object] | None = None,
    ) -> None:
        self.deploy_runs = deploy_runs
        self.attempts = attempts or {}
        self.responses = responses or {}
        self.seen: list[str] = []

    def __call__(self, path: str) -> str:
        self.seen.append(path)
        base, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if base == dr.DEPLOY_RUNS and self.deploy_runs is not None:
            page, size = int(params.get("page", "1")), int(params.get("per_page", "100"))
            runs = self.deploy_runs[(page - 1) * size : page * size]
            return json.dumps({"total_count": len(self.deploy_runs), "workflow_runs": runs})
        match = re.fullmatch(r"/repos/wangzitian0/truealpha/actions/runs/(\d+)/attempts/(\d+)", base)
        if match and (int(match[1]), int(match[2])) in self.attempts:
            return json.dumps(self.attempts[(int(match[1]), int(match[2]))])
        if path in self.responses:
            return json.dumps(self.responses[path])
        raise dr.InputUnavailable(f"GET {path} failed: no fixture")


def _attempt(deploy_type: str, hours_ago: float, conclusion: str, run_id: int = 0) -> object:
    stamp = NOW - timedelta(hours=hours_ago)
    return dr.Attempt(run_id, 1, deploy_type, "v0.1.0", stamp, stamp, conclusion)


# --- R2: the deploy streak --------------------------------------------------------------


def test_a_failed_earlier_attempt_breaks_the_streak() -> None:
    """Run 2 failed at 10 h ago, then passed on a rerun at 8 h ago. The run list shows only
    its last attempt (success). Counted from the list, the streak is 3. Counted from every
    attempt, the newest-first order is S, S, F, S, so the streak is 2."""
    runs = [
        _run(3, "Deploy staging v0.1.3", _stamp(9)),
        _run(2, "Deploy staging v0.1.2", _stamp(8), attempt=2, created=_stamp(10)),
        _run(1, "Deploy staging v0.1.1", _stamp(11)),
    ]
    first_try = _run(2, "Deploy staging v0.1.2", _stamp(10), "failure")
    gh = FakeGitHub(deploy_runs=runs, attempts={(2, 1): first_try})

    history = dr.read_deploy_history(gh)

    assert [(a.run_id, a.number, a.conclusion) for a in history.attempts] == [
        (2, 2, "success"),
        (3, 1, "success"),
        (2, 1, "failure"),
        (1, 1, "success"),
    ]
    assert "/repos/wangzitian0/truealpha/actions/runs/2/attempts/1" in gh.seen
    streak = dr.streak(history.attempts, exhaustive=history.exhaustive)
    assert (streak.count, streak.exact) == (2, True)


def test_staging_and_production_interleave_in_one_pooled_streak() -> None:
    attempts = [
        _attempt("staging", 1, "success"),
        _attempt("prod", 2, "success"),
        _attempt("staging", 3, "success"),
        _attempt("prod", 4, "failure"),
        _attempt("staging", 5, "success"),
        _attempt("staging", 6, "failure"),
    ]

    r2 = dr.r2_verdict(attempts, exhaustive=True)

    assert (r2.pooled.count, r2.pooled.exact) == (3, True)
    assert (r2.staging.count, r2.staging.exact) == (3, True)
    assert (r2.production.count, r2.production.exact) == (1, True)


def test_a_cancelled_attempt_breaks_the_streak_and_a_skipped_one_is_not_counted() -> None:
    """deploy-release.yml sets cancel-in-progress to false: no newer run cancels a deploy, and
    a cancelled deploy did not deliver its release. A skipped attempt ran nothing."""
    attempts = [
        _attempt("staging", 1, "success"),
        _attempt("staging", 2, "skipped"),
        _attempt("staging", 3, "success"),
        _attempt("staging", 4, "cancelled"),
        _attempt("staging", 5, "success"),
    ]
    streak = dr.streak(attempts, exhaustive=True)
    assert (streak.count, streak.exact) == (2, True)


def test_a_streak_that_reaches_the_end_of_a_partial_listing_is_a_lower_bound() -> None:
    runs = [_run(index, f"Deploy staging v0.1.{index}", _stamp(index)) for index in range(1, 103)]
    gh = FakeGitHub(deploy_runs=runs)

    history = dr.read_deploy_history(gh, max_pages=1)
    streak = dr.streak(history.attempts, exhaustive=history.exhaustive)

    assert history.exhaustive is False
    assert (streak.count, streak.exact) == (100, False)
    assert streak.text() == "at least 100"


def test_an_unreadable_earlier_attempt_stops_the_streak_as_a_lower_bound() -> None:
    runs = [
        _run(3, "Deploy staging v0.1.3", _stamp(1)),
        _run(2, "Deploy staging v0.1.2", _stamp(2), attempt=2, created=_stamp(4)),
        _run(1, "Deploy staging v0.1.1", _stamp(5)),
    ]
    history = dr.read_deploy_history(FakeGitHub(deploy_runs=runs))

    streak = dr.streak(history.attempts, exhaustive=history.exhaustive)

    assert (streak.count, streak.exact) == (2, False)
    assert history.unreadable and "run 2 attempt 1" in history.unreadable[0]


def _r2_fixture(production_failures: int) -> list:
    """25 staging successes (newest), then the 10 newest production attempts with
    `production_failures` failures at the oldest end. The pooled streak stays above 30."""
    attempts = [_attempt("staging", hour, "success") for hour in range(1, 26)]
    successes = 10 - production_failures
    attempts += [_attempt("prod", 30 + hour, "success") for hour in range(successes)]
    attempts += [_attempt("prod", 50 + hour, "failure") for hour in range(production_failures)]
    return attempts


def test_one_production_failure_in_the_last_ten_meets_r2() -> None:
    r2 = dr.r2_verdict(_r2_fixture(1), exhaustive=True)
    assert r2.pooled.count == 34
    assert (r2.production_window.size, r2.production_window.failures) == (10, 1)
    assert r2.met is True


def test_two_production_failures_in_the_last_ten_fail_r2() -> None:
    r2 = dr.r2_verdict(_r2_fixture(2), exhaustive=True)
    assert r2.pooled.count == 33
    assert (r2.production_window.size, r2.production_window.failures) == (10, 2)
    assert r2.met is False


def test_a_pooled_streak_below_thirty_fails_r2() -> None:
    attempts = [_attempt("staging", hour, "success") for hour in range(1, 20)]
    attempts += [_attempt("prod", 30 + hour, "success") for hour in range(10)]
    attempts += [_attempt("staging", 60, "failure")]
    r2 = dr.r2_verdict(attempts, exhaustive=True)
    assert (r2.pooled.count, r2.production_window.failures) == (29, 0)
    assert r2.met is False


# --- S4: Wilson intervals, S5: p-chart limits ---------------------------------------------


def test_the_wilson_interval_matches_published_values() -> None:
    assert dr.wilson(1, 10) == pytest.approx((0.01788, 0.40415), abs=1e-4)
    assert dr.wilson(0, 30) == pytest.approx((0.0, 0.11352), abs=1e-4)
    assert dr.wilson(3, 30) == pytest.approx((0.03458, 0.25621), abs=1e-4)
    assert dr.wilson(0, 0) is None


def test_the_p_chart_limits_follow_each_week_sample_size() -> None:
    chart = dr.p_chart([(50, 100), (5, 25), None, (0, 0)])
    assert chart.center == pytest.approx(55 / 125)
    assert chart.limits[0] == pytest.approx((0.29108, 0.58892), abs=1e-4)
    assert chart.limits[1] == pytest.approx((0.14217, 0.73783), abs=1e-4)
    assert chart.limits[2] is None and chart.limits[3] is None


def test_the_p_chart_limits_stay_inside_zero_and_one() -> None:
    chart = dr.p_chart([(10, 10), (9, 10)])
    assert chart.center == pytest.approx(0.95)
    assert chart.limits[0][1] == 1.0
    assert 0.0 <= chart.limits[0][0] < 0.95


# --- release age (moved from the deleted deploy_freshness tool) --------------------------

TAGS = ["v0.2.27", "v0.2.26", "v0.2.25", "v0.2.24", "v0.2.23", "v0.2.22"]


def _compare(base: str, head: str, ahead_by: int, oldest: str) -> dict[str, object]:
    path = f"/repos/wangzitian0/truealpha/compare/{base}...{head}?per_page=1"
    body = {"status": "ahead", "ahead_by": ahead_by, "commits": [{"commit": {"committer": {"date": oldest}}}]}
    return {path: body}


def test_the_newest_tag_is_the_highest_version_not_the_first_listed() -> None:
    assert dr.newest_tag(["v0.9.0", "v0.10.0", "v0.2.0", "latest", "v1.0.0-rc1"]) == "v0.10.0"


def test_release_age_is_the_age_of_the_oldest_commit_the_served_release_lacks() -> None:
    gh = FakeGitHub(responses=_compare("v0.2.23", "v0.2.27", 10, "2026-10-16T12:00:00Z"))
    staging, production = dr.ENVIRONMENTS

    on_staging = dr.release_age(staging, "v0.2.23", TAGS, gh, NOW)
    on_production = dr.release_age(production, "v0.2.23", TAGS, gh, NOW)

    assert (on_staging.newest, on_staging.tags_behind, on_staging.commits_behind) == ("v0.2.27", 4, 10)
    assert on_staging.age == timedelta(days=4)
    assert on_staging.within_bound is False, "4 days is over the staging bound of 3 days"
    assert on_production.within_bound is True, "4 days is inside the production bound of 14 days"
    assert (staging.max_age_days, production.max_age_days) == (3, 14)


def test_the_release_script_states_the_bounds_the_report_shows() -> None:
    """#819: `cut_release.sh` documents the promotion policy. The report shows the same bounds.
    Two copies that disagree send the operator one rule and show the reader another."""
    staging, production = dr.ENVIRONMENTS
    script = (REPO_ROOT / "tools" / "cut_release.sh").read_text(encoding="utf-8")
    header = script.split("# Promotion policy (#819).", 1)[1].split("set -euo pipefail", 1)[0]
    assert "tools/delivery_report.py" in header
    for phrase in (f"staging {staging.max_age_days} days", f"production {production.max_age_days} days"):
        assert phrase in " ".join(line.lstrip("# ") for line in header.splitlines()), phrase


def test_a_release_that_serves_the_newest_tag_has_no_age() -> None:
    staging = dr.ENVIRONMENTS[0]
    age = dr.release_age(staging, "v0.2.27", TAGS, FakeGitHub(), NOW)
    assert (age.tags_behind, age.commits_behind, age.age) == (0, 0, None)
    assert age.within_bound is True


# --- R3: tier rates ---------------------------------------------------------------------


def _ci(tier: str, sha: str, run_id: int, number: int, hours_ago: float, conclusion: str) -> object:
    stamp = NOW - timedelta(hours=hours_ago)
    return dr.CiAttempt(tier, sha, run_id, number, stamp, conclusion)


def test_first_run_pass_and_flake_count_each_sha_once() -> None:
    attempts = [
        _ci("main", "a", 10, 1, 9, "failure"),
        _ci("main", "a", 10, 2, 8, "success"),
        _ci("pr", "b", 11, 1, 9, "cancelled"),
        _ci("pr", "b", 12, 1, 7, "success"),
        _ci("pr", "c", 13, 1, 9, "failure"),
        _ci("main", "d", 14, 1, 9, "success"),
    ]
    records = dr.sha_records(attempts)
    by_sha = {record.sha: record for record in records}

    assert by_sha["a"].first_passed is False and by_sha["a"].flaky is True
    assert by_sha["b"].first_passed is True and by_sha["b"].flaky is False, "a cancelled run carries no verdict"
    assert by_sha["c"].first_passed is False and by_sha["c"].flaky is False, "a failure that never passed is no flake"
    assert by_sha["d"].first_passed is True
    assert [(a.run_id, a.number) for a in by_sha["a"].failed_before_pass()] == [(10, 1)]


def test_lead_time_runs_from_merge_to_the_first_deploy_that_carries_the_pr() -> None:
    monday = datetime(2026, 10, 12, tzinfo=UTC)
    main = ["c5", "c4", "c3", "c2", "c1"]
    tags = {"v0.1.0": "c1", "v0.1.1": "c3", "v0.1.2": "c5"}

    def done(deploy_type: str, release: str, at: datetime) -> object:
        return dr.Attempt(1, 1, deploy_type, release, at, at, "success")

    deploys = [
        done("prod", "v0.1.1", monday + timedelta(days=2, hours=12)),
        done("staging", "v0.1.2", monday + timedelta(days=1, hours=9)),
        done("staging", "v0.1.1", monday + timedelta(hours=12)),
    ]
    prs = [
        dr.MergedPr(1, "c2", monday + timedelta(hours=10)),
        dr.MergedPr(2, "c4", monday + timedelta(hours=14)),
        dr.MergedPr(3, "not-on-main", monday + timedelta(hours=15)),
    ]

    leads = {lead.pr.number: lead for lead in dr.lead_times(prs, main, tags, deploys)}

    assert leads[1].to_staging == timedelta(hours=2)
    assert leads[1].to_production == timedelta(hours=48)
    assert leads[1].production_release == "v0.1.1"
    assert leads[2].to_staging == timedelta(hours=19)
    assert leads[2].to_production is None
    assert 3 not in leads, "a merge commit the main listing does not hold has no measured lead time"


# --- unknown inputs ---------------------------------------------------------------------


def _unreachable(url: str) -> tuple[int, str]:
    return 0, f"{url}: connection refused"


def _down(base: str) -> list[str]:
    return [f"the surface is not serving: GET {base}/api/health did not answer"]


def test_an_unreadable_input_prints_unknown_and_never_a_number(tmp_path: Path) -> None:
    gh = FakeGitHub()

    report = dr.build_report(gh, _unreachable, now=NOW, repo=str(tmp_path), routing=_down)

    assert "| staging + production |" not in report, "a streak row needs a deploy history"
    assert re.search(r"\d+\.\d%", report) is None, "a rate was printed without a measurement"
    assert re.search(r"\| (staging|production) \| unknown", report), report
    assert report.count("unknown") >= 6, report


def test_the_routing_probe_gets_each_base_not_its_health_endpoint(tmp_path: Path) -> None:
    """A4 C1 (#673): `surface_contract` builds its own paths from a base. Given the health
    endpoint, it requests /api/health/api/health and reports a healthy surface as down."""
    probed: list[str] = []

    def routing(base: str) -> list[str]:
        probed.append(base)
        return []

    report = dr.build_report(FakeGitHub(), _unreachable, now=NOW, repo=str(tmp_path), routing=routing)

    assert probed == [environment.base for environment in dr.ENVIRONMENTS]
    assert not any(base.endswith("/api/health") for base in probed)
    assert "- staging: the deployed surface holds" in report


def test_a_release_in_flight_is_reported_without_a_wait() -> None:
    """The deleted workflow waited up to 15 min for a release caught mid-flight. A dashboard
    shows the state it reads: the walk of a release still deploying is "in flight"."""
    deploying = _run(7, "Deploy staging v0.2.27", _stamp(0.1), None, status="in_progress")
    walks = "/repos/wangzitian0/truealpha/actions/workflows/walk-release.yml/runs?per_page=100"
    deploys = "/repos/wangzitian0/truealpha/actions/workflows/deploy-release.yml/runs?per_page=100"
    gh = FakeGitHub(responses={walks: {"workflow_runs": []}, deploys: {"workflow_runs": [deploying]}})
    served = {"staging": "v0.2.27", "production": dr.Unknown("not read")}

    lines = dr.walk_section(served, gh, NOW)

    assert lines[4].startswith("| staging | v0.2.27 | in flight: 'Deploy staging v0.2.27' (run 7) is in_progress")
    assert lines[5] == "| production | unknown | unknown (not read) |"


def test_main_writes_the_report_to_the_step_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(dr, "build_report", lambda *_args, **_kwargs: "# Delivery report\n")

    assert dr.main(["--repo", str(tmp_path)]) == 0
    assert summary.read_text(encoding="utf-8") == "# Delivery report\n"


def test_the_report_runs_on_a_bare_python_without_site_packages() -> None:
    """The workflow runs the report on the runner's `python3`, with no `uv sync`. Every module
    it reuses (walk_evidence, nightly_verdicts, surface_contract) must import without the SDK."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(REPO_ROOT / "tools" / "delivery_report.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
