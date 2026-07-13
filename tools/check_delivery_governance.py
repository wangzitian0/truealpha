#!/usr/bin/env python3
"""Validate delivery manifests, the Vision issue graph, and optional GitHub parity."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

GATE0_VALIDATOR_PATH = Path(__file__).with_name("check_gate0_candidate.py")
GATE0_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "truealpha_gate0_candidate_validator", GATE0_VALIDATOR_PATH
)
assert GATE0_VALIDATOR_SPEC is not None and GATE0_VALIDATOR_SPEC.loader is not None
gate0_validator = importlib.util.module_from_spec(GATE0_VALIDATOR_SPEC)
sys.modules[GATE0_VALIDATOR_SPEC.name] = gate0_validator
GATE0_VALIDATOR_SPEC.loader.exec_module(gate0_validator)
validate_gate0_candidate = gate0_validator.validate_gate0_candidate

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "governance" / "vision-issue-graph.json"
GATE0_MANIFEST_PATH = "governance/gate0/manifest-v4.json"
GATE0_AUTHORIZATION_CONTROL_PATHS = frozenset(
    {
        ".github/workflows/ci-governance.yml",
        "Makefile",
        "tools/check_delivery_governance.py",
        "tools/check_gate0_candidate.py",
    }
)
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
MANIFEST_PREFIX = "governance/batches/"
BATCH_MIRROR_START = "<!-- capability-batch-mirror:start -->"
BATCH_MIRROR_END = "<!-- capability-batch-mirror:end -->"
GOVERNANCE_CONTROL_PATHS = (
    ".github/workflows/ci-governance.yml",
    "governance/**",
    "libs/runtime/tests/test_delivery_governance.py",
    "tools/check_delivery_governance.py",
)
LEASE_REQUIRED_EXACT_PATHS = frozenset(
    {
        "AGENTS.md",
        "init.md",
        "docs/architecture-contract-closure.md",
        "uv.lock",
        "apps/app-web/bun.lock",
        "apps/data-engine/src/data_engine/mvp_probe.py",
    }
)
HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "handoff_id",
        "revision",
        "state",
        "producer",
        "schema_epoch",
        "readiness_ceiling",
        "evidence",
        "allowed_consumers",
        "allowed_environments",
        "retention",
        "verification",
        "revocation",
    }
)
LEASE_FIELDS = frozenset(
    {
        "schema_version",
        "lease_id",
        "batch_id",
        "owner",
        "state",
        "paths",
        "base_sha",
        "expires_at",
    }
)
RUNG_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "schema_version",
        "batch_id",
        "manifest_sha256",
        "accepted_rung",
        "base_sha",
        "producer_head_sha",
        "commands",
        "negative_controls",
        "stable_handoff",
        "created_at",
    }
)
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


def run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def git_merge_base(left: str, right: str) -> str | None:
    result = run_git("merge-base", left, right)
    return result.stdout.strip() if result.returncode == 0 else None


def git_changed_paths(base: str, head: str) -> tuple[str, ...] | None:
    result = run_git("diff", "--name-only", "--diff-filter=ACMRD", f"{base}...{head}")
    if result.returncode != 0:
        return None
    return tuple(sorted(path for path in result.stdout.splitlines() if path))


def git_json(commit: str, relative_path: str) -> Any | None:
    result = run_git("show", f"{commit}:{relative_path}")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def git_commit_exists(commit: str) -> bool:
    return run_git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def repo_path(relative_path: str) -> Path | None:
    pure_path = PurePosixPath(relative_path)
    if (
        pure_path.is_absolute()
        or "\\" in relative_path
        or not pure_path.parts
        or any(part in {"", ".", ".."} for part in pure_path.parts)
    ):
        return None
    candidate = ROOT.joinpath(*pure_path.parts)
    try:
        candidate.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None
    return candidate


def valid_repo_pattern(pattern: str) -> bool:
    if PATH_PATTERN_RE.fullmatch(pattern) is None:
        return False
    literal = pattern.removesuffix("/**")
    return bool(literal) and repo_path(literal) is not None


def path_matches_pattern(path: str, pattern: str) -> bool:
    if repo_path(path) is None or not valid_repo_pattern(pattern):
        return False
    if pattern.endswith("/**"):
        prefix = pattern.removesuffix("/**").rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return path == pattern


def matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(path_matches_pattern(path, pattern) for pattern in patterns)


def validate_gate0_candidate_paths(validation: Validation, patterns: list[str]) -> None:
    """Reject filesystem objects whose bytes are not stable Git regular-file content."""
    for pattern in patterns:
        literal = pattern.removesuffix("/**")
        candidate = ROOT.joinpath(*PurePosixPath(literal).parts)
        if candidate.is_symlink():
            validation.require(False, f"Gate 0 v4 aggregate candidate: candidate path is a symlink: {literal!r}")
            continue
        if not pattern.endswith("/**"):
            validation.require(
                candidate.is_file(),
                f"Gate 0 v4 aggregate candidate: candidate path is not a regular file: {literal!r}",
            )
            continue
        validation.require(
            candidate.is_dir(),
            f"Gate 0 v4 aggregate candidate: candidate path is not a directory: {literal!r}",
        )
        if not candidate.is_dir():
            continue
        for entry in candidate.rglob("*"):
            relative = entry.relative_to(ROOT).as_posix()
            if entry.is_symlink():
                validation.require(
                    False,
                    f"Gate 0 v4 aggregate candidate: candidate path is a symlink: {relative!r}",
                )
            elif not entry.is_file() and not entry.is_dir():
                validation.require(
                    False,
                    f"Gate 0 v4 aggregate candidate: candidate path is not a regular file: {relative!r}",
                )


def requires_integration_lease(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        path in LEASE_REQUIRED_EXACT_PATHS
        or path.startswith("db/")
        or name in {"__init__.py", "registry.py", "registries.py", "definitions.py"}
        or "generated" in parts
        or name.endswith((".lock", ".lockb"))
    )


def batch_status_labels(batch: dict[str, Any]) -> set[str]:
    status = "queued" if batch.get("status") == "prepared" else batch.get("status")
    target_rung = batch.get("target_rung")
    if not isinstance(target_rung, str) or target_rung not in RUNG_LABELS:
        raise ValueError(f"invalid batch target rung: {target_rung!r}")
    desired = {f"batch:{status}", f"rung:{RUNG_LABELS[target_rung]}"}
    if target_rung in {"E0", "E1"}:
        desired.add("readiness:provisional")
    return desired


def render_batch_issue_body(body: str, batch_id: str, batch: dict[str, Any]) -> str:
    block = "\n".join(
        (
            BATCH_MIRROR_START,
            f"Batch: `{batch_id}`",
            f"Manifest: `{batch['manifest']}`",
            f"sha256:{batch['sha256']}",
            f"Canonical status: `{batch['status']}`",
            f"Target rung: `{batch['target_rung']}`",
            BATCH_MIRROR_END,
        )
    )
    pattern = re.compile(f"{re.escape(BATCH_MIRROR_START)}.*?{re.escape(BATCH_MIRROR_END)}", re.DOTALL)
    if pattern.search(body):
        return pattern.sub(block, body).rstrip() + "\n"
    prefix = body.rstrip()
    return f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"


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


class CommandRunner(Protocol):
    def __call__(self, command: str) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class PullRequestAdvance:
    batch_id: str
    manifest_path: str
    base_manifest: dict[str, Any]
    manifest: dict[str, Any]
    accepted_rung: str | None
    changed_paths: tuple[str, ...]
    kind: str = "capability_batch"


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
    if all(isinstance(status, str) and status in status_rank for status in gate_statuses):
        typed_statuses = [str(status) for status in gate_statuses]
        validation.require(
            typed_statuses == sorted(typed_statuses, key=lambda status: status_rank[status]),
            "Gate statuses must be done -> active -> queued",
        )
        validation.require(
            sum(status == "active" for status in typed_statuses)
            == (0 if all(status == "done" for status in typed_statuses) else 1),
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
                valid_repo_pattern(pattern),
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
    path = repo_path(relative_path)
    validation.require(path is not None, f"issue #{issue_number}: evidence path escapes the repository")
    if path is None:
        return None
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
    if isinstance(git_objects, list) and git_objects:
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
            assert isinstance(commit, str)
            assert isinstance(artifact_path, str)
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

    path = repo_path(relative_path)
    validation.require(path is not None, f"{batch_id}: manifest path escapes the repository")
    if path is None:
        return None
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
        if isinstance(issue_numbers, list) and issue_numbers_are_valid:
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
    validation.require(
        status in {"queued", "prepared", "active", "blocked", "cancelled", "done"},
        f"{batch_id}: invalid status",
    )
    if status == "done":
        validation.require(
            last_accepted == terminal,
            f"{batch_id}: done batch must have accepted its terminal rung",
        )
    activation = manifest.get("activation", {})
    corpus = manifest.get("corpus", {})
    if status in {"prepared", "active"}:
        reviewer = owners.get("reviewer")
        validation.require(bool(reviewer), f"{batch_id}: {status} batch reviewer is missing")
        validation.require(reviewer != implementation_owner, f"{batch_id}: reviewer must be independent")
        validation.require(
            isinstance(activation.get("base_sha"), str) and GIT_SHA_RE.fullmatch(activation["base_sha"]) is not None,
            f"{batch_id}: {status} batch must pin a full main base SHA",
        )
        validation.require(bool(corpus.get("manifest_path")), f"{batch_id}: {status} batch corpus path is missing")
        validation.require(
            isinstance(corpus.get("sha256"), str) and SHA256_RE.fullmatch(corpus["sha256"]) is not None,
            f"{batch_id}: {status} batch corpus SHA-256 is missing",
        )

    acceptance = manifest.get("acceptance", {})
    commands = acceptance.get("commands") if isinstance(acceptance, dict) else None
    validation.require(
        isinstance(commands, list)
        and bool(commands)
        and all(isinstance(command, str) and bool(command.strip()) for command in commands),
        f"{batch_id}: acceptance commands must be a non-empty string list",
    )
    negative_controls = acceptance.get("negative_controls") if isinstance(acceptance, dict) else None
    validation.require(
        isinstance(negative_controls, list)
        and bool(negative_controls)
        and all(isinstance(control, str) and bool(control.strip()) for control in negative_controls),
        f"{batch_id}: negative controls must be a non-empty string list",
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
                validate_handoff_dependency(validation, batch_id, manifest, dependency)

    paths = manifest.get("paths", {})
    validation.require(bool(paths.get("writable")), f"{batch_id}: writable paths are missing")
    validation.require(bool(paths.get("forbidden")), f"{batch_id}: forbidden paths are missing")
    validate_manifest_paths(validation, batch_id, paths)
    return manifest


def validate_file_reference(
    validation: Validation,
    *,
    owner: str,
    reference: Any,
) -> tuple[Path, dict[str, Any]] | None:
    validation.require(isinstance(reference, dict), f"{owner}: file reference must be an object")
    if not isinstance(reference, dict):
        return None
    relative_path = reference.get("path")
    expected_hash = reference.get("sha256")
    validation.require(isinstance(relative_path, str) and bool(relative_path), f"{owner}: path is missing")
    validation.require(
        isinstance(expected_hash, str) and SHA256_RE.fullmatch(expected_hash) is not None,
        f"{owner}: SHA-256 is invalid",
    )
    if not isinstance(relative_path, str) or not relative_path:
        return None
    path = repo_path(relative_path)
    validation.require(path is not None, f"{owner}: path escapes the repository")
    if path is None:
        return None
    validation.require(path.is_file(), f"{owner}: file does not exist: {relative_path}")
    if not path.is_file():
        return None
    validation.require(sha256(path) == expected_hash, f"{owner}: SHA-256 mismatch")
    payload = load_json(path)
    validation.require(isinstance(payload, dict), f"{owner}: payload must be an object")
    return (path, payload) if isinstance(payload, dict) else None


def validate_handoff_dependency(
    validation: Validation,
    batch_id: str,
    manifest: dict[str, Any],
    dependency: dict[str, Any],
) -> None:
    issue_number = dependency.get("issue")
    loaded = validate_file_reference(
        validation,
        owner=f"{batch_id}: handoff dependency #{issue_number}",
        reference=dependency.get("handoff_manifest"),
    )
    if loaded is None:
        return
    _path, handoff = loaded
    validation.require(set(handoff) == HANDOFF_FIELDS, f"{batch_id}: HandoffManifest fields do not match schema v1")
    validation.require(handoff.get("schema_version") == 1, f"{batch_id}: unsupported HandoffManifest schema")
    content = {key: value for key, value in handoff.items() if key != "handoff_id"}
    handoff_id = handoff.get("handoff_id")
    validation.require(
        isinstance(handoff_id, str)
        and re.fullmatch(r"handoff:[a-z0-9-]+:[0-9a-f]{64}", handoff_id) is not None
        and handoff_id.rsplit(":", maxsplit=1)[-1] == canonical_sha256(content),
        f"{batch_id}: HandoffManifest ID mismatch",
    )
    validation.require(handoff.get("state") == "accepted", f"{batch_id}: HandoffManifest is not accepted")
    producer = handoff.get("producer", {})
    validation.require(isinstance(producer, dict), f"{batch_id}: HandoffManifest producer is invalid")
    if isinstance(producer, dict):
        producer_head = producer.get("head_sha")
        validation.require(producer.get("issue") == issue_number, f"{batch_id}: handoff producer issue mismatch")
        validation.require(
            isinstance(producer_head, str)
            and GIT_SHA_RE.fullmatch(producer_head) is not None
            and git_commit_exists(producer_head),
            f"{batch_id}: handoff producer head is missing from Git history",
        )
    validation.require(
        batch_id in handoff.get("allowed_consumers", []),
        f"{batch_id}: handoff does not allow this consumer",
    )
    environment = str(manifest.get("corpus", {}).get("environment", "")).lower()
    required_environment = (
        "production-shadow"
        if "production" in environment
        else "staging"
        if "staging" in environment
        else "ci"
        if "ci" in environment
        else "local"
    )
    validation.require(
        required_environment in handoff.get("allowed_environments", []),
        f"{batch_id}: handoff does not allow environment {required_environment}",
    )
    evidence = handoff.get("evidence")
    validation.require(isinstance(evidence, list) and bool(evidence), f"{batch_id}: handoff evidence is missing")
    evidence_payloads: list[dict[str, Any]] = []
    if isinstance(evidence, list):
        for index, reference in enumerate(evidence):
            loaded_evidence = validate_file_reference(
                validation,
                owner=f"{batch_id}: handoff evidence[{index}]",
                reference=reference,
            )
            if loaded_evidence is not None:
                evidence_payloads.append(loaded_evidence[1])
    producer_batch = producer.get("batch_id") if isinstance(producer, dict) else None
    producer_head = producer.get("head_sha") if isinstance(producer, dict) else None
    for index, payload in enumerate(evidence_payloads):
        validate_rung_evidence(
            validation,
            owner=f"{batch_id}: handoff evidence[{index}]",
            evidence=payload,
            producer_batch=producer_batch,
            producer_head=producer_head,
        )
    verification = handoff.get("verification", {})
    producer_owner = producer.get("owner") if isinstance(producer, dict) else None
    validation.require(isinstance(verification, dict), f"{batch_id}: handoff verification is invalid")
    if isinstance(verification, dict):
        reviewer = verification.get("reviewer")
        validation.require(bool(reviewer), f"{batch_id}: handoff reviewer is missing")
        validation.require(reviewer != producer_owner, f"{batch_id}: handoff reviewer must be independent")
        validation.require(bool(verification.get("accepted_at")), f"{batch_id}: handoff acceptance time is missing")
        validation.require(bool(verification.get("attestation_ref")), f"{batch_id}: handoff attestation is missing")
        validation.require(
            verification.get("evidence_sha256") == canonical_sha256(evidence),
            f"{batch_id}: handoff evidence set hash mismatch",
        )
    revocation = handoff.get("revocation", {})
    validation.require(
        isinstance(revocation, dict) and all(value is None for value in revocation.values()),
        f"{batch_id}: accepted handoff carries revocation state",
    )


def validate_rung_evidence(
    validation: Validation,
    *,
    owner: str,
    evidence: dict[str, Any],
    producer_batch: Any,
    producer_head: Any,
) -> None:
    validation.require(set(evidence) == RUNG_EVIDENCE_FIELDS, f"{owner}: RungEvidence fields do not match schema v1")
    validation.require(evidence.get("schema_version") == 1, f"{owner}: unsupported RungEvidence schema")
    content = {key: value for key, value in evidence.items() if key != "evidence_id"}
    evidence_id = evidence.get("evidence_id")
    validation.require(
        isinstance(evidence_id, str)
        and re.fullmatch(r"rung-evidence:[a-zA-Z0-9._-]+:[0-9a-f]{64}", evidence_id) is not None
        and evidence_id.rsplit(":", maxsplit=1)[-1] == canonical_sha256(content),
        f"{owner}: RungEvidence ID mismatch",
    )
    validation.require(evidence.get("batch_id") == producer_batch, f"{owner}: producer batch mismatch")
    validation.require(evidence.get("producer_head_sha") == producer_head, f"{owner}: producer head mismatch")
    validation.require(
        isinstance(evidence.get("manifest_sha256"), str)
        and SHA256_RE.fullmatch(evidence["manifest_sha256"]) is not None,
        f"{owner}: manifest SHA-256 is invalid",
    )
    validation.require(evidence.get("accepted_rung") in RUNGS, f"{owner}: accepted rung is invalid")
    validation.require(
        isinstance(evidence.get("base_sha"), str) and GIT_SHA_RE.fullmatch(evidence["base_sha"]) is not None,
        f"{owner}: base SHA is invalid",
    )
    commands = evidence.get("commands")
    commands_are_valid = isinstance(commands, list) and bool(commands)
    validation.require(commands_are_valid, f"{owner}: command evidence is missing")
    if isinstance(commands, list):
        for index, report in enumerate(commands):
            report_is_valid = (
                isinstance(report, dict)
                and set(report) == {"command", "exit_code", "output_sha256"}
                and isinstance(report.get("command"), str)
                and bool(report["command"].strip())
                and report.get("exit_code") == 0
                and isinstance(report.get("output_sha256"), str)
                and SHA256_RE.fullmatch(report["output_sha256"]) is not None
            )
            validation.require(report_is_valid, f"{owner}: command report[{index}] is invalid")
    negative_controls = evidence.get("negative_controls")
    validation.require(
        isinstance(negative_controls, list)
        and bool(negative_controls)
        and all(isinstance(control, str) and bool(control.strip()) for control in negative_controls),
        f"{owner}: negative-control evidence is missing",
    )
    validation.require(evidence.get("stable_handoff") is False, f"{owner}: rung evidence cannot be a stable handoff")
    created_at = evidence.get("created_at")
    created_at_is_valid = False
    if isinstance(created_at, str):
        try:
            created_at_is_valid = datetime.fromisoformat(created_at.replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            pass
    validation.require(created_at_is_valid, f"{owner}: creation time is invalid")


def validate_corpus(validation: Validation, batch_id: str, manifest: dict[str, Any]) -> None:
    corpus = manifest.get("corpus", {})
    relative_path = corpus.get("manifest_path") if isinstance(corpus, dict) else None
    expected_hash = corpus.get("sha256") if isinstance(corpus, dict) else None
    validation.require(isinstance(relative_path, str) and bool(relative_path), f"{batch_id}: corpus path is missing")
    validation.require(
        isinstance(expected_hash, str) and SHA256_RE.fullmatch(expected_hash) is not None,
        f"{batch_id}: corpus SHA-256 is invalid",
    )
    if not isinstance(relative_path, str) or not relative_path:
        return
    path = repo_path(relative_path)
    validation.require(path is not None, f"{batch_id}: corpus path escapes the repository")
    if path is None:
        return
    validation.require(path.is_file(), f"{batch_id}: corpus manifest does not exist")
    if path.is_file():
        validation.require(sha256(path) == expected_hash, f"{batch_id}: corpus bytes do not match the frozen hash")


def validate_integration_lease(
    validation: Validation,
    *,
    batch_id: str,
    manifest: dict[str, Any],
    changed_integration_paths: tuple[str, ...],
    base_sha: str,
    now: datetime | None = None,
) -> None:
    if not changed_integration_paths:
        return
    paths = manifest.get("paths", {})
    loaded = validate_file_reference(
        validation,
        owner=f"{batch_id}: integration lease",
        reference=paths.get("lease_manifest") if isinstance(paths, dict) else None,
    )
    if loaded is None:
        return
    _path, lease = loaded
    validation.require(set(lease) == LEASE_FIELDS, f"{batch_id}: integration lease fields do not match schema v1")
    validation.require(lease.get("schema_version") == 1, f"{batch_id}: unsupported integration lease schema")
    expected_id = (
        f"integration-lease:{canonical_sha256({key: value for key, value in lease.items() if key != 'lease_id'})}"
    )
    validation.require(lease.get("lease_id") == expected_id, f"{batch_id}: integration lease ID mismatch")
    validation.require(lease.get("batch_id") == batch_id, f"{batch_id}: integration lease names another batch")
    validation.require(lease.get("state") == "active", f"{batch_id}: integration lease is not active")
    validation.require(lease.get("owner") == paths.get("lease_owner"), f"{batch_id}: integration lease owner mismatch")
    validation.require(lease.get("base_sha") == base_sha, f"{batch_id}: integration lease base SHA is stale")
    lease_paths = lease.get("paths", [])
    lease_paths_are_strings = isinstance(lease_paths, list) and all(
        isinstance(path, str) and valid_repo_pattern(path) for path in lease_paths
    )
    validation.require(
        lease_paths_are_strings
        and bool(lease_paths)
        and isinstance(lease_paths, list)
        and len(lease_paths) == len(set(lease_paths)),
        f"{batch_id}: integration lease paths are invalid",
    )
    for changed_path in changed_integration_paths:
        validation.require(
            isinstance(lease_paths, list) and matches_any(changed_path, lease_paths),
            f"{batch_id}: integration lease does not cover {changed_path!r}",
        )
    expires_at = lease.get("expires_at")
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        expiry = None
    validation.require(expiry is not None and expiry.tzinfo is not None, f"{batch_id}: lease expiry is invalid")
    if expiry is not None and expiry.tzinfo is not None:
        validation.require(expiry > (now or datetime.now(UTC)), f"{batch_id}: integration lease is expired")


def validate_status_transition(
    validation: Validation,
    *,
    batch_id: str,
    base_manifest: dict[str, Any],
    manifest: dict[str, Any],
) -> str | None:
    before = base_manifest.get("status")
    after = manifest.get("status")
    allowed = {
        ("queued", "prepared"),
        ("prepared", "active"),
        ("active", "active"),
        ("active", "blocked"),
        ("active", "cancelled"),
        ("active", "done"),
        ("blocked", "active"),
        ("blocked", "cancelled"),
        ("done", "done"),
    }
    validation.require((before, after) in allowed, f"{batch_id}: invalid batch transition {before!r} -> {after!r}")
    if (before, after) == ("done", "done"):
        for field in ("last_accepted_rung", "target_rung", "terminal_rung"):
            validation.require(
                manifest.get(field) == base_manifest.get(field),
                f"{batch_id}: corrective terminal rerun cannot change {field}",
            )
        return manifest.get("last_accepted_rung") if manifest.get("last_accepted_rung") in RUNGS else None
    if (before, after) == ("queued", "prepared"):
        validation.require(
            manifest.get("last_accepted_rung") == base_manifest.get("last_accepted_rung")
            and manifest.get("target_rung") == base_manifest.get("target_rung"),
            f"{batch_id}: preparation cannot accept or advance a rung",
        )
        return None
    if after not in {"active", "done"}:
        return None
    accepted_rung = base_manifest.get("target_rung")
    validation.require(
        manifest.get("last_accepted_rung") == accepted_rung,
        f"{batch_id}: PR must accept exactly its base manifest target rung",
    )
    terminal = manifest.get("terminal_rung")
    expected_target = (
        accepted_rung
        if accepted_rung == terminal
        else RUNGS[RUNGS.index(accepted_rung) + 1]
        if accepted_rung in RUNGS
        else None
    )
    validation.require(
        manifest.get("target_rung") == expected_target,
        f"{batch_id}: PR may advance the target by at most one rung",
    )
    validation.require(
        (after == "done") == (accepted_rung == terminal),
        f"{batch_id}: only terminal-rung acceptance may mark the batch done",
    )
    return accepted_rung if isinstance(accepted_rung, str) else None


def validate_pr_paths(
    validation: Validation,
    *,
    batch_id: str,
    manifest_path: str,
    manifest: dict[str, Any],
    changed_paths: tuple[str, ...],
    base_sha: str,
) -> None:
    paths = manifest.get("paths", {})
    writable = paths.get("writable", []) if isinstance(paths, dict) else []
    read_only = paths.get("read_only", []) if isinstance(paths, dict) else []
    forbidden = paths.get("forbidden", []) if isinstance(paths, dict) else []
    integration = paths.get("integration_surface", []) if isinstance(paths, dict) else []
    administrative = {manifest_path, str(GRAPH_PATH.relative_to(ROOT))}
    changed_integration: list[str] = []
    for path in changed_paths:
        if path in administrative:
            continue
        validation.require(not matches_any(path, forbidden), f"{batch_id}: changed forbidden path {path!r}")
        validation.require(not matches_any(path, read_only), f"{batch_id}: changed read-only path {path!r}")
        validation.require(matches_any(path, writable), f"{batch_id}: changed path is outside writable scope: {path!r}")
        if matches_any(path, integration) or requires_integration_lease(path):
            changed_integration.append(path)
    validate_integration_lease(
        validation,
        batch_id=batch_id,
        manifest=manifest,
        changed_integration_paths=tuple(changed_integration),
        base_sha=base_sha,
    )


def validate_new_batch_registration(
    validation: Validation,
    *,
    batch_id: str,
    graph: dict[str, Any],
    base_graph: dict[str, Any],
    manifest_path: str,
    manifest: dict[str, Any],
    changed_paths: tuple[str, ...],
) -> None:
    graph_batch = graph.get("batches", {}).get(batch_id, {})
    activation = manifest.get("activation")
    owners = manifest.get("owners")
    validation.require(manifest.get("revision") == 1, f"{batch_id}: new batch must start at revision 1")
    validation.require(
        manifest.get("status") == "queued"
        and manifest.get("last_accepted_rung") is None
        and manifest.get("target_rung") == "E0",
        f"{batch_id}: new batch must register as queued at E0 without accepted evidence",
    )
    validation.require(
        isinstance(activation, dict) and activation.get("base_sha") is None,
        f"{batch_id}: queued registration cannot pin an implementation base",
    )
    validation.require(
        isinstance(owners, dict) and owners.get("reviewer") is None,
        f"{batch_id}: queued registration cannot pre-assign a reviewer",
    )
    validation.require(
        graph_batch.get("status") == "queued" and graph_batch.get("target_rung") == "E0",
        f"{batch_id}: graph registration must be queued at E0",
    )
    base_without_batch = json.loads(json.dumps(base_graph))
    candidate_without_batch = json.loads(json.dumps(graph))
    candidate_without_batch.get("batches", {}).pop(batch_id, None)
    validation.require(
        base_without_batch == candidate_without_batch,
        f"{batch_id}: registration changed unrelated Vision graph content",
    )
    allowed_paths = {manifest_path, str(GRAPH_PATH.relative_to(ROOT))}
    validation.require(
        set(changed_paths) == allowed_paths,
        f"{batch_id}: registration may only add its manifest and Vision graph entry",
    )


def validate_gate0_pr_advance(
    validation: Validation,
    *,
    base_sha: str,
    changed_paths: tuple[str, ...],
    allow_blocked_gate_candidate: bool,
) -> PullRequestAdvance | None:
    label = "Gate 0 v4 aggregate candidate"
    manifest_path = repo_path(GATE0_MANIFEST_PATH)
    validation.require(
        manifest_path is not None and manifest_path.is_file() and not manifest_path.is_symlink(),
        f"{label}: manifest is missing from the PR head",
    )
    if manifest_path is None or not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    manifest = load_json(manifest_path)
    validation.require(isinstance(manifest, dict), f"{label}: manifest must be an object")
    if not isinstance(manifest, dict):
        return None
    validation.require(
        manifest.get("integration_base_sha") == base_sha,
        f"{label}: integration_base_sha does not match the PR base",
    )
    paths = manifest.get("paths")
    valid_paths = (
        isinstance(paths, list)
        and bool(paths)
        and all(isinstance(pattern, str) and PATH_PATTERN_RE.fullmatch(pattern) is not None for pattern in paths)
        and len(paths) == len(set(paths))
    )
    validation.require(valid_paths, f"{label}: paths must be unique exact files or terminal /** patterns")
    if valid_paths:
        assert isinstance(paths, list)
        validate_gate0_candidate_paths(validation, paths)
    non_manifest_paths = tuple(path for path in changed_paths if path != GATE0_MANIFEST_PATH)
    validation.require(bool(non_manifest_paths), f"{label}: aggregate PR changes only its manifest")
    if isinstance(paths, list):
        for path in non_manifest_paths:
            validation.require(
                matches_any(path, paths),
                f"{label}: changed path is outside manifest authorization: {path!r}",
            )
    changed_controls = sorted(set(non_manifest_paths) & GATE0_AUTHORIZATION_CONTROL_PATHS)
    if changed_controls and not allow_blocked_gate_candidate:
        # The control plane must land through a separately reviewed governance PR.
        # The Gate candidate then rebases and removes these paths before acceptance.
        validation.require(
            False,
            f"{label}: accepted candidate modifies authorization controls; "
            f"land them separately, rebase, and remove them from this diff: {changed_controls}",
        )
    gate_result = validate_gate0_candidate(
        Path(GATE0_MANIFEST_PATH),
        root=ROOT,
        check_live_comments=not allow_blocked_gate_candidate,
        require_accepted=not allow_blocked_gate_candidate,
    )
    for error in gate_result.errors:
        validation.require(False, f"{label}: {error}")
    if validation.errors:
        return None
    return PullRequestAdvance(
        batch_id="gate-0-v4",
        manifest_path=GATE0_MANIFEST_PATH,
        base_manifest={},
        manifest=manifest,
        accepted_rung=None,
        changed_paths=changed_paths,
        kind="gate_candidate",
    )


def validate_pr_advance(
    validation: Validation,
    *,
    graph: dict[str, Any],
    base_sha: str,
    head_sha: str,
    allow_blocked_gate_candidate: bool = False,
) -> PullRequestAdvance | None:
    valid_coordinates = all(GIT_SHA_RE.fullmatch(value) is not None for value in (base_sha, head_sha))
    validation.require(valid_coordinates, "PR base/head must be full Git SHAs")
    if not valid_coordinates:
        return None
    validation.require(git_commit_exists(base_sha), "PR base commit is missing from Git history")
    validation.require(git_commit_exists(head_sha), "PR head commit is missing from Git history")
    merge_base = git_merge_base(base_sha, head_sha)
    validation.require(merge_base == base_sha, "PR is stale: merge-base does not equal the declared current base")
    changed_paths = git_changed_paths(base_sha, head_sha)
    validation.require(
        changed_paths is not None and bool(changed_paths), "PR changed-path diff is empty or unavailable"
    )
    if changed_paths is None:
        return None
    changed_manifests = tuple(
        path for path in changed_paths if path.startswith(MANIFEST_PREFIX) and path.endswith(".json")
    )
    gate0_manifest_changed = GATE0_MANIFEST_PATH in changed_paths
    validation.require(
        not (gate0_manifest_changed and changed_manifests),
        "PR cannot mix the Gate 0 aggregate manifest with capability-batch manifests",
    )
    if gate0_manifest_changed and changed_manifests:
        return None
    if gate0_manifest_changed:
        return validate_gate0_pr_advance(
            validation,
            base_sha=base_sha,
            changed_paths=changed_paths,
            allow_blocked_gate_candidate=allow_blocked_gate_candidate,
        )
    if not changed_manifests:
        validation.require(
            all(matches_any(path, GOVERNANCE_CONTROL_PATHS) for path in changed_paths),
            "non-governance PR must advance exactly one capability-batch manifest",
        )
        return None
    validation.require(len(changed_manifests) == 1, "PR must advance exactly one capability-batch manifest")
    if len(changed_manifests) != 1:
        return None
    manifest_path = changed_manifests[0]
    matching_batches = [
        batch_id for batch_id, batch in graph.get("batches", {}).items() if batch.get("manifest") == manifest_path
    ]
    validation.require(len(matching_batches) == 1, "changed manifest is not the unique graph-owned batch manifest")
    if len(matching_batches) != 1:
        return None
    batch_id = matching_batches[0]
    candidate_manifest_path = repo_path(manifest_path)
    validation.require(
        candidate_manifest_path is not None and candidate_manifest_path.is_file(),
        f"{batch_id}: changed manifest is missing from the PR head",
    )
    if candidate_manifest_path is None or not candidate_manifest_path.is_file():
        return None
    manifest = load_json(candidate_manifest_path)
    base_graph = git_json(base_sha, str(GRAPH_PATH.relative_to(ROOT)))
    validation.require(isinstance(base_graph, dict), f"{batch_id}: base Vision graph is unavailable")
    if not isinstance(base_graph, dict):
        return None
    base_batch = base_graph.get("batches", {}).get(batch_id)
    if not isinstance(base_batch, dict):
        validate_new_batch_registration(
            validation,
            batch_id=batch_id,
            graph=graph,
            base_graph=base_graph,
            manifest_path=manifest_path,
            manifest=manifest,
            changed_paths=changed_paths,
        )
        return PullRequestAdvance(
            batch_id=batch_id,
            manifest_path=manifest_path,
            base_manifest={},
            manifest=manifest,
            accepted_rung=None,
            changed_paths=changed_paths,
        )
    base_manifest_path = base_batch.get("manifest")
    base_manifest = git_json(base_sha, base_manifest_path) if isinstance(base_manifest_path, str) else None
    validation.require(isinstance(base_manifest, dict), f"{batch_id}: base manifest is unavailable")
    if not isinstance(base_manifest, dict):
        return None
    validation.require(
        manifest.get("revision") == base_manifest.get("revision", 0) + 1,
        f"{batch_id}: manifest revision must increase by exactly one",
    )
    base_graph_without_batch = json.loads(json.dumps(base_graph))
    graph_without_batch = json.loads(json.dumps(graph))
    base_graph_without_batch["batches"].pop(batch_id, None)
    graph_without_batch["batches"].pop(batch_id, None)
    validation.require(
        base_graph_without_batch == graph_without_batch,
        f"{batch_id}: PR changed unrelated Vision graph content",
    )
    activation = manifest.get("activation", {})
    validation.require(
        isinstance(activation, dict) and activation.get("base_sha") == base_sha,
        f"{batch_id}: manifest base SHA does not match the PR base",
    )
    validate_corpus(validation, batch_id, manifest)
    accepted_rung = validate_status_transition(
        validation,
        batch_id=batch_id,
        base_manifest=base_manifest,
        manifest=manifest,
    )
    if base_manifest.get("status") == "queued" and manifest.get("status") == "prepared":
        preparation_paths = {
            manifest_path,
            str(GRAPH_PATH.relative_to(ROOT)),
            manifest.get("corpus", {}).get("manifest_path"),
        }
        validation.require(
            all(path in preparation_paths for path in changed_paths),
            f"{batch_id}: preparation PR may only freeze governance and corpus manifest bytes",
        )
    validate_pr_paths(
        validation,
        batch_id=batch_id,
        manifest_path=manifest_path,
        manifest=manifest,
        changed_paths=changed_paths,
        base_sha=base_sha,
    )
    return PullRequestAdvance(
        batch_id=batch_id,
        manifest_path=manifest_path,
        base_manifest=base_manifest,
        manifest=manifest,
        accepted_rung=accepted_rung,
        changed_paths=changed_paths,
    )


def _default_command_runner(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def execute_acceptance_commands(
    validation: Validation,
    *,
    advance: PullRequestAdvance,
    head_sha: str,
    output_path: Path,
    runner: CommandRunner = _default_command_runner,
) -> None:
    gate_acceptance = advance.kind == "gate_candidate" and advance.manifest.get("status") == "accepted"
    if advance.accepted_rung is None and not gate_acceptance:
        return
    current_head = run_git("rev-parse", "HEAD")
    validation.require(
        current_head.returncode == 0 and current_head.stdout.strip() == head_sha,
        f"{advance.batch_id}: acceptance commands are not running on the exact PR head",
    )
    if validation.errors:
        return
    acceptance = advance.manifest.get("acceptance")
    commands = acceptance.get("commands") if isinstance(acceptance, dict) else None
    validation.require(
        isinstance(commands, list)
        and bool(commands)
        and all(isinstance(command, str) and command for command in commands),
        f"{advance.batch_id}: acceptance commands are missing",
    )
    if validation.errors:
        return
    assert isinstance(commands, list)
    reports: list[dict[str, Any]] = []
    for command in commands:
        result = runner(command)
        output_digest = hashlib.sha256(f"{result.stdout}\n{result.stderr}".encode()).hexdigest()
        reports.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "output_sha256": output_digest,
            }
        )
        validation.require(result.returncode == 0, f"{advance.batch_id}: acceptance command failed: {command}")
    if validation.errors:
        return
    if gate_acceptance:
        base_sha = advance.manifest["integration_base_sha"]
        negative_controls: list[str] = []
    else:
        base_sha = advance.manifest["activation"]["base_sha"]
        negative_controls = advance.manifest["acceptance"]["negative_controls"]
    evidence = {
        "schema_version": 1,
        "batch_id": advance.batch_id,
        "manifest_sha256": sha256(ROOT / advance.manifest_path),
        "accepted_rung": advance.accepted_rung,
        "base_sha": base_sha,
        "producer_head_sha": head_sha,
        "commands": reports,
        "negative_controls": negative_controls,
        "stable_handoff": False,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    evidence_kind = "gate-evidence" if gate_acceptance else "rung-evidence"
    evidence_id = f"{evidence_kind}:{advance.batch_id}:{canonical_sha256(evidence)}"
    output = {"evidence_id": evidence_id, **evidence}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
            mirrored_status = "queued" if batch["status"] == "prepared" else batch["status"]
            expected_batch_label = f"batch:{mirrored_status}"
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


def validate(
    github_path: Path | None,
    *,
    pr_base_sha: str | None = None,
    pr_head_sha: str | None = None,
    execute_acceptance: bool = False,
    evidence_output: Path | None = None,
    allow_blocked_gate_candidate: bool = False,
) -> Validation:
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

    advance = None
    validation.require(
        (pr_base_sha is None) == (pr_head_sha is None),
        "PR validation requires both --pr-base-sha and --pr-head-sha",
    )
    if pr_base_sha is not None and pr_head_sha is not None:
        advance = validate_pr_advance(
            validation,
            graph=graph,
            base_sha=pr_base_sha,
            head_sha=pr_head_sha,
            allow_blocked_gate_candidate=allow_blocked_gate_candidate,
        )
    if github_path is not None:
        github_graph = graph
        if pr_base_sha is not None and (advance is None or advance.base_manifest):
            base_graph = git_json(pr_base_sha, str(GRAPH_PATH.relative_to(ROOT)))
            validation.require(isinstance(base_graph, dict), "base Vision graph is unavailable for live parity")
            if isinstance(base_graph, dict):
                github_graph = base_graph
        validate_github(validation, github_graph, load_json(github_path))
    if execute_acceptance:
        validation.require(evidence_output is not None, "--execute-acceptance requires --evidence-output")
        if advance is not None and evidence_output is not None and not validation.errors:
            execute_acceptance_commands(
                validation,
                advance=advance,
                head_sha=pr_head_sha or "",
                output_path=evidence_output,
            )
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-issues", type=Path, help="JSON exported by gh issue list")
    parser.add_argument("--pr-base-sha", help="Exact current pull-request base commit")
    parser.add_argument("--pr-head-sha", help="Exact pull-request head commit")
    parser.add_argument("--execute-acceptance", action="store_true")
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument(
        "--allow-blocked-gate-candidate",
        action="store_true",
        help="Allow a structurally valid blocked Gate 0 candidate only for a draft PR",
    )
    args = parser.parse_args()
    validation = validate(
        args.github_issues,
        pr_base_sha=args.pr_base_sha,
        pr_head_sha=args.pr_head_sha,
        execute_acceptance=args.execute_acceptance,
        evidence_output=args.evidence_output,
        allow_blocked_gate_candidate=args.allow_blocked_gate_candidate,
    )
    if validation.errors:
        for error in validation.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    scope_parts = ["offline graph"]
    if args.github_issues:
        scope_parts.append("live GitHub parity")
    if args.pr_base_sha:
        scope_parts.append("pull-request authorization")
    scope = ", ".join(scope_parts)
    print(f"Delivery governance check passed: {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
