"""Tests for `release_history`: the staging releases, read from the GitHub run lists (#1468 F1).

`FakeApi` answers the REST paths that `read_releases` reads. These tests build small histories by
hand. `test_staging_line.py` feeds the real history of 2026-10-13 to 2026-10-21 through the same
reader.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from truealpha_tools import release_history
from truealpha_tools.release_history import JobReader, read_releases
from truealpha_tools.staging_line import staging_red

REPO = "wangzitian0/truealpha"
T0 = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
CI_STEP = "images_release / plan::Select available images"
SUMMARY = {
    "name": "ci-required",
    "conclusion": "failure",
    "steps": [{"name": "Reject any failed", "conclusion": "failure"}],
}


def stamp(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    run_id: int, title: str, conclusion: str | None, start: int, end: int, branch: str = "main", attempt: int = 1
) -> dict:
    return {
        "run_attempt": attempt,
        "id": run_id,
        "display_title": title,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "created_at": stamp(start),
        "updated_at": stamp(end),
        "head_branch": branch,
    }


def job(name: str, step: str, conclusion: str = "failure") -> dict:
    return {"name": name, "conclusion": conclusion, "steps": [{"name": step, "conclusion": conclusion}]}


class FakeApi:
    def __init__(self, *, deploys=(), walks=(), ci=(), jobs=None, attempts=None) -> None:
        self.deploys = list(deploys)
        self.walks = list(walks)
        self.ci = list(ci)
        self.jobs = jobs or {}
        self.attempts = attempts or {}
        self.calls: list[str] = []

    def jobs_read(self) -> list[int]:
        return sorted(int(c.split("/runs/")[1].split("/")[0]) for c in self.calls if "/jobs?" in c)

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        attempt = re.search(r"/runs/(\d+)/attempts/(\d+)(/jobs)?", path)
        if attempt:
            key = (int(attempt.group(1)), int(attempt.group(2)))
            return {"jobs": self.jobs.get(key, [])} if attempt.group(3) else self.attempts[key]
        if "/actions/runs/" in path and "/jobs?" in path:
            return {"jobs": self.jobs.get(int(path.split("/runs/")[1].split("/")[0]), [])}
        if "workflows/deploy-release.yml/runs" in path:
            return {"workflow_runs": self.deploys}
        if "workflows/walk-release.yml/runs" in path:
            return {"workflow_runs": self.walks}
        if "workflows/ci-required.yml/runs?branch=" in path:
            branch = path.split("branch=")[1].split("&")[0]
            return {"workflow_runs": [r for r in self.ci if r["head_branch"] == branch]}
        failed = re.search(r"workflows/ci-required.yml/runs\?event=push&status=(\w+)&", path)
        if failed:
            return {"workflow_runs": [r for r in self.ci if r["conclusion"] == failed.group(1)]}
        raise AssertionError(f"unexpected GET {path}")


def walk_run(run_id: int, tag: str, start: int) -> dict:
    return run(run_id, f"Walk Deploy staging {tag}", "success", start, start + 3)


def walk_jobs(conclusion: str) -> list[dict]:
    return [
        {
            "name": "walk",
            "conclusion": "success",
            "steps": [{"name": "Walk the deployed surface", "conclusion": conclusion}],
        }
    ]


def test_failed_step_skips_the_summary_job() -> None:
    api = FakeApi(jobs={7: [SUMMARY, job("images_release / plan", "Select available images")]})
    assert JobReader(REPO, api).failed_step(7) == "images_release / plan::Select available images"


def test_failed_step_is_empty_when_only_the_summary_job_failed() -> None:
    """The summary job names no cause, so a run that only the summary job failed has no step."""
    assert JobReader(REPO, FakeApi(jobs={7: [SUMMARY]})).failed_step(7) == ""
    assert JobReader(REPO, FakeApi(jobs={7: []})).failed_step(7) == ""


def test_failed_step_skips_a_step_that_did_not_fail() -> None:
    steps = [
        {"name": "Checkout", "conclusion": "success"},
        {"name": "Dispatch the validated request to infra2", "conclusion": "failure"},
        {"name": "Post step", "conclusion": "skipped"},
    ]
    api = FakeApi(jobs={7: [{"name": "request", "conclusion": "failure", "steps": steps}]})
    assert JobReader(REPO, api).failed_step(7) == "request::Dispatch the validated request to infra2"


def test_job_reader_reads_the_jobs_of_a_run_once() -> None:
    api = FakeApi(jobs={7: [job("request", "Dispatch")]})
    reader = JobReader(REPO, api)
    reader.failed_step(7)
    reader.failed_step(7)
    assert api.jobs_read() == [7]


def test_job_reader_refuses_a_payload_without_jobs() -> None:
    with pytest.raises(ValueError, match="jobs payload"):
        JobReader(REPO, lambda path: {"unexpected": True}).jobs(7)


def test_one_release_for_each_tag_push_and_a_deleted_tag_counts() -> None:
    """v0.0.5 failed its deploy. v0.0.6 was pushed twice and its CI failed twice. The summary job
    failed too and does not name the step."""
    api = FakeApi(
        deploys=[
            run(105, "Deploy staging v0.0.5", "failure", 10, 15),
            run(90, "Deploy prod v0.0.5", "failure", 20, 25),
        ],
        ci=[
            run(150, "ci-required", "success", 0, 5, "v0.0.5"),
            run(201, "ci-required", "failure", 30, 35, "v0.0.6"),
            run(202, "ci-required", "failure", 60, 65, "v0.0.6"),
            run(300, "ci-required", "failure", 70, 71, "some-feature-branch"),
        ],
        jobs={
            105: [job("request", "Dispatch")],
            201: [SUMMARY, job("images_release / plan", "Select available images")],
            202: [SUMMARY, job("images_release / plan", "Select available images")],
        },
    )
    releases = read_releases(REPO, api, next_tag="v0.0.6")
    assert [(r.tag, r.cut_at) for r in releases] == [
        ("v0.0.5", T0),
        ("v0.0.6", T0 + timedelta(minutes=30)),
        ("v0.0.6", T0 + timedelta(minutes=60)),
    ]
    assert [d.conclusion for d in releases[0].deploys] == ["failure"], "a prod deploy is no staging deploy"
    assert [r.ci[0].failed_step for r in releases[1:]] == [CI_STEP, CI_STEP]
    red = staging_red(releases)
    assert red is not None and red.since == "v0.0.6" and red.label.startswith("ci-required / ")


def test_a_hand_cut_that_skips_a_deleted_number_does_not_hide_its_failed_pushes() -> None:
    """v0.0.6 failed its CI twice and was deleted. An operator cuts v0.0.7. The pushes of v0.0.6 have
    no deploy and no walk run, so the list of failed tag pushes is the only place that names them."""
    api = FakeApi(
        deploys=[
            run(40, "Deploy staging v0.0.5", "success", 1, 4),
            run(70, "Deploy staging v0.0.7", "failure", 90, 95),
        ],
        walks=[walk_run(41, "v0.0.5", 5)],
        ci=[
            run(50, "ci-required", "success", 0, 5, "v0.0.5"),
            run(51, "ci-required", "failure", 30, 35, "v0.0.6"),
            run(52, "ci-required", "failure", 60, 65, "v0.0.6"),
            run(53, "ci-required", "success", 85, 89, "v0.0.7"),
        ],
        jobs={
            41: walk_jobs("success"),
            51: [job("images_release / plan", "Select available images")],
            52: [job("images_release / plan", "Select available images")],
            70: [job("request", "Dispatch")],
        },
    )
    releases = read_releases(REPO, api, next_tag="v0.0.8")
    assert [(r.tag, len(r.ci)) for r in releases] == [("v0.0.5", 1), ("v0.0.6", 1), ("v0.0.6", 1), ("v0.0.7", 1)]
    red = staging_red(releases)
    assert red is not None
    assert red.since == "v0.0.6"
    assert red.newest_failed == "v0.0.7"


def green_history(*, last_green: int, tags: int = 20) -> FakeApi:
    """Tags v0.0.1 to v0.0.<tags>. Each tag has a deploy at minute 10n and a walk at 10n+5. The
    walk of tag `last_green` passes. Every later walk fails."""
    deploys, walks, ci, jobs = [], [], [], {}
    for n in range(1, tags + 1):
        tag = f"v0.0.{n}"
        deploys.append(run(1000 + n, f"Deploy staging {tag}", "success", 10 * n, 10 * n + 3))
        ok = n <= last_green
        walks.append(
            run(2000 + n, f"Walk Deploy staging {tag}", "success" if ok else "failure", 10 * n + 4, 10 * n + 6)
        )
        jobs[2000 + n] = walk_jobs("success") if ok else [job("walk", "Walk the deployed surface")]
        ci.append(run(3000 + n, "ci-required", "success", 10 * n - 1, 10 * n + 1, tag))
    return FakeApi(deploys=deploys, walks=walks, ci=ci, jobs=jobs)


def test_the_reader_starts_at_the_newest_green_walk_and_reads_forward() -> None:
    api = green_history(last_green=12, tags=20)
    tags = [r.tag for r in read_releases(REPO, api, next_tag="v0.0.21")]
    assert tags == [f"v0.0.{n}" for n in range(12, 21)], "the tag of the green walk and every later tag"


def test_with_no_green_walk_the_reader_takes_the_newest_tags_up_to_the_bound() -> None:
    api = green_history(last_green=0, tags=20)
    tags = [r.tag for r in read_releases(REPO, api, next_tag=None, max_tags=8)]
    assert tags == [f"v0.0.{n}" for n in range(13, 21)]


def test_a_walk_with_a_skipped_step_does_not_start_the_window() -> None:
    """The newest success walk, v0.0.15, walked nothing. The window starts at the v0.0.12 walk."""
    api = green_history(last_green=12, tags=20)
    for run_ in api.walks:
        if run_["id"] == 2015:
            run_["conclusion"] = "success"
    api.jobs[2015] = walk_jobs("skipped")
    tags = [r.tag for r in read_releases(REPO, api, next_tag=None)]
    assert tags[0] == "v0.0.12"


def test_a_failed_push_after_the_green_walk_is_read_and_one_before_it_is_not() -> None:
    api = green_history(last_green=12, tags=14)
    api.ci += [
        run(4001, "ci-required", "failure", 5, 6, "v0.0.3"),
        run(4002, "ci-required", "failure", 200, 201, "v0.0.16"),
        run(4003, "ci-required", "timed_out", 210, 211, "v0.0.17"),
    ]
    tags = [(r.tag, len(r.ci)) for r in read_releases(REPO, api, next_tag=None)]
    assert ("v0.0.16", 1) in tags and ("v0.0.17", 1) in tags
    assert not any(tag == "v0.0.3" for tag, _ in tags)


def test_the_next_tag_is_read_even_when_it_has_no_run_of_any_kind() -> None:
    api = green_history(last_green=12, tags=14)
    api.ci.append(run(4005, "ci-required", "failure", 300, 301, "v0.0.15"))
    tags = [r.tag for r in read_releases(REPO, api, next_tag="v0.0.15")]
    assert tags[-1] == "v0.0.15"


def test_only_the_newest_pushes_of_a_tag_are_read() -> None:
    pushes = [run(100 + n, "ci-required", "failure", 10 * n, 10 * n + 1, "v0.0.6") for n in range(12)]
    releases = read_releases(REPO, FakeApi(ci=pushes), next_tag="v0.0.6")
    assert len(releases) == release_history.CI_ATTEMPTS_PER_TAG
    assert releases[-1].cut_at == T0 + timedelta(minutes=110), "the newest push is kept"
    assert releases[0].cut_at == T0 + timedelta(minutes=40), "the four oldest pushes are dropped"


def test_a_deploy_and_a_walk_belong_to_the_newest_push_of_the_tag_only() -> None:
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", "success", 40, 45)],
        walks=[walk_run(71, "v0.0.6", 46)],
        ci=[run(51, "ci-required", "failure", 0, 5, "v0.0.6"), run(52, "ci-required", "success", 30, 35, "v0.0.6")],
        jobs={51: [job("images_release / plan", "Select available images")], 71: walk_jobs("success")},
    )
    older, newest = read_releases(REPO, api, next_tag=None)
    assert (older.deploys, older.walks) == ((), ())
    assert len(newest.deploys) == 1 and newest.walked_green


def test_a_walk_that_started_before_the_push_still_counts() -> None:
    """On 2026-10-20 an operator started the walk of v0.2.23 30 seconds before its CI run began.
    A time filter dropped that walk, which was the first green one of the tag."""
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", "success", 5, 9)],
        walks=[walk_run(71, "v0.0.6", 0)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
        jobs={71: walk_jobs("success")},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert release.walked_green


def test_a_green_walk_run_with_a_skipped_walk_step_is_no_green_walk() -> None:
    """With no walk credentials the run ends `success` and the step is skipped. Nothing was walked."""
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", "success", 5, 9)],
        walks=[walk_run(71, "v0.0.6", 10)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
        jobs={71: walk_jobs("skipped")},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert not release.walked_green
    assert [w.conclusion for w in release.walks] == ["skipped"]


def test_a_run_in_flight_has_no_conclusion() -> None:
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", None, 5, 9)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert [d.conclusion for d in release.deploys] == [""]
    assert release.failure() is None


def test_a_tag_with_deploy_runs_and_no_ci_run_is_one_release_dated_by_its_first_run() -> None:
    api = FakeApi(
        deploys=[
            run(70, "Deploy staging v0.0.6", "failure", 20, 25),
            run(71, "Deploy staging v0.0.6", "failure", 30, 35),
        ],
        jobs={71: [job("request", "Dispatch")]},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert release.cut_at == T0 + timedelta(minutes=20)
    assert release.failure() is not None and release.failure().step == "request::Dispatch"  # type: ignore[union-attr]


def test_the_next_tag_without_any_run_is_no_release() -> None:
    assert read_releases(REPO, FakeApi(), next_tag="v0.0.6") == ()


def test_jobs_are_read_for_the_newest_failed_run_of_each_stage_only() -> None:
    api = FakeApi(
        deploys=[
            run(104, "Deploy staging v0.0.5", "failure", 10, 15),
            run(105, "Deploy staging v0.0.5", "failure", 20, 25),
            run(106, "Deploy staging v0.0.5", "success", 40, 45),
        ],
        ci=[run(150, "ci-required", "success", 0, 5, "v0.0.5")],
        jobs={105: [job("request", "Confirm")]},
    )
    read_releases(REPO, api, next_tag=None)
    assert api.jobs_read() == [105], "the older failed deploy 104 names no step that failure() reads"


def test_a_deploy_title_of_another_shape_is_ignored() -> None:
    api = FakeApi(
        deploys=[
            run(1, "Deploy staging latest", "failure", 0, 1),
            run(2, "Deploy production v0.0.5", "failure", 0, 1),
            run(3, "Deploy prod v0.0.5", "failure", 0, 1),
        ],
    )
    assert read_releases(REPO, api, next_tag=None) == ()


def test_an_unexpected_runs_payload_fails_closed() -> None:
    with pytest.raises(ValueError, match="unexpected workflow runs payload"):
        read_releases(REPO, lambda path: {"total_count": 0}, next_tag=None)


def test_a_run_without_an_integer_id_fails_closed() -> None:
    api = FakeApi(deploys=[{**run(1, "Deploy staging v0.0.5", "success", 0, 1), "id": "one"}])
    with pytest.raises(ValueError, match="integer id"):
        read_releases(REPO, api, next_tag=None)


def test_a_rerun_in_flight_adds_the_failed_attempt_that_it_hides() -> None:
    """A re-run keeps the run id. The run list shows run 70 in flight and hides the failed attempt 1.
    The reader reads attempt 1, so the rerun does not lift a stop."""
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", None, 5, 12, attempt=2)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
        attempts={(70, 1): run(70, "Deploy staging v0.0.6", "failure", 5, 9)},
        jobs={(70, 1): [job("request", "Dispatch")]},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert [d.conclusion for d in release.deploys] == ["", "failure"]
    failure = release.failure()
    assert failure is not None and failure.step == "request::Dispatch"


def test_a_rerun_that_is_in_flight_after_a_green_first_attempt_adds_nothing() -> None:
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", None, 5, 12, attempt=2)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
        attempts={(70, 1): run(70, "Deploy staging v0.0.6", "success", 5, 9)},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert [d.conclusion for d in release.deploys] == [""]


def test_the_step_that_failed_first_names_the_cause_not_a_probe_that_ran_after_it() -> None:
    """A probe with `if: !cancelled()` runs after a failed walk and fails with it. Its name sorts
    before the walk step, but the walk step failed first."""
    walk = {
        "name": "walk",
        "conclusion": "failure",
        "steps": [
            {"name": "Probe the MCP credential gate", "conclusion": "failure", "completed_at": stamp(20)},
            {"name": "Walk the deployed surface", "conclusion": "failure", "completed_at": stamp(10)},
        ],
    }
    assert JobReader(REPO, FakeApi(jobs={7: [walk]})).failed_step(7) == "walk::Walk the deployed surface"


def test_steps_that_failed_at_the_same_time_get_the_same_label_in_every_payload_order() -> None:
    """Two shards of one job fail in the same second. GitHub lists them in a different order on
    different reads, and the same fault must get the same label each time."""
    shard0 = {
        "name": "test-core (0)",
        "conclusion": "failure",
        "steps": [{"name": "Run tests", "conclusion": "failure", "completed_at": stamp(5)}],
    }
    shard1 = {
        "name": "test-core (1)",
        "conclusion": "failure",
        "steps": [{"name": "Run tests", "conclusion": "failure", "completed_at": stamp(5)}],
    }
    first = JobReader(REPO, FakeApi(jobs={7: [shard0, shard1]})).failed_step(7)
    second = JobReader(REPO, FakeApi(jobs={7: [shard1, shard0]})).failed_step(7)
    assert first == second == "test-core (0)::Run tests"


def test_a_step_with_a_time_sorts_before_a_step_with_none() -> None:
    steps = [
        {"name": "Untimed", "conclusion": "failure"},
        {"name": "Timed", "conclusion": "failure", "completed_at": stamp(5)},
    ]
    jobs = [{"name": "request", "conclusion": "failure", "steps": steps}]
    assert JobReader(REPO, FakeApi(jobs={7: jobs})).failed_step(7) == "request::Timed"


def test_steps_with_no_time_follow_the_order_of_their_labels() -> None:
    steps = [{"name": "Second", "conclusion": "failure"}, {"name": "First", "conclusion": "failure"}]
    jobs = [{"name": "request", "conclusion": "failure", "steps": steps}]
    assert JobReader(REPO, FakeApi(jobs={7: jobs})).failed_step(7) == "request::First"


def slow_history(*, anchor_tag: str, runs: list[dict], ci: list[dict], jobs: dict) -> FakeApi:
    return FakeApi(
        deploys=[r for r in runs if r["display_title"].startswith("Deploy")],
        walks=[r for r in runs if r["display_title"].startswith("Walk")],
        ci=ci,
        jobs={**jobs, 9001: walk_jobs("success")},
    )


def test_a_release_cut_between_two_failing_releases_breaks_their_run_even_if_it_ended_before_the_anchor() -> None:
    """v0.0.2 and v0.0.4 fail at the same step. v0.0.3 sits between them in cut order, failed its CI
    at minute 30, and ended before the walk at minute 100 that starts the window. The rule needs
    v0.0.3: it resets the run, and no stop follows."""
    api = FakeApi(
        deploys=[
            run(11, "Deploy staging v0.0.1", "success", 2, 90),
            run(12, "Deploy staging v0.0.2", "failure", 11, 120),
            run(14, "Deploy staging v0.0.4", "failure", 141, 150),
        ],
        walks=[run(9001, "Walk Deploy staging v0.0.1", "success", 92, 100)],
        ci=[
            run(21, "ci-required", "success", 1, 2, "v0.0.1"),
            run(22, "ci-required", "success", 9, 10, "v0.0.2"),
            run(23, "ci-required", "failure", 28, 30, "v0.0.3"),
            run(24, "ci-required", "success", 140, 141, "v0.0.4"),
        ],
        jobs={
            9001: walk_jobs("success"),
            12: [job("request", "Dispatch")],
            14: [job("request", "Dispatch")],
            23: [job("images_release / plan", "Select available images")],
        },
    )
    releases = read_releases(REPO, api, next_tag="v0.0.5")
    assert [r.tag for r in releases] == ["v0.0.1", "v0.0.2", "v0.0.3", "v0.0.4"]
    assert staging_red(releases) is None


def test_a_release_cut_last_that_ended_before_the_anchor_is_read() -> None:
    """v0.0.1 and v0.0.2 fail at the same step after the walk of v0.0.3. v0.0.4 was cut last, ended
    before the walk, and its walk failed. It is the newest release and it has no passing walk, so it
    decides whether the line is open."""
    api = FakeApi(
        deploys=[
            run(11, "Deploy staging v0.0.1", "failure", 2, 110),
            run(12, "Deploy staging v0.0.2", "failure", 3, 120),
            run(13, "Deploy staging v0.0.3", "success", 4, 95),
            run(14, "Deploy staging v0.0.4", "success", 141, 143),
        ],
        walks=[
            run(9001, "Walk Deploy staging v0.0.3", "success", 96, 100),
            run(34, "Walk Deploy staging v0.0.4", "failure", 144, 146),
        ],
        ci=[
            run(21, "ci-required", "success", 1, 2, "v0.0.1"),
            run(22, "ci-required", "success", 2, 3, "v0.0.2"),
            run(23, "ci-required", "success", 3, 4, "v0.0.3"),
            run(24, "ci-required", "success", 140, 141, "v0.0.4"),
        ],
        jobs={
            9001: walk_jobs("success"),
            11: [job("request", "Dispatch")],
            12: [job("request", "Dispatch")],
            34: [job("walk", "Walk the deployed surface")],
        },
    )
    releases = read_releases(REPO, api, next_tag=None)
    assert [r.tag for r in releases] == ["v0.0.1", "v0.0.2", "v0.0.3", "v0.0.4"]
    red = staging_red(releases)
    assert red is not None and red.since == "v0.0.1"


def test_a_cancelled_rerun_still_shows_the_failed_attempt_before_it() -> None:
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", "cancelled", 5, 12, attempt=2)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
        attempts={(70, 1): run(70, "Deploy staging v0.0.6", "failure", 5, 9)},
        jobs={(70, 1): [job("request", "Dispatch")]},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    failure = release.failure()
    assert failure is not None and failure.step == "request::Dispatch"


def test_a_rerun_of_a_green_walk_that_is_in_flight_keeps_the_pass() -> None:
    """The operator re-runs the walk of v0.0.6. While attempt 2 runs, the list shows it in flight.
    Attempt 1 passed, and the pass stays."""
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.6", "success", 5, 9)],
        walks=[run(71, "Walk Deploy staging v0.0.6", None, 10, 30, attempt=2)],
        ci=[run(52, "ci-required", "success", 1, 4, "v0.0.6")],
        attempts={(71, 1): run(71, "Walk Deploy staging v0.0.6", "success", 10, 14)},
        jobs={(71, 1): walk_jobs("success")},
    )
    (release,) = read_releases(REPO, api, next_tag=None)
    assert release.walked_green


def test_a_startup_failure_push_of_a_deleted_tag_is_read() -> None:
    api = FakeApi(
        deploys=[run(70, "Deploy staging v0.0.7", "success", 50, 55)],
        ci=[
            run(51, "ci-required", "startup_failure", 30, 31, "v0.0.6"),
            run(52, "ci-required", "success", 40, 41, "v0.0.7"),
        ],
    )
    tags = [r.tag for r in read_releases(REPO, api, next_tag=None)]
    assert "v0.0.6" in tags


def test_the_next_tag_is_queried_even_when_nothing_else_names_it() -> None:
    api = green_history(last_green=12, tags=14)
    read_releases(REPO, api, next_tag="v0.0.15")
    assert any("branch=v0.0.15" in call for call in api.calls)


def test_a_run_that_changed_at_the_instant_of_the_walk_is_not_after_it() -> None:
    """An old tag with a run at the exact time of the anchor walk is not part of the window. If it
    were, the window would reach down to v0.0.5."""
    api = green_history(last_green=12, tags=14)
    anchor = next(r for r in api.walks if r["id"] == 2012)["updated_at"]
    api.deploys.append({**run(1999, "Deploy staging v0.0.5", "success", 0, 0), "updated_at": anchor})
    tags = [r.tag for r in read_releases(REPO, api, next_tag=None)]
    assert tags[0] == "v0.0.12"


def test_a_page_of_other_deploy_types_is_no_title_drift_and_a_page_with_a_changed_name_is() -> None:
    other = FakeApi(deploys=[run(1, "Deploy preview/tag v0.0.5", "success", 0, 1)])
    assert read_releases(REPO, other, next_tag=None) == ()
    renamed = FakeApi(deploys=[run(1, "Release to staging: v0.0.5", "success", 0, 1)])
    with pytest.raises(ValueError, match="run-name changed"):
        read_releases(REPO, renamed, next_tag=None)


def test_a_run_name_that_matches_no_run_raises_instead_of_reading_an_empty_history() -> None:
    api = FakeApi(deploys=[run(1, "Staging deploy of v0.0.5", "failure", 0, 1)])
    with pytest.raises(ValueError, match="run-name changed"):
        read_releases(REPO, api, next_tag=None)
    api = FakeApi(walks=[run(1, "Walk of v0.0.5", "success", 0, 1)])
    with pytest.raises(ValueError, match="run-name changed"):
        read_releases(REPO, api, next_tag=None)


def test_a_page_with_only_prod_runs_is_no_title_drift() -> None:
    api = FakeApi(deploys=[run(1, "Deploy prod v0.0.5", "success", 0, 1)])
    assert read_releases(REPO, api, next_tag=None) == ()
