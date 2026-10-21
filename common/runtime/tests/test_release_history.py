"""Tests for `release_history`: the staging releases, read from the GitHub run lists (#1468 F1).

`FakeApi` answers the REST paths that `read_releases` reads. These tests build small histories by
hand. `test_staging_line.py` feeds the real history of 2026-10-13 to 2026-10-21 through the same
reader.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from truealpha_tools import release_history
from truealpha_tools.release_history import JobReader, read_releases, tag_names
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


def run(run_id: int, title: str, conclusion: str | None, start: int, end: int, branch: str = "main") -> dict:
    return {
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
    def __init__(self, *, deploys=(), walks=(), ci=(), failed_pushes=None, jobs=None) -> None:
        self.deploys = list(deploys)
        self.walks = list(walks)
        self.ci = list(ci)
        self.failed_pushes = failed_pushes
        self.jobs = jobs or {}
        self.calls: list[str] = []

    def jobs_read(self) -> list[int]:
        return sorted(int(c.split("/runs/")[1].split("/")[0]) for c in self.calls if "/jobs?" in c)

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if "/actions/runs/" in path and "/jobs?" in path:
            return {"jobs": self.jobs.get(int(path.split("/runs/")[1].split("/")[0]), [])}
        if "workflows/deploy-release.yml/runs" in path:
            return {"workflow_runs": self.deploys}
        if "workflows/walk-release.yml/runs" in path:
            return {"workflow_runs": self.walks}
        if "workflows/ci-required.yml/runs?branch=" in path:
            branch = path.split("branch=")[1].split("&")[0]
            return {"workflow_runs": [r for r in self.ci if r["head_branch"] == branch]}
        if "workflows/ci-required.yml/runs?event=push&status=failure" in path:
            runs = self.ci if self.failed_pushes is None else self.failed_pushes
            return {"workflow_runs": [r for r in runs if r["conclusion"] == "failure"]}
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


def test_failed_step_is_the_first_failed_step_by_name_and_skips_the_summary_job() -> None:
    api = FakeApi(jobs={7: [SUMMARY, job("zeta", "Last"), job("images_release / plan", "Select available images")]})
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


def test_a_failed_push_older_than_the_window_is_not_read() -> None:
    names = tag_names(
        REPO,
        FakeApi(
            failed_pushes=[
                run(1, "ci-required", "failure", 0, 1, "v0.0.1"),
                run(2, "ci-required", "failure", 0, 1, "v0.0.30"),
            ]
        ),
        ["v0.0.20", "v0.0.21"],
        next_tag=None,
    )
    assert names == ["v0.0.20", "v0.0.21", "v0.0.30"]


def test_the_window_keeps_the_newest_tags_and_drops_the_oldest() -> None:
    known = [f"v0.0.{n}" for n in range(1, 21)]
    names = tag_names(REPO, FakeApi(), known, next_tag=None, recent=8)
    assert names == [f"v0.0.{n}" for n in range(13, 21)]


def test_the_next_tag_is_read_once_even_when_it_is_already_known() -> None:
    assert tag_names(REPO, FakeApi(), ["v0.0.5", "v0.0.6"], next_tag="v0.0.6") == ["v0.0.5", "v0.0.6"]
    assert tag_names(REPO, FakeApi(), ["v0.0.5"], next_tag="v0.0.6") == ["v0.0.5", "v0.0.6"]


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
