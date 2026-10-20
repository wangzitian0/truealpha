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

import re
from collections.abc import Iterable
from functools import cache
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


def job_step(workflow: str, job_id: str, name: str) -> dict[str, Any]:
    """The step with this exact `name` inside ONE job.

    `step` resolves across the whole workflow and (correctly) refuses a name two jobs
    share. Some names are deliberately shared: ci-web's split gave both halves the same
    migration step and ci-python has three, one per job that needs a schema. Naming the
    job is what makes such a step addressable without going back to a text split.
    """
    candidates = job(workflow, job_id).get("steps", [])
    found = [candidate for candidate in candidates if candidate.get("name") == name]
    if not found:
        named = [candidate.get("name") for candidate in candidates if candidate.get("name")]
        raise WorkflowContractError(
            f"{workflow}:{job_id} has no step named {name!r}. It has {named}. A renamed step is "
            f"a contract change: rename it here too, or the property this test protects has "
            f"silently stopped being checked"
        )
    if len(found) > 1:
        raise WorkflowContractError(f"{workflow}:{job_id} has {len(found)} steps named {name!r}")
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


# --- paths-filter: which lanes a set of changed paths selects (#1412) -----------------
#
# The paths-filter action builds each pattern with `picomatch(pattern, {dot: true})`
# (dorny/paths-filter v3, src/filter.ts, picomatch ^2.3.1). By default it selects a lane
# when SOME pattern matches SOME changed file. `predicate-quantifier: every` changes
# "some pattern" to "all patterns". The functions below follow those rules for the glob
# syntax the filters use.
#
# A test that matched paths with `startswith` or `fnmatch` would pass for the wrong
# reason: `fnmatch` lets `*` cross `/`, and neither knows that `a/**` also matches `a`.
# #1168 wrote `!apps/app-web/tests/**` entries that never excluded anything, and a
# rules-only PR (#1409) built all three images. The matcher is small on purpose. It
# refuses syntax it does not implement, so an unsupported pattern fails a test instead
# of being guessed.

#: picomatch reads these as character classes, braces, extglobs and escapes.
_UNSUPPORTED_GLOB_CHARACTERS = frozenset("[]{}()\\")


@cache
def _segment_regex(segment: str) -> re.Pattern[str]:
    """One path segment: `*` is any run of characters but `/`, `?` is one such character.

    With `dot: true` both match a leading dot, so `*` matches `.env.example`.
    """
    pieces = []
    for character in segment:
        if character == "*":
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(character))
    return re.compile("".join(pieces))


def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # A whole-segment `**` is zero or more segments: `a/**` matches `a`, `a/b`, `a/b/c`.
        if not rest:
            return True
        return any(_match_segments(rest, path[skip:]) for skip in range(len(path) + 1))
    if not path:
        return False
    return _segment_regex(head).fullmatch(path[0]) is not None and _match_segments(rest, path[1:])


def glob_matches(pattern: str, path: str) -> bool:
    """True when `picomatch(pattern, {dot: true})(path)` is true, for the syntax the filters use.

    Supported: `*`, `?`, a whole-segment `**`, and one leading `!` that negates the whole
    pattern (picomatch returns TRUE for every path the pattern does not match).
    Anything else raises ValueError. So does a path that git never reports: empty, rooted
    or with a trailing `/` (picomatch treats a trailing `/` in ways this matcher does not).
    Checked once against picomatch 2.3.1 itself: no difference on more than 70,000 pairs of
    pattern and path (every pattern of the filters before and after #1412, the tracked
    files of the repository, and dot-path cases). PICOMATCH_GOLDEN in test_ci_workflows.py
    keeps a sample of those answers.
    """
    if not path or path.startswith("/") or path.endswith("/"):
        raise ValueError(f"unsupported path {path!r}: expected a file name as git reports it")
    negated = pattern.startswith("!")
    body = pattern[1:] if negated else pattern
    if not body or body.startswith(("!", "/", "./")):
        raise ValueError(f"unsupported glob {pattern!r}: empty, repeated negation or rooted")
    if _UNSUPPORTED_GLOB_CHARACTERS & set(body) or "!" in body:
        raise ValueError(f"unsupported glob {pattern!r}: only *, ? and ** are implemented")
    segments = tuple(body.split("/"))
    if any("**" in segment and segment != "**" for segment in segments):
        raise ValueError(f"unsupported glob {pattern!r}: ** must be a whole path segment")
    return _match_segments(segments, tuple(path.split("/"))) is not negated


def paths_filters(workflow: str, job_id: str = "changes") -> list[tuple[str, dict[str, list[str]]]]:
    """Each `dorny/paths-filter` step of one job: its quantifier and its `{lane: [pattern]}`."""
    found = []
    for spec in job(workflow, job_id).get("steps", []):
        if not str(spec.get("uses", "")).startswith("dorny/paths-filter@"):
            continue
        options = spec.get("with") or {}
        quantifier = str(options.get("predicate-quantifier", "some"))
        if quantifier not in {"some", "every"}:
            raise WorkflowContractError(f"{workflow}:{job_id} sets predicate-quantifier {quantifier!r}")
        lanes: dict[str, list[str]] = {}
        for lane, entries in yaml.safe_load(options["filters"]).items():
            patterns: list[str] = []
            for entry in entries if isinstance(entries, list) else [entries]:
                if not isinstance(entry, str):
                    raise WorkflowContractError(f"{workflow}:{job_id} lane {lane!r} has a non-string entry {entry!r}")
                patterns.append(entry)
            lanes[lane] = patterns
        found.append((quantifier, lanes))
    if not found:
        raise WorkflowContractError(f"{workflow}:{job_id} has no dorny/paths-filter step")
    return found


def selected_lanes(workflow: str, paths: Iterable[str], job_id: str = "changes") -> set[str]:
    """The lanes the paths-filter action would set to `true` for these changed paths.

    Models a pull_request run: the step's own `if` skips only tags and the merge queue.
    """
    changed = list(paths)
    selected: set[str] = set()
    for quantifier, lanes in paths_filters(workflow, job_id):
        combine = all if quantifier == "every" else any
        for lane, patterns in lanes.items():
            if any(combine(glob_matches(pattern, path) for pattern in patterns) for path in changed):
                selected.add(lane)
    return selected
