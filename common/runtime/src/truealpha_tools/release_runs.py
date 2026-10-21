"""The words that the delivery report and the staging release train share (#1468).

Both tools read the same GitHub Actions runs of a release: the `ci-required` run of a tag push,
the `deploy-release` run and the `walk-release` run. The facts below have one definition, here:
the run titles, the workflow files, the tag name, the summary job, the failed conclusions and the
timestamp parser. `delivery_report.py` and the train import them.

Other tools still hold their own copies of some of these facts: `walk_evidence.py`,
`staging_evidence.py`, `nightly_verdicts.py` and the workflow paths of `delivery_report.py`. Move a
fact here when you change it there.

Stdlib only: `auto-release-staging.yml` runs the train with no `uv sync`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

#: Read one GitHub REST path and return the parsed JSON.
GitHubGet = Callable[[str], object]

#: The workflow files of a release.
DEPLOY_WORKFLOW = "deploy-release.yml"
WALK_WORKFLOW = "walk-release.yml"
CI_WORKFLOW = "ci-required.yml"
#: A release tag. The three groups are major, minor and patch.
TAG_RE = re.compile(r"\Av(\d+)\.(\d+)\.(\d+)\Z")
#: The run-name of `deploy-release.yml`, for example "Deploy staging v0.2.30".
DEPLOY_TITLE = re.compile(r"\ADeploy (?P<deploy_type>staging|prod) (?P<release>\S+)\Z")
#: The run-name of `walk-release.yml`, for example "Walk Deploy staging v0.2.30".
WALK_TITLE = re.compile(r"\AWalk Deploy (?P<deploy_type>staging|prod) (?P<release>\S+)\Z")
#: The job of `ci-required` that fails when any other job fails. It names no cause.
SUMMARY_JOB = "ci-required"
#: Run conclusions that count as a failure. A cancelled or skipped run carries no verdict.
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})


def workflow_runs(payload: object) -> list[dict[str, object]]:
    """The `workflow_runs` list of a GitHub runs payload. Raise ValueError for any other shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError(f"unexpected workflow runs payload: {str(payload)[:200]}")
    return [run for run in payload["workflow_runs"] if isinstance(run, dict)]


def tag_key(name: str) -> tuple[int, int, int] | None:
    """The sort key of a release tag, or None when `name` is no release tag."""
    match = TAG_RE.match(name)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def parse_time(value: object) -> datetime | None:
    """A GitHub timestamp as an aware UTC datetime, or None when the value is empty or invalid."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
