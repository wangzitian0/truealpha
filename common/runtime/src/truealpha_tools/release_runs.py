"""The words that the delivery report and the staging release train share (#1468).

Both tools read the same GitHub Actions runs of a release: the `ci-required` run of a tag push,
the `deploy-release` run and the `walk-release` run. Each fact about those runs has one
definition, here. The delivery report is a dashboard and the release train is a gate. Neither
defines the title of a run, the name of a tag, or the name of the summary job on its own.

Stdlib only: `auto-release-staging.yml` runs the train with no `uv sync`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

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
