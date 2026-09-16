"""Fail while the deployed release has never had its surface walked.

#560 (W4). The release run answers "is it deployed". This answers "did anyone
confirm a person can use it" — a STANDING question, because the answer can be
"no" for reasons that have nothing to do with the release run's own outcome
(credentials never configured, the step added after the release, a walk that
failed and was ignored).

Keeping them separate is deliberate and was learned the hard way: making the
release run itself fail on a missing walk blocked every prod release, since prod
requires this repository's own successful "Deploy staging <tag>" run — including
the release that would have carried the fix. Blocking the lane on a setup
deficiency produces exactly the invisible-work outcome #560 exists to prevent.

#855/#860: the walk moved out of `deploy-release.yml`'s own run into its own
deferred workflow, `walk-release.yml` (triggered by `deploy-release.yml`'s
`workflow_run` completion, or by hand as the flake recovery). This file now
reads THAT workflow's runs — same evidence question, same standing-check
shape, different run to look inside.

Usage:
  python tools/walk_evidence.py --deploy-type prod --environment production --release v0.0.20

A release caught mid-flight is not judged mid-flight (2026-09-16, #876): the
environment starts serving a tag minutes before its deploy run ends and its walk
runs, so a check that landed in that window read "no walk for v0.0.72" and filed
an alert for a release that was about to be walked. When the served release's
deploy run or walk run is still running — or its deploy finished moments ago and
the walk has not been created yet — this waits (bounded) and judges the outcome.
A release still in flight after the bound is red: that is its own problem.

Exit codes:
  0 - the deployed release has a successful surface walk
  1 - it does not, or the evidence cannot be found, or it stayed in flight past the bound
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from infra2_sdk.deploy_health import HttpGet, default_http_get
from truealpha_runtime.deployed_release import ReleaseIdentityError, read_deployed_release

WALK_STEP_NAME = "Walk the deployed surface"
# Query the walk workflow's OWN runs, not every run in the repository: on a
# busy repo a given release's run falls off a 100-item all-workflows page
# within days, and "no such run exists" would then be false rather than merely
# unhelpful.
RUNS_PATH = "/repos/wangzitian0/truealpha/actions/workflows/walk-release.yml/runs?per_page=100"
WINDOW = "the last 100 walk-release runs"
DEPLOY_RUNS_PATH = "/repos/wangzitian0/truealpha/actions/workflows/deploy-release.yml/runs?per_page=100"
#: A staging release measured 12-13 min from tag to walked on 2026-09-16, of which the
#: deploy run is ~9 min and the walk ~2 min; the check can land anywhere in that span.
IN_FLIGHT_WAIT = timedelta(minutes=15)
POLL_SECONDS = 30.0
#: workflow_run creates the walk seconds after the deploy run completes; allow for a
#: slow GitHub before calling a completed-green deploy with no walk "unwalked".
WALK_START_GRACE = timedelta(minutes=3)
GhApi = Callable[[str], str]


class MissingWalkEvidence(RuntimeError):
    """No successful surface walk is recorded for the deployed release."""


def _gh_api(path: str) -> str:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MissingWalkEvidence(f"gh api {path} failed: {result.stderr.strip()}")
    return result.stdout


def find_walk(deploy_type: str, release: str, *, environment: str = "", gh_api: GhApi = _gh_api) -> dict[str, object]:
    """The walk step of this repo's own `walk-release.yml` run for `<deploy_type> <release>`.

    `deploy_type` and `environment` are NOT interchangeable and were conflated
    in the first version of this file: deploy-release.yml's run-name (which
    walk-release.yml's own run-name echoes, #855/#860) is built from
    `inputs.deploy_type` ("prod", "staging"), while the freshness matrix names
    environments for humans ("production"). Looking for "Deploy production
    <tag>" would have made this red forever, for a reason unrelated to what it
    guards — the exact defect class it exists to catch, and invisible to a
    manual check that happens to pass the right word (review).

    A walk run reaches this title either automatically (`workflow_run`, fired
    by deploy-release.yml's own completion) or by a manual re-run
    (`workflow_dispatch`, the #811 flake recovery) — both are real evidence,
    so neither `event` value is excluded.
    """
    environment = environment or deploy_type
    title = f"Walk Deploy {deploy_type} {release}"
    runs = json.loads(gh_api(RUNS_PATH))
    matching = [
        run
        for run in runs.get("workflow_runs", [])
        if run.get("display_title") == title and run.get("event") in ("workflow_run", "workflow_dispatch")
    ]
    if not matching:
        raise MissingWalkEvidence(
            f"no {title!r} run in {WINDOW}, so nothing recent has walked the surface "
            f"{environment} is serving. Either that release is older than the window, or it "
            f"predates the post-release walk, or {environment} is serving something this "
            f"repository did not release — open the walk-release run list to tell which"
        )
    newest = max(matching, key=lambda run: str(run.get("created_at", "")))
    jobs = json.loads(gh_api(f"/repos/wangzitian0/truealpha/actions/runs/{newest['id']}/jobs"))
    for job in jobs.get("jobs", []):
        for step in job.get("steps", []):
            if step.get("name") == WALK_STEP_NAME:
                return {"run_id": newest["id"], "conclusion": step.get("conclusion")}
    raise MissingWalkEvidence(
        f"{title!r} (run {newest['id']}) has no {WALK_STEP_NAME!r} step — it predates the "
        f"post-release walk, so its surface was never verified"
    )


def _utc(stamp: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def in_flight(deploy_type: str, release: str, *, gh_api: GhApi, now: datetime) -> str | None:
    """What of this release is still running, or None once there is an outcome to judge.

    Three in-flight shapes, each measured rather than assumed: the deploy run for the
    release has not completed; a walk run for it has not completed; or the newest deploy
    run completed green less than `WALK_START_GRACE` ago and no walk run has been created
    since (the `workflow_run` hand-off). A deploy that finished non-green is an outcome —
    walk-release never runs for it — so it is judged at once, as unwalked.
    """
    deploy_title = f"Deploy {deploy_type} {release}"
    walk_title = f"Walk {deploy_title}"
    deploys = [
        run
        for run in json.loads(gh_api(DEPLOY_RUNS_PATH)).get("workflow_runs", [])
        if run.get("display_title") == deploy_title
    ]
    walks = [
        run for run in json.loads(gh_api(RUNS_PATH)).get("workflow_runs", []) if run.get("display_title") == walk_title
    ]
    for run in (*deploys, *walks):
        if run.get("status") != "completed":
            return f"{run.get('display_title')!r} (run {run.get('id')}) is {run.get('status')}"
    if deploys:
        newest = max(deploys, key=lambda run: str(run.get("updated_at", "")))
        finished = _utc(newest.get("updated_at"))
        walked_after = any(str(walk.get("created_at", "")) >= str(newest.get("updated_at", "")) for walk in walks)
        if (
            newest.get("conclusion") == "success"
            and finished is not None
            and not walked_after
            and now - finished < WALK_START_GRACE
        ):
            return f"{deploy_title!r} (run {newest.get('id')}) finished at {newest.get('updated_at')} and its walk has not started yet"
    return None


def wait_until_settled(
    deploy_type: str,
    release: str,
    *,
    gh_api: GhApi,
    wait: timedelta = IN_FLIGHT_WAIT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str | None:
    """Poll `in_flight` until the release has an outcome; the still-running description if the bound ran out."""
    deadline = clock() + wait
    while True:
        pending = in_flight(deploy_type, release, gh_api=gh_api, now=clock())
        if pending is None:
            return None
        if clock() >= deadline:
            return pending
        print(f"waiting for the release to settle: {pending}", flush=True)
        sleep(POLL_SECONDS)


def check_walk_evidence(
    deploy_type: str,
    release: str,
    *,
    environment: str = "",
    gh_api: GhApi = _gh_api,
    wait: timedelta = IN_FLIGHT_WAIT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    environment = environment or deploy_type
    try:
        pending = wait_until_settled(deploy_type, release, gh_api=gh_api, wait=wait, sleep=sleep, clock=clock)
    except MissingWalkEvidence as exc:
        print(f"walk evidence missing: {exc}", file=sys.stderr)
        return 1
    if pending is not None:
        print(
            f"walk evidence missing: {environment} serves {release}, and after {int(wait.total_seconds() // 60)} min "
            f"the release is still in flight — {pending}. A release that does not settle in that long is stuck",
            file=sys.stderr,
        )
        return 1
    try:
        found = find_walk(deploy_type, release, environment=environment, gh_api=gh_api)
    except MissingWalkEvidence as exc:
        print(f"walk evidence missing: {exc}", file=sys.stderr)
        return 1
    if found["conclusion"] != "success":
        print(
            f"walk evidence missing: {environment} serves {release}, but the surface walk in run "
            f"{found['run_id']} concluded {found['conclusion']!r}. The release is deployed and "
            f"unverified — nothing has confirmed a person can use it (#560)",
            file=sys.stderr,
        )
        return 1
    print(f"{environment} serves {release}, and run {found['run_id']} walked its surface")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # The value deploy-release.yml's run-name is built from, not the human name.
    parser.add_argument("--deploy-type", required=True)
    # #585: `--url` replaces the workflow's `curl | jq -r '.git_sha'`, which was
    # the fourth implementation of this read and the only one that validated
    # nothing — a non-object body made jq print "null", and this tool then
    # reported "no 'Deploy prod null' run", a true sentence about the wrong
    # question. `--release` stays for a manual run against a known tag.
    parser.add_argument("--url", default="")
    parser.add_argument("--release", default="")
    parser.add_argument("--environment", default="")
    return parser


def resolve_release(url: str, release: str, *, http_get: HttpGet | None = None) -> str:
    """The release to judge: an explicit one, or whatever the environment serves."""
    if release:
        return release
    if not url:
        raise MissingWalkEvidence("one of --release or --url is required")
    return read_deployed_release(url, http_get or default_http_get())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        release = resolve_release(args.url, args.release)
    except (MissingWalkEvidence, ReleaseIdentityError) as exc:
        print(f"walk evidence missing: {exc}", file=sys.stderr)
        return 1
    return check_walk_evidence(args.deploy_type, release, environment=args.environment)


if __name__ == "__main__":
    raise SystemExit(main())
