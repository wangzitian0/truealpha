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

Usage:
  python tools/walk_evidence.py --deploy-type prod --environment production --release v0.0.20

Exit codes:
  0 - the deployed release has a successful surface walk
  1 - it does not, or the evidence cannot be found
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence

from infra2_sdk.deploy_health import HttpGet, default_http_get
from truealpha_runtime.deployed_release import ReleaseIdentityError, read_deployed_release

WALK_STEP_NAME = "Walk the deployed surface"
# Query the release workflow's OWN runs, not every run in the repository: on a
# busy repo the release run falls off a 100-item all-workflows page within days,
# and "no such run exists" would then be false rather than merely unhelpful.
RUNS_PATH = "/repos/wangzitian0/truealpha/actions/workflows/deploy-release.yml/runs?per_page=100"
WINDOW = "the last 100 deploy-release runs"
GhApi = Callable[[str], str]


class MissingWalkEvidence(RuntimeError):
    """No successful surface walk is recorded for the deployed release."""


def _gh_api(path: str) -> str:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MissingWalkEvidence(f"gh api {path} failed: {result.stderr.strip()}")
    return result.stdout


def find_walk(deploy_type: str, release: str, *, environment: str = "", gh_api: GhApi = _gh_api) -> dict[str, object]:
    """The walk step of this repo's own `Deploy <deploy_type> <release>` run.

    `deploy_type` and `environment` are NOT interchangeable and were conflated
    in the first version of this file: deploy-release.yml's run-name is built
    from `inputs.deploy_type` ("prod", "staging"), while the freshness matrix
    names environments for humans ("production"). Looking for
    "Deploy production <tag>" would have made this red forever, for a reason
    unrelated to what it guards — the exact defect class it exists to catch, and
    invisible to a manual check that happens to pass the right word (review).
    """
    environment = environment or deploy_type
    title = f"Deploy {deploy_type} {release}"
    runs = json.loads(gh_api(RUNS_PATH))
    matching = [
        run
        for run in runs.get("workflow_runs", [])
        if run.get("display_title") == title and run.get("event") == "workflow_dispatch"
    ]
    if not matching:
        raise MissingWalkEvidence(
            f"no {title!r} run in {WINDOW}, so nothing recent has walked the surface "
            f"{environment} is serving. Either that release is older than the window, or it "
            f"predates the post-release walk, or {environment} is serving something this "
            f"repository did not release — open the deploy-release run list to tell which"
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


def check_walk_evidence(deploy_type: str, release: str, *, environment: str = "", gh_api: GhApi = _gh_api) -> int:
    environment = environment or deploy_type
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
