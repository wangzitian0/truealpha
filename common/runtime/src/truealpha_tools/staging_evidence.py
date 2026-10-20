"""The staging evidence that lets `cut_release.sh --prod` promote a tag (#1412 R1).

Before #1412, `cut_release.sh vX.Y.Z --prod` on an existing tag deployed staging again, waited
for that walk, and then deployed production. Staging succeeded at the first try in 0.74 of the
runs and production in 0.68, so a production attempt succeeded at the first try about half the
time. Production now promotes the tag that staging already verified. This module reads that
verification from GitHub. It never deploys and never dispatches.

Three facts must hold for the commit that the tag points at now:

1. The tag's own `ci-required` push run at that commit is green. Its creation time is the
   anchor: GitHub created the run after the tag was pushed at that commit.
2. A successful `Deploy staging <tag>` run started after the anchor. `deploy-release.yml` runs
   are `workflow_dispatch` runs from main, so their `head_sha` is a main commit, not the tag's
   commit. A run resolves the tag when it starts. A run that started before the anchor deployed
   what the tag named then: `--auto` deletes a tag whose CI never went green (#940), and a later
   release can use the number again at another commit.
3. The newest `Walk Deploy staging <tag>` run created after that deploy run completed has a
   successful walk step. This is the rule of `walk_evidence.find_walk`: a skipped walk step is
   not a walk. The walk does not read which release staging serves, so no staging deploy of
   another tag may run between the end of the deploy and the end of the walk.

Stdlib only. `cut_release.sh` runs this on a bare `python3`.

Usage:
  python3 tools/staging_evidence.py --release v0.2.23 --release-sha <40-character SHA>

Exit 0: prints `<tag ci-required run id> <staging deploy run id> <walk run id>` on stdout.
Exit 1: a fact is missing. The reason and the command that produces the fact go to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from truealpha_tools.walk_evidence import RUNS_PATH, MissingWalkEvidence, find_walk

REPO = "wangzitian0/truealpha"
CI_RUNS = f"/repos/{REPO}/actions/workflows/ci-required.yml/runs"
DEPLOY_RUNS = f"/repos/{REPO}/actions/workflows/deploy-release.yml/runs"
WALK_RUNS = f"/repos/{REPO}/actions/workflows/walk-release.yml/runs"
#: A deploy-release.yml job stops after 50 minutes. A staging deploy of another tag that overlaps
#: the walk window therefore started less than this long before the anchor.
LOOKBACK = timedelta(hours=1)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RELEASE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

Run = dict[str, object]
RunLister = Callable[[str], list[Run]]
GhApi = Callable[[str], str]


class NotPromotable(RuntimeError):
    """A fact that the promotion needs is missing. The message names the fact and the fix."""


@dataclass(frozen=True)
class StagingEvidence:
    tag_run_id: int
    deploy_run_id: int
    walk_run_id: int


def _gh(arguments: Sequence[str]) -> str:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise NotPromotable(f"gh {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def list_runs(path: str) -> list[Run]:
    """Every run of a paginated runs query, one JSON object for each output line."""
    output = _gh(["api", path, "--paginate", "--jq", ".workflow_runs[]"])
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def gh_api(path: str) -> str:
    return _gh(["api", path])


def _time(stamp: object) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _started(run: Run) -> datetime:
    """The start of the newest attempt. A re-run keeps `created_at` and moves `run_started_at`."""
    return _time(run.get("run_started_at") or run["created_at"])


def _query_time(stamp: datetime) -> str:
    # `>=` URL-encoded, the form the runs API accepts for `created`.
    return "%3E%3D" + stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _redeploy_hint(release: str) -> str:
    return f'tools/cut_release.sh {release} --message "..." --redeploy'


def _rewalk_hint(release: str) -> str:
    return f"gh workflow run walk-release.yml -f deploy_type=staging -f version_ref={release}"


def _anchor(release: str, release_sha: str, runs: RunLister) -> Run:
    """Fact 1: the newest ci-required push run of the tag at its commit, and it is green.

    The API filters by branch too. The code checks the branch again, so that a wrong filter
    cannot admit the run of another tag.
    """
    short = release_sha[:8]
    tag_runs = [
        run
        for run in runs(f"{CI_RUNS}?event=push&branch={release}&per_page=100")
        if run.get("head_branch") == release and run.get("event") == "push" and run.get("head_sha") == release_sha
    ]
    if not tag_runs:
        raise NotPromotable(
            f"no ci-required push run of {release} at {short} exists, so nothing shows when {release} "
            f"began to name {short}. A staging deploy cannot be bound to this commit. "
            f"Release a new number to staging, or deploy this tag to staging again: {_redeploy_hint(release)}"
        )
    anchor = max(tag_runs, key=lambda run: _time(run["created_at"]))
    if anchor.get("conclusion") != "success":
        raise NotPromotable(
            f"the newest ci-required run of {release} at {short} (run {anchor.get('id')}) concluded "
            f"{anchor.get('conclusion')!r}. A tag whose own CI is not green is not a release."
        )
    return anchor


def _is_successful_deploy(run: Run, title: str, anchored_at: datetime) -> bool:
    return (
        run.get("display_title") == title
        and run.get("event") == "workflow_dispatch"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and _started(run) >= anchored_at
    )


def _staging_deploy(release: str, release_sha: str, deploys: list[Run], anchor: Run) -> Run:
    """Fact 2: the newest successful staging deploy of the tag that started after the anchor."""
    title = f"Deploy staging {release}"
    anchored_at = _time(anchor["created_at"])
    staged = [run for run in deploys if _is_successful_deploy(run, title, anchored_at)]
    if not staged:
        raise NotPromotable(
            f"no successful {title!r} run started after run {anchor.get('id')}, the ci-required run "
            f"that shows {release} at {release_sha[:8]} (created {anchor['created_at']}). A run of that name "
            f"that started earlier deployed what {release} named then. Deploy staging and walk it: "
            f"{_redeploy_hint(release)}"
        )
    return max(staged, key=_started)


def _walk(release: str, deploy: Run, walk_runs: list[Run], api: GhApi) -> Run:
    """Fact 3a: the newest walk created after the deploy completed has a successful walk step.

    `find_walk` holds the walk rule. This function only narrows the runs that it reads to the
    runs created after this deploy completed.
    """
    deployed_at = _time(deploy["updated_at"])
    walks = [run for run in walk_runs if _time(run["created_at"]) >= deployed_at]

    def scoped_api(path: str) -> str:
        return json.dumps({"workflow_runs": walks}) if path == RUNS_PATH else api(path)

    try:
        walk = find_walk("staging", release, gh_api=scoped_api)
    except MissingWalkEvidence as exc:
        raise NotPromotable(
            f"no 'Walk Deploy staging {release}' run with a walk step was created after deploy run "
            f"{deploy['id']} completed ({deploy['updated_at']}): {exc}. Walk it again: {_rewalk_hint(release)}"
        ) from exc
    if walk["conclusion"] != "success":
        raise NotPromotable(
            f"the newest walk of {release} after deploy run {deploy['id']} is run {walk['run_id']}, and its "
            f"walk step concluded {walk['conclusion']!r}. Walk it again: {_rewalk_hint(release)}"
        )
    return next(run for run in walks if run.get("id") == walk["run_id"])


def _overlaps(other: Run, deployed_at: datetime, walk_ended: datetime) -> bool:
    """A run that has not completed still changes staging, so it has no end yet."""
    ended = _time(other["updated_at"]) if other.get("status") == "completed" else None
    return _started(other) < walk_ended and (ended is None or ended > deployed_at)


def _check_no_other_deploy(release: str, deploys: list[Run], deploy: Run, walk: Run) -> None:
    """Fact 3b: no staging deploy of another tag ran between the end of the deploy and the end of the walk."""
    deployed_at = _time(deploy["updated_at"])
    walk_ended = _time(walk.get("updated_at") or walk["created_at"])
    for other in deploys:
        title = str(other.get("display_title", ""))
        if not title.startswith("Deploy staging ") or title == f"Deploy staging {release}":
            continue
        if _overlaps(other, deployed_at, walk_ended):
            raise NotPromotable(
                f"{title!r} (run {other.get('id')}) ran between the end of deploy run {deploy['id']} and the "
                f"end of walk run {walk['id']}, so that walk can have walked another release. "
                f"Deploy staging and walk it again: {_redeploy_hint(release)}"
            )


def find_staging_evidence(
    release: str,
    release_sha: str,
    *,
    runs: RunLister = list_runs,
    api: GhApi = gh_api,
) -> StagingEvidence:
    """The three facts of the module docstring, or `NotPromotable` for the first missing one."""
    if not _RELEASE.match(release) or not _SHA.match(release_sha):
        raise NotPromotable(f"needs a vX.Y.Z release and a 40-character SHA, not {release!r} and {release_sha!r}")
    anchor = _anchor(release, release_sha, runs)
    since = _query_time(_time(anchor["created_at"]) - LOOKBACK)
    deploys = runs(f"{DEPLOY_RUNS}?created={since}&per_page=100")
    deploy = _staging_deploy(release, release_sha, deploys, anchor)
    walk = _walk(release, deploy, runs(f"{WALK_RUNS}?created={since}&per_page=100"), api)
    _check_no_other_deploy(release, deploys, deploy, walk)
    return StagingEvidence(
        tag_run_id=int(str(anchor["id"])), deploy_run_id=int(str(deploy["id"])), walk_run_id=int(str(walk["id"]))
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", required=True)
    parser.add_argument("--release-sha", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = find_staging_evidence(args.release, args.release_sha)
    except NotPromotable as exc:
        print(f"staging evidence missing: {exc}", file=sys.stderr)
        return 1
    print(
        f"staging verified {args.release} at {args.release_sha[:8]}: tag ci-required run {evidence.tag_run_id}, "
        f"staging deploy run {evidence.deploy_run_id}, walk run {evidence.walk_run_id}",
        file=sys.stderr,
    )
    print(f"{evidence.tag_run_id} {evidence.deploy_run_id} {evidence.walk_run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
