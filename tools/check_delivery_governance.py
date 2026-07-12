#!/usr/bin/env python3
"""Validate delivery manifests, the Vision issue graph, and optional GitHub parity."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "governance" / "vision-issue-graph.json"
RUNGS = ("E0", "E1", "E2", "E3", "E4", "E5")
RUNG_LABELS = {
    "E0": "code",
    "E1": "tiny",
    "E2": "contract",
    "E3": "medium",
    "E4": "hardening",
    "E5": "large",
}
TERMINAL_EVIDENCE = frozenset((*RUNGS[2:], "GRADUATION"))
EDGE_CLASSES = frozenset(("start", "freeze", "closure", "informational"))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_REF_RE = re.compile(r"^git:([0-9a-f]{40}):(.+)$")
EVIDENCE_ID_RE = re.compile(r"^capability-evidence:issue-[0-9]+:v[0-9]+$")
PATH_PATTERN_RE = re.compile(r"^[^*?\[\]]+(?:/\*\*)?$")
CAPABILITY_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "issue",
        "state",
        "accepted_rung",
        "producer_commit",
        "source_pr",
        "accepted_by",
        "accepted_at",
        "commands",
        "git_objects",
        "attestation_ref",
        "claim_ceiling",
        "residual_risks",
    }
)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_object(commit: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", f"{commit}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def labels(issue: dict[str, Any]) -> set[str]:
    return {label["name"] for label in issue.get("labels", [])}


def milestone_title(issue: dict[str, Any]) -> str | None:
    milestone = issue.get("milestone")
    return milestone.get("title") if milestone else None


def path_patterns_overlap(left: str, right: str) -> bool:
    if PATH_PATTERN_RE.fullmatch(left) is None or PATH_PATTERN_RE.fullmatch(right) is None:
        return True

    def fixed_prefix(pattern: str) -> str:
        wildcard_positions = [position for token in ("*", "?", "[") if (position := pattern.find(token)) >= 0]
        end = min(wildcard_positions) if wildcard_positions else len(pattern)
        return pattern[:end].rstrip("/")

    left_prefix = fixed_prefix(left)
    right_prefix = fixed_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(f"{right_prefix}/")
        or right_prefix.startswith(f"{left_prefix}/")
    )


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)


def validate_gate_order(
    validation: Validation,
    gates: dict[int, dict[str, Any]],
    gate_order: Any,
) -> list[int]:
    validation.require(isinstance(gate_order, list), "gate_order must be a list")
    if not isinstance(gate_order, list):
        return []
    validation.require(bool(gate_order), "gate_order must not be empty")
    entries_are_ids = all(
        isinstance(gate_number, int) and not isinstance(gate_number, bool) for gate_number in gate_order
    )
    validation.require(
        entries_are_ids,
        "gate_order entries must be integer issue IDs",
    )
    if not gate_order or not entries_are_ids:
        return []
    validation.require(len(gate_order) == len(set(gate_order)), "gate_order must not contain duplicates")
    validation.require(set(gate_order) == set(gates), "gate_order must contain every Gate ID exactly once")
    if len(gate_order) != len(set(gate_order)) or set(gate_order) != set(gates):
        return []

    gate_statuses = [gates[number].get("status") for number in gate_order]
    validation.require(
        all(status in {"done", "active", "queued"} for status in gate_statuses),
        "invalid Gate lifecycle status",
    )
    status_rank = {"done": 0, "active": 1, "queued": 2}
    if all(status in status_rank for status in gate_statuses):
        validation.require(
            gate_statuses == sorted(gate_statuses, key=status_rank.__getitem__),
            "Gate statuses must be done -> active -> queued",
        )
        validation.require(
            sum(status == "active" for status in gate_statuses)
            == (0 if all(status == "done" for status in gate_statuses) else 1),
            "exactly the earliest incomplete Gate must be active",
        )
    return gate_order


def validate_manifest_paths(validation: Validation, batch_id: str, paths: dict[str, Any]) -> None:
    categories: dict[str, list[str]] = {}
    for category in ("writable", "read_only", "forbidden", "integration_surface"):
        patterns = paths.get(category)
        validation.require(
            isinstance(patterns, list) and all(isinstance(pattern, str) and pattern for pattern in patterns),
            f"{batch_id}: {category} paths must be a list of non-empty globs",
        )
        categories[category] = patterns if isinstance(patterns, list) else []
        for pattern in categories[category]:
            validation.require(
                PATH_PATTERN_RE.fullmatch(pattern) is not None,
                f"{batch_id}: path pattern {pattern!r} must be exact or end with '/**'",
            )
        validation.require(
            len(categories[category]) == len(set(categories[category])),
            f"{batch_id}: {category} paths contain duplicates",
        )

    for writable in categories["writable"]:
        for forbidden in categories["forbidden"]:
            validation.require(
                not path_patterns_overlap(writable, forbidden),
                f"{batch_id}: writable path {writable!r} overlaps forbidden path {forbidden!r}",
            )
        for read_only in categories["read_only"]:
            validation.require(
                not path_patterns_overlap(writable, read_only),
                f"{batch_id}: writable path {writable!r} overlaps read-only path {read_only!r}",
            )
        for integration_surface in categories["integration_surface"]:
            if path_patterns_overlap(writable, integration_surface):
                validation.require(
                    bool(paths.get("lease_owner")),
                    f"{batch_id}: shared writable path {writable!r} requires a lease owner",
                )

    for forbidden in categories["forbidden"]:
        for integration_surface in categories["integration_surface"]:
            validation.require(
                not path_patterns_overlap(forbidden, integration_surface),
                f"{batch_id}: forbidden path {forbidden!r} overlaps integration surface {integration_surface!r}",
            )


def validate_capability_evidence(
    validation: Validation,
    issue_number: int,
    terminal_evidence: str,
    evidence_ref: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if evidence_ref is None:
        return None
    validation.require(isinstance(evidence_ref, dict), f"issue #{issue_number}: evidence reference must be an object")
    if not isinstance(evidence_ref, dict):
        return None
    relative_path = evidence_ref.get("path")
    expected_hash = evidence_ref.get("sha256")
    validation.require(
        isinstance(relative_path, str) and bool(relative_path),
        f"issue #{issue_number}: evidence path is missing",
    )
    validation.require(
        isinstance(expected_hash, str) and SHA256_RE.fullmatch(expected_hash) is not None,
        f"issue #{issue_number}: evidence SHA-256 is invalid",
    )
    if not isinstance(relative_path, str) or not relative_path:
        return None
    path = ROOT / relative_path
    validation.require(path.is_file(), f"issue #{issue_number}: evidence file does not exist")
    if not path.is_file():
        return None
    actual_hash = sha256(path)
    validation.require(expected_hash == actual_hash, f"issue #{issue_number}: evidence SHA-256 mismatch")
    evidence = load_json(path)
    validation.require(isinstance(evidence, dict), f"issue #{issue_number}: evidence payload must be an object")
    if not isinstance(evidence, dict):
        return None
    validation.require(
        set(evidence) == CAPABILITY_EVIDENCE_FIELDS,
        f"issue #{issue_number}: evidence fields do not match schema v1",
    )
    validation.require(evidence.get("schema_version") == 1, f"issue #{issue_number}: unsupported evidence schema")
    validation.require(
        isinstance(evidence.get("evidence_id"), str) and EVIDENCE_ID_RE.fullmatch(evidence["evidence_id"]) is not None,
        f"issue #{issue_number}: evidence ID is invalid",
    )
    if isinstance(evidence.get("evidence_id"), str):
        validation.require(
            evidence["evidence_id"].startswith(f"capability-evidence:issue-{issue_number}:v"),
            f"issue #{issue_number}: evidence ID names another issue",
        )
    validation.require(evidence.get("issue") == issue_number, f"issue #{issue_number}: evidence issue mismatch")
    validation.require(evidence.get("state") == "accepted", f"issue #{issue_number}: evidence is not accepted")
    validation.require(
        evidence.get("accepted_rung") == terminal_evidence,
        f"issue #{issue_number}: accepted evidence does not reach its terminal rung",
    )
    commit = evidence.get("producer_commit")
    commit_is_valid = isinstance(commit, str) and GIT_SHA_RE.fullmatch(commit) is not None
    validation.require(commit_is_valid, f"issue #{issue_number}: invalid evidence producer commit")
    commands = evidence.get("commands")
    validation.require(
        isinstance(commands, list)
        and bool(commands)
        and all(isinstance(command, str) and bool(command.strip()) for command in commands),
        f"issue #{issue_number}: evidence commands must be a non-empty string list",
    )
    attestation_ref = evidence.get("attestation_ref")
    validation.require(
        isinstance(attestation_ref, str) and bool(attestation_ref.strip()),
        f"issue #{issue_number}: attestation is missing",
    )
    validation.require(
        isinstance(evidence.get("source_pr"), int)
        and not isinstance(evidence.get("source_pr"), bool)
        and evidence["source_pr"] > 0,
        f"issue #{issue_number}: source PR is invalid",
    )
    validation.require(
        isinstance(evidence.get("accepted_by"), str) and bool(evidence["accepted_by"].strip()),
        f"issue #{issue_number}: evidence acceptor is missing",
    )
    accepted_at = evidence.get("accepted_at")
    accepted_at_is_valid = False
    if isinstance(accepted_at, str) and accepted_at:
        try:
            accepted_at_is_valid = datetime.fromisoformat(accepted_at.replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            pass
    validation.require(accepted_at_is_valid, f"issue #{issue_number}: evidence acceptance time is invalid")
    validation.require(
        isinstance(evidence.get("claim_ceiling"), str) and bool(evidence["claim_ceiling"].strip()),
        f"issue #{issue_number}: claim ceiling is missing",
    )
    residual_risks = evidence.get("residual_risks")
    validation.require(
        isinstance(residual_risks, list)
        and all(isinstance(risk, str) and bool(risk.strip()) for risk in residual_risks),
        f"issue #{issue_number}: residual risks must be a string list",
    )
    git_objects = evidence.get("git_objects")
    git_objects_are_valid = isinstance(git_objects, list) and bool(git_objects)
    validation.require(git_objects_are_valid, f"issue #{issue_number}: evidence git_objects are missing")
    if git_objects_are_valid:
        for index, artifact in enumerate(git_objects):
            if not isinstance(artifact, dict):
                validation.require(False, f"issue #{issue_number}: git_objects[{index}] must be an object")
                continue
            artifact_path = artifact.get("path")
            expected_oid = artifact.get("oid")
            path_is_valid = isinstance(artifact_path, str) and bool(artifact_path)
            oid_is_valid = isinstance(expected_oid, str) and GIT_SHA_RE.fullmatch(expected_oid) is not None
            validation.require(path_is_valid, f"issue #{issue_number}: git_objects[{index}] path is missing")
            validation.require(oid_is_valid, f"issue #{issue_number}: git_objects[{index}] oid is invalid")
            if not (commit_is_valid and path_is_valid and oid_is_valid):
                continue
            validation.require(
                git_object(commit, artifact_path) == expected_oid,
                f"issue #{issue_number}: git object mismatch for {artifact_path!r}",
            )
    return evidence


def validate_manifest(
    validation: Validation,
    batch_id: str,
    graph_batch: dict[str, Any],
    issues: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    relative_path = graph_batch.get("manifest")
    validation.require(isinstance(relative_path, str), f"{batch_id}: manifest path is missing")
    if not isinstance(relative_path, str):
        return None

    path = ROOT / relative_path
    validation.require(path.is_file(), f"{batch_id}: manifest does not exist: {relative_path}")
    if not path.is_file():
        return None

    expected_digest = graph_batch.get("sha256")
    actual_digest = sha256(path)
    validation.require(
        isinstance(expected_digest, str) and SHA256_RE.fullmatch(expected_digest) is not None,
        f"{batch_id}: graph SHA-256 is invalid",
    )
    validation.require(
        expected_digest == actual_digest,
        f"{batch_id}: manifest hash mismatch; graph={expected_digest}, actual={actual_digest}",
    )

    manifest = load_json(path)
    validation.require(isinstance(manifest, dict), f"{batch_id}: manifest payload must be an object")
    if not isinstance(manifest, dict):
        return None
    for field in (
        "schema_version",
        "batch_id",
        "revision",
        "issue",
        "owner_gate",
        "status",
        "lane",
        "last_accepted_rung",
        "target_rung",
        "terminal_rung",
        "claim_ceiling",
        "capability_issues",
        "closes_issues",
        "owners",
        "activation",
        "dependencies",
        "corpus",
        "paths",
        "release_activation",
        "acceptance",
        "invalidation",
        "rollback",
    ):
        validation.require(field in manifest, f"{batch_id}: manifest field {field!r} is missing")

    validation.require(manifest.get("schema_version") == 1, f"{batch_id}: unsupported manifest schema")
    revision = manifest.get("revision")
    validation.require(
        isinstance(revision, int) and not isinstance(revision, bool) and revision > 0,
        f"{batch_id}: manifest revision must be a positive integer",
    )
    for field in ("capability_issues", "closes_issues"):
        issue_numbers = manifest.get(field)
        issue_numbers_are_valid = isinstance(issue_numbers, list) and all(
            isinstance(number, int) and not isinstance(number, bool) and number > 0 for number in issue_numbers
        )
        validation.require(
            issue_numbers_are_valid,
            f"{batch_id}: {field} must be a list of positive issue IDs",
        )
        if issue_numbers_are_valid:
            validation.require(
                len(issue_numbers) == len(set(issue_numbers)), f"{batch_id}: {field} contains duplicates"
            )
    validation.require(isinstance(manifest.get("owners"), dict), f"{batch_id}: owners must be an object")
    validation.require(isinstance(manifest.get("activation"), dict), f"{batch_id}: activation must be an object")
    validation.require(isinstance(manifest.get("paths"), dict), f"{batch_id}: paths must be an object")
    validation.require(
        isinstance(manifest.get("dependencies"), list),
        f"{batch_id}: dependencies must be a list",
    )

    validation.require(
        manifest.get("batch_id") == batch_id, f"{batch_id}: manifest batch_id disagrees with its graph key"
    )
    for field in ("issue", "owner_gate", "status", "target_rung", "terminal_rung"):
        validation.require(
            manifest.get(field) == graph_batch.get(field),
            f"{batch_id}: manifest {field} disagrees with the Vision graph",
        )

    last_accepted = manifest.get("last_accepted_rung")
    target_rung = manifest.get("target_rung")
    terminal = manifest.get("terminal_rung")
    validation.require(
        last_accepted is None or last_accepted in RUNGS,
        f"{batch_id}: invalid last accepted rung {last_accepted!r}",
    )
    validation.require(target_rung in RUNGS, f"{batch_id}: invalid target rung {target_rung!r}")
    validation.require(terminal in RUNGS, f"{batch_id}: invalid terminal rung {terminal!r}")
    if target_rung in RUNGS:
        if last_accepted == terminal:
            validation.require(
                manifest.get("status") == "done" and target_rung == terminal,
                f"{batch_id}: only a done batch may have accepted its terminal rung",
            )
        else:
            if last_accepted is None:
                expected_target = "E0"
            elif last_accepted in RUNGS and RUNGS.index(last_accepted) < len(RUNGS) - 1:
                expected_target = RUNGS[RUNGS.index(last_accepted) + 1]
            else:
                expected_target = None
                validation.require(False, f"{batch_id}: terminal E5 evidence cannot advance to another rung")
            if expected_target is not None:
                validation.require(
                    target_rung == expected_target,
                    f"{batch_id}: target must be exactly one rung above the last accepted evidence",
                )
    if target_rung in RUNGS and terminal in RUNGS:
        validation.require(
            RUNGS.index(target_rung) <= RUNGS.index(terminal),
            f"{batch_id}: target rung is above the terminal rung",
        )
    if last_accepted in RUNGS and terminal in RUNGS:
        validation.require(
            RUNGS.index(last_accepted) <= RUNGS.index(terminal),
            f"{batch_id}: last accepted rung is above the terminal rung",
        )

    owner_gate = manifest.get("owner_gate")
    for issue_number in manifest.get("capability_issues", []):
        validation.require(issue_number in issues, f"{batch_id}: unknown capability issue #{issue_number}")
        if issue_number in issues:
            validation.require(
                issues[issue_number]["gate"] == owner_gate,
                f"{batch_id}: capability issue #{issue_number} belongs to another Gate",
            )
    if terminal == "E1":
        validation.require(
            manifest.get("closes_issues") == [],
            f"{batch_id}: E1 discovery cannot close a higher-rung capability issue",
        )
        output = manifest.get("acceptance", {}).get("output", {})
        validation.require(
            output.get("stable_handoff") is False,
            f"{batch_id}: E1 output must not be a stable handoff",
        )
        validation.require(
            manifest.get("release_activation", {}).get("allowed") is False,
            f"{batch_id}: E1 code cannot enter the accepted release allow-list",
        )

    owners = manifest.get("owners", {})
    implementation_owner = owners.get("implementation")
    validation.require(bool(implementation_owner), f"{batch_id}: implementation owner is missing")
    status = manifest.get("status")
    validation.require(status in {"queued", "active", "blocked", "cancelled", "done"}, f"{batch_id}: invalid status")
    if status == "done":
        validation.require(
            last_accepted == terminal,
            f"{batch_id}: done batch must have accepted its terminal rung",
        )
    activation = manifest.get("activation", {})
    corpus = manifest.get("corpus", {})
    if status == "active":
        reviewer = owners.get("reviewer")
        validation.require(bool(reviewer), f"{batch_id}: active batch reviewer is missing")
        validation.require(reviewer != implementation_owner, f"{batch_id}: reviewer must be independent")
        validation.require(
            isinstance(activation.get("base_sha"), str) and GIT_SHA_RE.fullmatch(activation["base_sha"]) is not None,
            f"{batch_id}: active batch must pin a full main base SHA",
        )
        validation.require(bool(corpus.get("manifest_path")), f"{batch_id}: active batch corpus path is missing")
        validation.require(
            isinstance(corpus.get("sha256"), str) and SHA256_RE.fullmatch(corpus["sha256"]) is not None,
            f"{batch_id}: active batch corpus SHA-256 is missing",
        )

    pinned_dependency_paths: dict[str, str] = {}
    for dependency in manifest.get("dependencies", []):
        dependency_class = dependency.get("class")
        validation.require(dependency_class in EDGE_CLASSES, f"{batch_id}: invalid dependency class")
        if dependency_class == "start":
            validation.require(
                dependency.get("state") == "accepted",
                f"{batch_id}: every start dependency must already be accepted",
            )
            issue_number = dependency.get("issue")
            if dependency.get("legacy_accepted") is True:
                validation.require(
                    issue_number in {57, 58},
                    f"{batch_id}: only issue #57/#58 may use legacy accepted dependencies",
                )
                expected_evidence = issues.get(issue_number, {}).get("accepted_evidence")
                validation.require(
                    dependency.get("evidence") == expected_evidence,
                    f"{batch_id}: legacy dependency #{issue_number} must pin its accepted evidence record",
                )
                if isinstance(expected_evidence, dict):
                    evidence_path = ROOT / expected_evidence["path"]
                    if evidence_path.is_file():
                        evidence_payload = load_json(evidence_path)
                        producer_commit = evidence_payload.get("producer_commit")
                        evidence_objects = {
                            (artifact.get("path"), artifact.get("oid"))
                            for artifact in evidence_payload.get("git_objects", [])
                            if isinstance(artifact, dict)
                        }
                        if "ref" in dependency or "git_tree" in dependency:
                            declared_objects = [{"ref": dependency.get("ref"), "oid": dependency.get("git_tree")}]
                        else:
                            declared_objects = dependency.get("git_objects")
                        validation.require(
                            isinstance(declared_objects, list) and bool(declared_objects),
                            f"{batch_id}: legacy dependency #{issue_number} must declare exact Git objects",
                        )
                        for declared in declared_objects if isinstance(declared_objects, list) else []:
                            if not isinstance(declared, dict):
                                validation.require(
                                    False,
                                    f"{batch_id}: legacy dependency #{issue_number} Git object must be an object",
                                )
                                continue
                            match = GIT_REF_RE.fullmatch(str(declared.get("ref", "")))
                            declared_oid = declared.get("oid")
                            validation.require(
                                match is not None and match.group(1) == producer_commit,
                                f"{batch_id}: legacy dependency #{issue_number} ref must use its evidence commit",
                            )
                            validation.require(
                                isinstance(declared_oid, str) and GIT_SHA_RE.fullmatch(declared_oid) is not None,
                                f"{batch_id}: legacy dependency #{issue_number} Git object OID is invalid",
                            )
                            if match is None or not isinstance(declared_oid, str):
                                continue
                            validation.require(
                                git_object(match.group(1), match.group(2)) == declared_oid,
                                f"{batch_id}: legacy dependency #{issue_number} git object does not match its ref",
                            )
                            validation.require(
                                (match.group(2), declared_oid) in evidence_objects,
                                f"{batch_id}: legacy dependency #{issue_number} ref is not in its evidence object set",
                            )
                            previous_oid = pinned_dependency_paths.get(match.group(2))
                            validation.require(
                                previous_oid in {None, declared_oid},
                                f"{batch_id}: dependencies pin conflicting objects for {match.group(2)!r}",
                            )
                            pinned_dependency_paths[match.group(2)] = declared_oid
            else:
                validation.require(
                    bool(dependency.get("handoff_manifest")),
                    f"{batch_id}: non-legacy start dependency #{issue_number} requires a HandoffManifest",
                )

    paths = manifest.get("paths", {})
    validation.require(bool(paths.get("writable")), f"{batch_id}: writable paths are missing")
    validation.require(bool(paths.get("forbidden")), f"{batch_id}: forbidden paths are missing")
    validate_manifest_paths(validation, batch_id, paths)
    return manifest


def validate_acyclic(validation: Validation, nodes: set[int], edges: list[tuple[int, int]]) -> None:
    outgoing: dict[int, set[int]] = defaultdict(set)
    indegree = {node: 0 for node in nodes}
    for source, target in edges:
        validation.require(source in nodes, f"dependency source #{source} is not managed")
        validation.require(target in nodes, f"dependency target #{target} is not managed")
        validation.require(source != target, f"dependency edge #{source} -> #{target} is self-referential")
        if source in nodes and target in nodes and target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1

    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in sorted(outgoing[source]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    validation.require(visited == len(nodes), "Vision issue dependency graph contains a cycle")


def validate_github(
    validation: Validation,
    graph: dict[str, Any],
    github_issues: list[dict[str, Any]],
) -> None:
    gates = {int(number): gate for number, gate in graph["gates"].items()}
    capability_issues = {int(number): issue for number, issue in graph["issues"].items()}
    batches = graph["batches"]
    batch_by_issue = {batch["issue"]: batch for batch in batches.values()}
    expected = {graph["root_issue"], *gates, *capability_issues, *batch_by_issue}
    live = {issue["number"]: issue for issue in github_issues if "scope:vision" in labels(issue)}
    incoming_edges: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph.get("artifact_edges", []):
        if edge.get("class") != "informational":
            incoming_edges[edge.get("to")].append(edge)
    validation.require(
        set(live) == expected,
        f"GitHub scope:vision parity mismatch; missing={sorted(expected - set(live))}, extra={sorted(set(live) - expected)}",
    )

    for issue_number in sorted(expected & set(live)):
        issue = live[issue_number]
        issue_labels = labels(issue)
        issue_state = str(issue.get("state", "")).upper()
        if issue_number == graph["root_issue"]:
            expected_milestone = None
            root_should_close = graph.get("root_accepted_evidence") is not None and all(
                gate.get("status") == "done" for gate in gates.values()
            )
            validation.require(
                (issue_state == "CLOSED") == root_should_close,
                f"#{issue_number}: root state disagrees with Gate graduation fan-in",
            )
            validation.require(
                not any(label.startswith(("gate:", "batch:")) for label in issue_labels),
                f"#{issue_number}: root issue must not carry Gate or batch status",
            )
        elif issue_number in gates:
            expected_milestone = gates[issue_number]["milestone"]
            expected_gate_label = f"gate:{gates[issue_number]['status']}"
            validation.require(expected_gate_label in issue_labels, f"#{issue_number}: missing {expected_gate_label}")
            validation.require(
                not any(label.startswith("batch:") for label in issue_labels),
                f"#{issue_number}: Gate epic must not carry a batch status",
            )
            validation.require(
                len([label for label in issue_labels if label.startswith("gate:")]) == 1,
                f"#{issue_number}: Gate epic must carry exactly one Gate status",
            )
            gate_done = gates[issue_number]["status"] == "done"
            validation.require(
                (issue_state == "CLOSED") == gate_done,
                f"#{issue_number}: GitHub state disagrees with Gate lifecycle",
            )
            if gate_done:
                validation.require(
                    gates[issue_number].get("accepted_evidence") is not None,
                    f"#{issue_number}: closed Gate lacks an exact candidate evidence bundle",
                )
                for child in gates[issue_number]["acceptance_issues"]:
                    validation.require(
                        str(live[child].get("state", "")).upper() == "CLOSED",
                        f"#{issue_number}: Gate closed while child #{child} remains open",
                    )
        elif issue_number in capability_issues:
            expected_milestone = gates[capability_issues[issue_number]["gate"]]["milestone"]
            validation.require(
                not any(label.startswith(("batch:", "gate:")) for label in issue_labels),
                f"#{issue_number}: capability issue must not carry a batch or Gate status",
            )
            if issue_state == "CLOSED":
                validation.require(
                    capability_issues[issue_number].get("accepted_evidence") is not None,
                    f"#{issue_number}: closed capability lacks accepted terminal evidence",
                )
                for edge in incoming_edges[issue_number]:
                    predecessor = edge.get("from")
                    if predecessor not in live:
                        validation.require(False, f"#{issue_number}: dependency #{predecessor} is missing from GitHub")
                        continue
                    validation.require(
                        str(live[predecessor].get("state", "")).upper() == "CLOSED",
                        f"#{issue_number}: closed before {edge.get('class')} predecessor #{predecessor}",
                    )
                    if predecessor in capability_issues:
                        validation.require(
                            capability_issues[predecessor].get("accepted_evidence") is not None,
                            f"#{issue_number}: predecessor #{predecessor} lacks accepted evidence",
                        )
        else:
            batch = batch_by_issue[issue_number]
            expected_milestone = gates[batch["owner_gate"]]["milestone"]
            expected_batch_label = f"batch:{batch['status']}"
            validation.require(expected_batch_label in issue_labels, f"#{issue_number}: missing {expected_batch_label}")
            validation.require(
                len([label for label in issue_labels if label.startswith("batch:")]) == 1,
                f"#{issue_number}: batch must carry exactly one batch status",
            )
            validation.require(
                f"rung:{RUNG_LABELS[batch['target_rung']]}" in issue_labels,
                f"#{issue_number}: current rung label disagrees with the manifest",
            )
            validation.require(
                len([label for label in issue_labels if label.startswith("rung:")]) == 1,
                f"#{issue_number}: batch must carry exactly one target rung",
            )
            if batch["target_rung"] in {"E0", "E1"}:
                validation.require(
                    "readiness:provisional" in issue_labels,
                    f"#{issue_number}: lower-rung batch must be readiness:provisional",
                )
                validation.require(
                    len([label for label in issue_labels if label.startswith("readiness:")]) == 1,
                    f"#{issue_number}: provisional batch must carry exactly one readiness state",
                )
            validation.require(
                not any(label.startswith("gate:") for label in issue_labels),
                f"#{issue_number}: batch issue must not carry a Gate status",
            )
            expected_open = batch["status"] not in {"done", "cancelled"}
            validation.require(
                (issue_state == "OPEN") == expected_open,
                f"#{issue_number}: GitHub state disagrees with batch lifecycle",
            )
            body = issue.get("body") or ""
            validation.require(batch["manifest"] in body, f"#{issue_number}: manifest path is missing from body")
            validation.require(
                f"sha256:{batch['sha256']}" in body,
                f"#{issue_number}: manifest SHA-256 is missing or stale in body",
            )

        validation.require(
            milestone_title(issue) == expected_milestone,
            f"#{issue_number}: milestone mismatch; expected={expected_milestone!r}, actual={milestone_title(issue)!r}",
        )


def validate(github_path: Path | None) -> Validation:
    validation = Validation()
    graph = load_json(GRAPH_PATH)
    validation.require(graph.get("schema_version") == 1, "unsupported Vision graph schema")
    gates = {int(number): gate for number, gate in graph.get("gates", {}).items()}
    gate_order = validate_gate_order(validation, gates, graph.get("gate_order"))

    issue_map = {int(number): issue for number, issue in graph.get("issues", {}).items()}
    acceptance_issues: list[int] = []
    for gate_number, gate in gates.items():
        for issue_number in gate.get("acceptance_issues", []):
            acceptance_issues.append(issue_number)
            validation.require(
                issue_number in issue_map,
                f"Gate #{gate_number}: issue #{issue_number} has no terminal evidence mapping",
            )
            if issue_number in issue_map:
                validation.require(
                    issue_map[issue_number].get("gate") == gate_number,
                    f"issue #{issue_number}: Gate ownership mismatch",
                )
    validation.require(
        len(acceptance_issues) == len(set(acceptance_issues)), "a capability issue belongs to more than one Gate"
    )
    validation.require(
        set(acceptance_issues) == set(issue_map), "Gate acceptance lists and terminal evidence map differ"
    )
    for issue_number, issue in issue_map.items():
        validation.require(
            issue.get("terminal_evidence") in TERMINAL_EVIDENCE,
            f"issue #{issue_number}: invalid terminal evidence",
        )
        validate_capability_evidence(
            validation,
            issue_number,
            issue.get("terminal_evidence"),
            issue.get("accepted_evidence"),
        )

    manifests: dict[str, dict[str, Any]] = {}
    for batch_id, graph_batch in graph.get("batches", {}).items():
        manifest = validate_manifest(validation, batch_id, graph_batch, issue_map)
        if manifest is not None:
            manifests[batch_id] = manifest

    active_writes: list[tuple[str, str]] = []
    for batch_id, manifest in manifests.items():
        if manifest.get("status") != "active":
            continue
        for writable in manifest["paths"]["writable"]:
            for other_writable, other_batch in active_writes:
                validation.require(
                    not path_patterns_overlap(writable, other_writable),
                    f"active batches {other_batch} and {batch_id} overlap writable paths "
                    f"{other_writable!r} and {writable!r}",
                )
            active_writes.append((writable, batch_id))

    root_issue = graph.get("root_issue")
    batch_issues = {batch["issue"] for batch in graph.get("batches", {}).values()}
    nodes = {root_issue, *gates, *issue_map, *batch_issues}
    validation.require(
        len(nodes) == 1 + len(gates) + len(issue_map) + len(batch_issues),
        "root, Gate, capability, and batch issue IDs must be unique",
    )
    edges: list[tuple[int, int]] = []
    for edge in graph.get("artifact_edges", []):
        validation.require(edge.get("class") in EDGE_CLASSES, f"invalid edge class in {edge}")
        edges.append((edge.get("from"), edge.get("to")))
    for gate_number, gate in gates.items():
        edges.extend((issue_number, gate_number) for issue_number in gate["acceptance_issues"])
    if gate_order:
        edges.extend(zip(gate_order, gate_order[1:]))
        edges.append((gate_order[-1], root_issue))
    validate_acyclic(validation, nodes, edges)

    if github_path is not None:
        validate_github(validation, graph, load_json(github_path))
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-issues", type=Path, help="JSON exported by gh issue list")
    args = parser.parse_args()
    validation = validate(args.github_issues)
    if validation.errors:
        for error in validation.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    scope = "offline graph and live GitHub parity" if args.github_issues else "offline graph"
    print(f"Delivery governance check passed: {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
