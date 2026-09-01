"""Resolve a workflow, a job and a step by name, so tests assert properties.

Test infrastructure, not runtime code — which is why it lives beside the tests
rather than in `truealpha_runtime`. Nothing the application or the deployers run
reads a workflow file; only tests do.

#583. Six test files each opened `.github/workflows/*.yml` and asserted a
substring, every one of them locating the region it cared about by splitting the
file on a step name:

    guard = workflow.split("Walk the deployed surface", 1)[1].split("bun install", 1)[0]

That is fragile in a specific and demonstrated way: when the credential check
moved from inside the walk step into its own probe, the split silently selected
a different region and the assertion failed for a reason unrelated to the
property. Its neighbours had the same fragility and nobody noticed, because each
file had invented the technique separately.

Resolving by name raises when the step is gone, which is itself the assertion a
renamed step should trigger — "the step this test is about no longer exists" is
a better failure than an assertion over the wrong slice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


class WorkflowContractError(AssertionError):
    """The workflow, job or step a test is about does not exist."""


def load(name: str) -> dict[str, Any]:
    path = WORKFLOWS / name
    if not path.exists():
        raise WorkflowContractError(f"{name} does not exist in {WORKFLOWS}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def source(name: str) -> str:
    """The raw text, for the few properties that are about formatting."""
    path = WORKFLOWS / name
    if not path.exists():
        raise WorkflowContractError(f"{name} does not exist in {WORKFLOWS}")
    return path.read_text(encoding="utf-8")


def job(workflow: str, job_id: str) -> dict[str, Any]:
    jobs = load(workflow).get("jobs", {})
    if job_id not in jobs:
        raise WorkflowContractError(f"{workflow} has no job {job_id!r} (it has {sorted(jobs)})")
    return jobs[job_id]


def steps(workflow: str) -> list[dict[str, Any]]:
    return [step for spec in load(workflow).get("jobs", {}).values() for step in spec.get("steps", [])]


def step(workflow: str, name: str) -> dict[str, Any]:
    """The step with this exact `name`, from any job in the workflow."""
    found = [candidate for candidate in steps(workflow) if candidate.get("name") == name]
    if not found:
        named = [candidate.get("name") for candidate in steps(workflow) if candidate.get("name")]
        raise WorkflowContractError(
            f"{workflow} has no step named {name!r}. It has {named}. A renamed step is a "
            f"contract change: rename it here too, or the property this test protects has "
            f"silently stopped being checked"
        )
    if len(found) > 1:
        raise WorkflowContractError(f"{workflow} has {len(found)} steps named {name!r}")
    return found[0]


def spec_text(spec: dict[str, Any]) -> str:
    """One parsed step rendered as a searchable string.

    Separate from `step_text` because a step name is only unique within a
    workflow until two jobs legitimately share one — ci-web's split gave both
    halves an `Apply DB migrations and roles`, and a scan that must cover every
    step cannot go through a by-name lookup that (correctly) refuses a
    duplicate.
    """
    parts = [str(spec.get("run", "")), str(spec.get("if", "")), str(spec.get("name", ""))]
    parts += [f"{key}={value}" for key, value in (spec.get("env") or {}).items()]
    parts += [str(spec.get("uses", "")), str(spec.get("with", ""))]
    return "\n".join(part for part in parts if part)


def step_text(workflow: str, name: str) -> str:
    """One named step, rendered by `spec_text` — its `run`, `if`, `name`, `env`,
    `uses` and `with`, as one searchable string.

    The unit an assertion is usually about: "this step checks X", "this step is
    gated on Y". Built from the parsed step, so it cannot pick up a neighbour's
    text the way a file split can. Note the `name` is part of the rendering: an
    assertion looking for a fragment that also appears in a step's title will
    match it (review).
    """
    return spec_text(step(workflow, name))


def triggers(workflow: str) -> dict[str, Any]:
    """The `on:` block. PyYAML parses a bare `on` as the boolean True."""
    parsed = load(workflow)
    return parsed.get("on") if "on" in parsed else parsed.get(True, {})
