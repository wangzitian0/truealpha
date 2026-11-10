# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import re; from collections.abc import Iterable; from functools import cache; from pathlib import Path; from typing import Any; import yaml; WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
class WorkflowContractError(AssertionError): """The workflow, job or step a test is about does not exist."""
def load(name: str) -> dict[str, Any]:
    path = WORKFLOWS / name
    if not path.exists(): raise WorkflowContractError(f"{name} does not exist in {WORKFLOWS}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))
def source(name: str) -> str:
    path = WORKFLOWS / name
    if not path.exists(): raise WorkflowContractError(f"{name} does not exist in {WORKFLOWS}")
    return path.read_text(encoding="utf-8")
def job(workflow: str, job_id: str) -> dict[str, Any]:
    jobs = load(workflow).get("jobs", {})
    if job_id not in jobs: raise WorkflowContractError(f"{workflow} has no job {job_id!r} (it has {sorted(jobs)})")
    return jobs[job_id]
def steps(workflow: str) -> list[dict[str, Any]]: return [step for spec in load(workflow).get("jobs", {}).values() for step in spec.get("steps", [])]
def step(workflow: str, name: str) -> dict[str, Any]:
    found = [candidate for candidate in steps(workflow) if candidate.get("name") == name]
    if not found: named = [candidate.get("name") for candidate in steps(workflow) if candidate.get("name")]; raise WorkflowContractError( f'{workflow} has no step named {name!r}. It has {named}. A renamed step is a contract change: rename it here too, or the property this test protects has silently stopped being checked' )
    if len(found) > 1: raise WorkflowContractError(f"{workflow} has {len(found)} steps named {name!r}")
    return found[0]
def job_step(workflow: str, job_id: str, name: str) -> dict[str, Any]:
    candidates = job(workflow, job_id).get("steps", []); found = [candidate for candidate in candidates if candidate.get("name") == name]
    if not found: named = [candidate.get("name") for candidate in candidates if candidate.get("name")]; raise WorkflowContractError( f'{workflow}:{job_id} has no step named {name!r}. It has {named}. A renamed step is a contract change: rename it here too, or the property this test protects has silently stopped being checked' )
    if len(found) > 1: raise WorkflowContractError(f"{workflow}:{job_id} has {len(found)} steps named {name!r}")
    return found[0]
def spec_text(spec: dict[str, Any]) -> str: parts = [str(spec.get("run", "")), str(spec.get("if", "")), str(spec.get("name", ""))]; parts += [f"{key}={value}" for key, value in (spec.get("env") or {}).items()]; parts += [str(spec.get("uses", "")), str(spec.get("with", ""))]; return "\n".join(part for part in parts if part)
def step_text(workflow: str, name: str) -> str: return spec_text(step(workflow, name))
def triggers(workflow: str) -> dict[str, Any]: parsed = load(workflow); return parsed.get("on") if "on" in parsed else parsed.get(True, {})
_UNSUPPORTED_GLOB_CHARACTERS = frozenset("[]{}()\\")
@cache
def _segment_regex(segment: str) -> re.Pattern[str]:
    pieces = []
    for character in segment:
        if character == "*": pieces.append("[^/]*")
        elif character == "?": pieces.append("[^/]")
        else: pieces.append(re.escape(character))
    return re.compile("".join(pieces))
def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern: return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest: return True
        return any(_match_segments(rest, path[skip:]) for skip in range(len(path) + 1))
    if not path: return False
    return _segment_regex(head).fullmatch(path[0]) is not None and _match_segments(rest, path[1:])
def glob_matches(pattern: str, path: str) -> bool:
    if not path or path.startswith("/") or path.endswith("/"): raise ValueError(f"unsupported path {path!r}: expected a file name as git reports it")
    negated = pattern.startswith("!"); body = pattern[1:] if negated else pattern
    if not body or body.startswith(("!", "/", "./")): raise ValueError(f"unsupported glob {pattern!r}: empty, repeated negation or rooted")
    if _UNSUPPORTED_GLOB_CHARACTERS & set(body) or "!" in body: raise ValueError(f"unsupported glob {pattern!r}: only *, ? and ** are implemented")
    segments = tuple(body.split("/"))
    if any("**" in segment and segment != "**" for segment in segments): raise ValueError(f"unsupported glob {pattern!r}: ** must be a whole path segment")
    return _match_segments(segments, tuple(path.split("/"))) is not negated
def paths_filters(workflow: str, job_id: str = "changes") -> list[tuple[str, dict[str, list[str]]]]:
    found = []
    for spec in job(workflow, job_id).get("steps", []):
        if not str(spec.get("uses", "")).startswith("dorny/paths-filter@"): continue
        options = spec.get("with") or {}; quantifier = str(options.get("predicate-quantifier", "some"))
        if quantifier not in {"some", "every"}: raise WorkflowContractError(f"{workflow}:{job_id} sets predicate-quantifier {quantifier!r}")
        lanes: dict[str, list[str]] = {}
        for lane, entries in yaml.safe_load(options["filters"]).items():
            patterns: list[str] = []
            for entry in entries if isinstance(entries, list) else [entries]:
                if not isinstance(entry, str): raise WorkflowContractError(f"{workflow}:{job_id} lane {lane!r} has a non-string entry {entry!r}")
                patterns.append(entry)
            lanes[lane] = patterns
        found.append((quantifier, lanes))
    if not found: raise WorkflowContractError(f"{workflow}:{job_id} has no dorny/paths-filter step")
    return found
def selected_lanes(workflow: str, paths: Iterable[str], job_id: str = "changes") -> set[str]:
    changed = list(paths); selected: set[str] = set()
    for quantifier, lanes in paths_filters(workflow, job_id):
        combine = all if quantifier == "every" else any
        for lane, patterns in lanes.items():
            if any(combine(glob_matches(pattern, path) for pattern in patterns) for path in changed): selected.add(lane)
    return selected
