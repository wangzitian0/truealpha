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
  python tools/walk_evidence.py --environment prod --release v0.0.20

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


def find_walk(environment: str, release: str, *, gh_api: GhApi = _gh_api) -> dict[str, object]:
    """The walk step of this repo's own `Deploy <environment> <release>` run."""
    title = f"Deploy {environment} {release}"
    runs = json.loads(gh_api(RUNS_PATH))
    matching = [
        run
        for run in runs.get("workflow_runs", [])
        if run.get("display_title") == title and run.get("event") == "workflow_dispatch"
    ]
    if not matching:
        raise MissingWalkEvidence(
            f"no {title!r} run in {WINDOW}, so nothing recent has walked the surface "
            f"{environment} is serving. Either the release predates the post-release walk, or "
            f"{environment} is serving something this repository did not release"
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


def check_walk_evidence(environment: str, release: str, *, gh_api: GhApi = _gh_api) -> int:
    try:
        found = find_walk(environment, release, gh_api=gh_api)
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
    parser.add_argument("--environment", required=True)
    parser.add_argument("--release", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return check_walk_evidence(args.environment, args.release)


if __name__ == "__main__":
    raise SystemExit(main())
