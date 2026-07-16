#!/usr/bin/env python3
"""Validate an immutable, versioned Gate 0 candidate and its acceptance boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("governance/gate0/manifest-v4.json")
V4_FROZEN_TREE_SHA256 = "c1463ee2e735ee9895b9f6d6b746d90cda7fb34a3812d064724c907bdcbe0c8b"
V4_FROZEN_COMMIT_SHA = "8bfcd328258e79e14977c60525769ce3302f2ea4"
V4_FROZEN_PROOF_PATH = "governance/gate0/v4-frozen-tree-proof.v1.json"
V4_FROZEN_PROOF_SHA256 = "cb231578a36ad68ccd8b40ea2ad5c7085b8eadfdba6e9194e263659738310627"
V4_MANIFEST_PATHS = (
    "apps/data-engine/tests/batches/mvp_medium_validation/test_e1_slice.py",
    "apps/data-engine/tests/batches/mvp_medium_validation/test_e2_slice.py",
    "apps/data-engine/tests/batches/mvp_medium_validation/test_e3_slice.py",
    "docs/architecture-contract-closure.md",
    "init.md",
    "governance/gate0/**",
    "governance/schemas/gate0-candidate-manifest.schema.json",
    "libs/contracts/src/truealpha_contracts/__init__.py",
    "libs/contracts/src/truealpha_contracts/policy_bundle.py",
    "libs/contracts/tests/test_policy_bundle.py",
    "libs/runtime/tests/test_gate0_candidate.py",
    "tools/check_gate0_candidate.py",
)
SUCCESSOR_MANIFEST_PATHS = (
    *V4_MANIFEST_PATHS,
    "libs/runtime/tests/test_delivery_governance.py",
    "tools/check_delivery_governance.py",
)
EXPECTED_MANIFEST_PATHS = V4_MANIFEST_PATHS
SUCCESSOR_BINDINGS = {
    "authoritative_architecture": "init.md",
    "stable_contract_export": "libs/contracts/src/truealpha_contracts/__init__.py",
    "candidate_validator": "tools/check_gate0_candidate.py",
    "delivery_validator": "tools/check_delivery_governance.py",
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSIONED_MANIFEST_RE = re.compile(r"^governance/gate0/manifest-v[0-9]+\.json$")
COMMENT_URL_RE = re.compile(
    r"^https://github\.com/wangzitian0/truealpha/issues/(?P<issue>[0-9]+)#issuecomment-(?P<comment>[0-9]+)$"
)

ISSUES = (57, 58, 59, 60, 61)
CANDIDATE_ISSUES = (59, 60, 61)
EXPECTED_DEPENDENCIES = {57: (), 58: (), 59: (57, 58), 60: (59,), 61: (59, 60)}
EXPECTED_KINDS = {
    57: "accepted_capability_evidence",
    58: "accepted_capability_evidence",
    59: "research_semantics_candidate",
    60: "source_readiness_candidate",
    61: "policy_bundle_candidate",
}
EXPECTED_ARTIFACT_TYPES = {
    59: "research-semantics-candidate",
    60: "source-readiness-candidate",
    61: "gate0-policy-bundle-candidate",
}
EXPECTED_ALIASES = frozenset(
    {
        "analyst-backtest",
        "etf-virtual-company",
        "gross-profit-per-employee",
        "large-model-value-v0",
        "peg",
        "supply-chain",
        "theme-purity",
        "three-tier-valuation",
    }
)
GOLDEN_ROLE_SPECS = {
    "input": ("public-development-golden-input", "input"),
    "expected": ("public-development-golden-expected-output", "expected"),
    "provenance": ("public-development-golden-provenance", "provenance"),
}
FORBIDDEN_PUBLIC_GOLDEN_KEYS = frozenset(
    {
        "approval",
        "approvals",
        "attestation",
        "attestations",
        "custodian",
        "holdout_labels",
        "protected_labels",
        "reviewer",
    }
)
EXPECTED_ISSUERS = frozenset(
    {
        "AbbVie",
        "Alphabet",
        "Amazon",
        "Apple",
        "Berkshire Hathaway",
        "Broadcom",
        "Costco",
        "Eli Lilly",
        "Exxon Mobil",
        "JPMorgan Chase",
        "Johnson & Johnson",
        "Mastercard",
        "Meta Platforms",
        "Micron Technology",
        "Microsoft",
        "NVIDIA",
        "Netflix",
        "Tesla",
        "Visa",
        "Walmart",
    }
)
EXPECTED_CUSIPS = frozenset(
    {
        "00287Y109",
        "02079K107",
        "02079K305",
        "023135106",
        "037833100",
        "084670702",
        "11135F101",
        "22160K105",
        "30231G102",
        "30303M102",
        "46625H100",
        "478160104",
        "532457108",
        "57636Q104",
        "594918104",
        "595112103",
        "64110L106",
        "67066G104",
        "88160R101",
        "92826C839",
        "931142103",
    }
)
EXPECTED_MINIMUMS = {
    "issuers": 20,
    "instruments": 21,
    "funds": 1,
    "themes": 1,
    "analysts": 20,
    "scenarios": 1,
    "screens": 1,
    "rankings": 1,
    "strategies": 1,
    "canonical_questions": 8,
}
EXPECTED_SOURCE_DOMAINS = frozenset(
    {
        "universe_membership",
        "issuer_security_listing_identity",
        "financial_facts",
        "headcount",
        "company_guidance",
        "analyst_events_and_consensus",
        "etf_holdings",
        "segment_revenue",
        "supply_chain_relationships",
        "daily_prices",
        "fx_rates",
        "corporate_actions",
    }
)
EXPECTED_ENVIRONMENTS = frozenset({"local_dev", "local_test", "github_ci", "staging", "production"})
EXPECTED_ATTESTATIONS = {
    "issue59_product_owner": 59,
    "issue59_independent_review": 59,
    "issue59_holdout_custody": 59,
    "issue59_known_reference": 59,
    "issue60_rights_authority": 60,
    "issue60_budget_authority": 60,
    "issue60_live_probe_owner": 60,
    "issue61_product_owner_policy": 61,
    "issue61_independent_slo_review": 61,
}
PRODUCT_OWNER_LOGIN = "wangzitian0"
ATTESTATION_BODY_TERMS = {
    "issue59_product_owner": (
        (r"\bproduct[- ]owner\b",),
        (r"\bresearch semantics\b",),
        (r"\bpublic golden",),
        (r"\bholdout\b",),
    ),
    "issue59_independent_review": (
        (r"\bindependent review", r"\bindependent reviewer\b"),
        (r"\borganization\b",),
        (r"\bconflict",),
    ),
    "issue59_holdout_custody": (
        (r"\bcustodian\b",),
        (r"\bauthorized evaluator\b",),
        (r"\bprotected store\b",),
        (r"\bcandidate author",),
        (r"\bno access\b", r"\bnot (?:have|granted) access\b"),
    ),
    "issue59_known_reference": (
        (r"\bindependent owner\b",),
        (r"\bimmutable artifact\b",),
        (r"\bexpected (?:output|result)\b",),
        (r"\bbroken (?:engine )?(?:output|result)\b",),
    ),
    "issue60_rights_authority": (
        (r"\brights authority\b",),
        (r"\bauthorized\b",),
        (r"\bpermissions?\b",),
        (r"\brights artifact\b",),
    ),
    "issue60_budget_authority": (
        (r"\bbudget authority\b",),
        (r"\bauthorized\b",),
        (r"\bbudget artifact\b",),
        (r"\bceiling\b",),
    ),
    "issue60_live_probe_owner": (
        (r"\blive probe owner\b",),
        (r"\benvironment\b",),
        (r"\bpartition\b",),
        (r"\bprobe artifact\b",),
    ),
    "issue61_product_owner_policy": (
        (r"\bproduct[- ]owner\b",),
        (r"\bpolicy\b",),
        (r"\bwall[- ]clock\b",),
        (r"\bthreshold",),
    ),
    "issue61_independent_slo_review": (
        (r"\bindependent\b",),
        (r"\breviewer\b",),
        (r"\borganization\b",),
        (r"\bconflict",),
        (r"\bSLO\b",),
    ),
}

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "manifest_version",
        "gate_issue",
        "status",
        "integration_branch",
        "integration_base_sha",
        "included_issues",
        "dependency_order",
        "candidate_payload_sha256",
        "candidate_tree_sha256",
        "paths",
        "artifacts",
        "external_attestations",
        "merge_policy",
        "acceptance",
        "blocking_reasons",
    }
)
SUCCESSOR_MANIFEST_FIELDS = MANIFEST_FIELDS | {"predecessor_manifest", "integration_bindings"}
PREDECESSOR_MANIFEST_FIELDS = frozenset(
    {"manifest_id", "manifest_version", "path", "sha256", "candidate_commit_sha", "candidate_tree_proof"}
)
FROZEN_PROOF_REFERENCE_FIELDS = frozenset({"path", "sha256"})
FROZEN_TREE_PROOF_FIELDS = frozenset(
    {"schema_version", "proof_id", "candidate_commit_sha", "manifest", "candidate_tree_sha256", "paths"}
)
INTEGRATION_BINDING_FIELDS = frozenset({"role", "path", "sha256"})
FOUNDATION_ARTIFACT_FIELDS = frozenset({"issue", "kind", "path", "sha256", "state"})
CANDIDATE_ARTIFACT_FIELDS = FOUNDATION_ARTIFACT_FIELDS | {"depends_on"}
EXTERNAL_ATTESTATION_FIELDS = frozenset({"key", "issue", "target_sha256", "status", "ref"})
INTERNAL_ATTESTATION_FIELDS = {
    59: {
        "product_owner": frozenset({"status", "approved_packet_sha256", "ref"}),
        "independent_review": frozenset({"status", "reviewer", "organization", "conflicts", "ref"}),
        "holdout_custody": frozenset({"status", "custodian", "authorized_evaluator", "protected_store_ref"}),
        "known_reference": frozenset(
            {"status", "independent_owner", "artifact_ref", "expected_output_ref", "broken_engine_result_ref"}
        ),
    },
    60: {
        "product_owner_source_plan": frozenset({"status", "ref"}),
        "rights_authority": frozenset({"status", "approver", "ref"}),
        "budget_authority": frozenset({"status", "approver", "ref"}),
        "live_probe_owner": frozenset({"status", "owner", "artifact_ref"}),
    },
    61: {
        "product_owner_thresholds_and_wall_clock": frozenset({"status", "ref"}),
        "independent_slo_review": frozenset({"status", "reviewer", "organization", "ref"}),
        "topt_baseline": frozenset({"status", "ref"}),
        "runbook_owners": frozenset({"status", "owners", "ref"}),
    },
}

ISSUE59_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "artifact_version",
        "issue",
        "state",
        "source_packet",
        "dependencies",
        "scope",
        "catalog",
        "semantics",
        "evaluation",
        "attestations",
        "blocking_reasons",
    }
)
ISSUE60_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "artifact_version",
        "issue",
        "state",
        "depends_on",
        "scope",
        "observed_probe_evidence",
        "source_paths",
        "rights_requirements",
        "budget_requirements",
        "runtime_requirements",
        "attestations",
        "blocking_reasons",
    }
)
ISSUE61_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "artifact_version",
        "issue",
        "state",
        "dependencies",
        "acceptance_boundary",
        "scope",
        "applicability",
        "proposed_module_slos",
        "capture_slo",
        "proposed_natural_refresh_requirements",
        "natural_refresh_exclusions",
        "proposed_consumer_slos",
        "usage_telemetry_slo",
        "planned_demand_compiler",
        "attestations",
        "blocking_reasons",
    }
)

CommentFetcher = Callable[[int], dict[str, Any]]


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]
    blockers: tuple[str, ...]
    accepted: bool

    @property
    def valid(self) -> bool:
        return not self.errors


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return _sha256_bytes(encoded)


def candidate_payload_sha256(manifest: dict[str, Any]) -> str:
    artifacts = manifest.get("artifacts")
    compact: list[dict[str, Any]] = []
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict):
                compact.append({"issue": artifact.get("issue"), "sha256": artifact.get("sha256")})
    payload: dict[str, Any] = {"included_issues": manifest.get("included_issues"), "artifacts": compact}
    if manifest.get("manifest_version", 0) >= 5:
        payload["predecessor_manifest"] = manifest.get("predecessor_manifest")
        payload["integration_bindings"] = manifest.get("integration_bindings")
    return _canonical_sha256(payload)


def path_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern.removesuffix("/**")
        return path.startswith(f"{prefix}/")
    return path == pattern


def manifest_authorized_files(
    root: Path,
    patterns: list[str] | tuple[str, ...],
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[str, ...]:
    manifest_relative = manifest_path.as_posix()
    authorized: set[str] = set()
    for pattern in patterns:
        if pattern.endswith("/**"):
            prefix = pattern.removesuffix("/**")
            directory = root / prefix
            if directory.is_dir():
                for path in directory.rglob("*"):
                    if path.is_symlink():
                        raise ValueError(f"authorized candidate path is a symlink: {path.relative_to(root)}")
                    if path.is_file():
                        authorized.add(path.relative_to(root).as_posix())
        else:
            path = root / pattern
            if path.is_symlink():
                raise ValueError(f"authorized candidate path is a symlink: {pattern}")
            if path.is_file():
                authorized.add(pattern)
    authorized.discard(manifest_relative)
    authorized = {path for path in authorized if VERSIONED_MANIFEST_RE.fullmatch(path) is None}
    return tuple(sorted(authorized))


def candidate_tree_sha256(
    root: Path,
    patterns: list[str] | tuple[str, ...],
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> str:
    digest = hashlib.sha256()
    for relative_path in manifest_authorized_files(root, patterns, manifest_path=manifest_path):
        raw = (root / relative_path).read_bytes()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def frozen_candidate_tree_sha256(commit_sha: str, patterns: list[str] | tuple[str, ...]) -> str:
    """Hash a historical candidate from immutable Git objects, not the current checkout."""
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", commit_sha],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest_paths = {
        path
        for path in listing.stdout.splitlines()
        if any(path_matches(path, pattern) for pattern in patterns) and VERSIONED_MANIFEST_RE.fullmatch(path) is None
    }
    digest = hashlib.sha256()
    for relative_path in sorted(manifest_paths):
        raw = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{commit_sha}:{relative_path}"],
            check=True,
            capture_output=True,
        ).stdout
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def frozen_file_bytes(commit_sha: str, relative_path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{commit_sha}:{relative_path}"],
        check=True,
        capture_output=True,
    ).stdout


def frozen_commit_available(commit_sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _strict_fields(validation: Validation, value: Any, expected: frozenset[str], label: str) -> bool:
    validation.require(isinstance(value, dict), f"{label}: expected an object")
    if not isinstance(value, dict):
        return False
    actual = set(value)
    validation.require(
        actual == expected,
        f"{label}: fields differ; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}",
    )
    return actual == expected


def _nonempty_strings(value: Any) -> bool:
    return (
        isinstance(value, list) and bool(value) and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _contains_forbidden_public_golden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key.lower() in FORBIDDEN_PUBLIC_GOLDEN_KEYS or _contains_forbidden_public_golden_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_public_golden_key(child) for child in value)
    return False


def _repo_file(validation: Validation, root: Path, relative_path: Any, label: str) -> Path | None:
    validation.require(isinstance(relative_path, str) and bool(relative_path), f"{label}: path is missing")
    if not isinstance(relative_path, str) or not relative_path:
        return None
    unresolved_path = root / relative_path
    validation.require(not unresolved_path.is_symlink(), f"{label}: symlink paths are forbidden")
    if unresolved_path.is_symlink():
        return None
    path = unresolved_path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        validation.require(False, f"{label}: path escapes repository")
        return None
    validation.require(path.is_file(), f"{label}: file does not exist: {relative_path}")
    return path if path.is_file() else None


def _validate_frozen_tree_proof(
    validation: Validation,
    *,
    root: Path,
    reference: Any,
    predecessor_path: str,
    predecessor_sha: str,
    predecessor: dict[str, Any],
    predecessor_version: int,
    candidate_commit: str,
) -> dict[str, Any] | None:
    label = "Gate 0 predecessor frozen-tree proof"
    if not _strict_fields(validation, reference, FROZEN_PROOF_REFERENCE_FIELDS, label):
        return None
    assert isinstance(reference, dict)
    proof_path = _repo_file(validation, root, reference.get("path"), label)
    if proof_path is None:
        return None
    validation.require(reference.get("sha256") == _sha256_file(proof_path), f"{label}: file SHA-256 mismatch")
    try:
        proof = _load_json(proof_path)
    except json.JSONDecodeError as error:
        validation.require(False, f"{label}: invalid JSON: {error}")
        return None
    if not _strict_fields(validation, proof, FROZEN_TREE_PROOF_FIELDS, label):
        return None
    assert isinstance(proof, dict)
    manifest_ref = proof.get("manifest")
    _strict_fields(validation, manifest_ref, frozenset({"path", "sha256"}), f"{label} manifest")
    validation.require(proof.get("schema_version") == 1, f"{label}: unsupported schema")
    validation.require(
        proof.get("proof_id") == f"gate-0-v{predecessor_version}-frozen-tree-proof-v1",
        f"{label}: wrong identity",
    )
    validation.require(proof.get("candidate_commit_sha") == candidate_commit, f"{label}: wrong commit")
    validation.require(
        isinstance(manifest_ref, dict)
        and manifest_ref.get("path") == predecessor_path
        and manifest_ref.get("sha256") == predecessor_sha,
        f"{label}: manifest identity mismatch",
    )
    validation.require(
        proof.get("candidate_tree_sha256") == predecessor.get("candidate_tree_sha256"),
        f"{label}: candidate tree mismatch",
    )
    validation.require(proof.get("paths") == predecessor.get("paths"), f"{label}: authorized paths mismatch")
    return proof


def _validate_comment_ref(
    validation: Validation,
    *,
    issue: int,
    comment_id: Any,
    ref: Any,
    expected_sha256: Any,
    label: str,
    fetcher: CommentFetcher | None,
) -> dict[str, Any] | None:
    validation.require(isinstance(comment_id, int) and not isinstance(comment_id, bool), f"{label}: invalid comment ID")
    validation.require(isinstance(ref, str), f"{label}: comment ref must be a string")
    match = COMMENT_URL_RE.fullmatch(ref) if isinstance(ref, str) else None
    validation.require(match is not None, f"{label}: comment ref is not an exact repository issue-comment URL")
    if match is not None:
        validation.require(int(match.group("issue")) == issue, f"{label}: comment ref names another issue")
        validation.require(int(match.group("comment")) == comment_id, f"{label}: comment ref and ID disagree")
    validation.require(
        isinstance(expected_sha256, str) and SHA256_RE.fullmatch(expected_sha256) is not None,
        f"{label}: invalid comment SHA-256",
    )
    if fetcher is None or not isinstance(comment_id, int):
        return None
    try:
        comment = fetcher(comment_id)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        validation.require(False, f"{label}: cannot load live comment: {error}")
        return None
    body = comment.get("body")
    validation.require(isinstance(body, str), f"{label}: live comment body is missing")
    if isinstance(body, str):
        validation.require(_sha256_bytes(body.encode()) == expected_sha256, f"{label}: live comment SHA-256 mismatch")
    validation.require(comment.get("id") == comment_id, f"{label}: live comment ID mismatch")
    validation.require(comment.get("html_url") == ref, f"{label}: live comment URL mismatch")
    return comment


def _github_comment_fetcher(comment_id: int) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", f"repos/wangzitian0/truealpha/issues/comments/{comment_id}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned a non-object comment")
    return value


def _validate_foundation(
    validation: Validation,
    *,
    issue: int,
    path: Path,
    payload: Any,
) -> None:
    label = f"issue #{issue} foundation"
    validation.require(isinstance(payload, dict), f"{label}: evidence payload must be an object")
    if not isinstance(payload, dict):
        return
    validation.require(payload.get("issue") == issue, f"{label}: evidence issue mismatch")
    validation.require(payload.get("state") == "accepted", f"{label}: evidence is not accepted")
    validation.require(payload.get("accepted_rung") == "E2", f"{label}: evidence does not bind terminal rung E2")
    validation.require(
        payload.get("evidence_id", "").startswith(f"capability-evidence:issue-{issue}:v"),
        f"{label}: evidence ID names another issue",
    )
    validation.require(path.name.startswith(f"issue-{issue}."), f"{label}: evidence filename names another issue")


def _validate_scope_59(validation: Validation, scope: Any) -> None:
    expected_fields = frozenset(
        {
            "universe_id",
            "kind",
            "source",
            "minimums",
            "issuers",
            "selected_instrument_cusips",
            "selected_instruments",
            "identity_invariants",
        }
    )
    if not _strict_fields(validation, scope, expected_fields, "issue #59 scope"):
        return
    assert isinstance(scope, dict)
    validation.require(scope.get("universe_id") == "universe:topt-us-2026-03-31", "issue #59: wrong UniverseRef")
    validation.require(scope.get("kind") == "fixed_point_in_time_research_cohort", "issue #59: wrong universe kind")
    source = scope.get("source")
    source_fields = frozenset({"series", "class", "accession", "report_date", "primary_document_sha256"})
    if _strict_fields(validation, source, source_fields, "issue #59 scope source"):
        assert isinstance(source, dict)
        validation.require(source.get("series") == "S000088434", "issue #59: wrong TOPT series")
        validation.require(source.get("class") == "C000254701", "issue #59: wrong TOPT class")
        validation.require(source.get("accession") == "000207169126012475", "issue #59: wrong TOPT accession")
        validation.require(source.get("report_date") == "2026-03-31", "issue #59: wrong TOPT report date")
        validation.require(
            source.get("primary_document_sha256") == "7e46eb6babead70230986162349bb33f27d7af2a51a095b5850340aa0a534934",
            "issue #59: wrong TOPT primary document hash",
        )
    validation.require(scope.get("minimums") == EXPECTED_MINIMUMS, "issue #59: exact scope minimums changed")
    issuers = scope.get("issuers")
    validation.require(
        isinstance(issuers, list) and len(issuers) == 20 and set(issuers) == EXPECTED_ISSUERS,
        "issue #59: exact 20-issuer scope changed",
    )
    cusips = scope.get("selected_instrument_cusips")
    validation.require(
        isinstance(cusips, list) and len(cusips) == 21 and set(cusips) == EXPECTED_CUSIPS,
        "issue #59: exact 21-instrument scope changed",
    )
    instruments = scope.get("selected_instruments")
    validation.require(
        isinstance(instruments, list) and len(instruments) == 21, "issue #59: selected instrument row count must be 21"
    )
    if isinstance(instruments, list):
        for index, instrument in enumerate(instruments):
            _strict_fields(
                validation,
                instrument,
                frozenset({"ticker", "cusip", "issuer_lei", "filing_weight_percent"}),
                f"issue #59 selected instrument[{index}]",
            )
        validation.require(
            {item.get("cusip") for item in instruments if isinstance(item, dict)} == EXPECTED_CUSIPS,
            "issue #59: selected instrument records do not match the exact CUSIP scope",
        )
    validation.require(
        isinstance(cusips, list) and {"02079K107", "02079K305"} <= set(cusips),
        "issue #59: Alphabet share classes were collapsed",
    )


def _validate_issue59(
    validation: Validation,
    payload: Any,
    *,
    root: Path,
    fetcher: CommentFetcher | None,
) -> None:
    if not _strict_fields(validation, payload, ISSUE59_FIELDS, "issue #59 artifact"):
        return
    assert isinstance(payload, dict)
    _validate_candidate_identity(validation, payload, 59)
    source_packet = payload.get("source_packet")
    if _strict_fields(validation, source_packet, frozenset({"ref", "comment_id", "sha256"}), "issue #59 source packet"):
        assert isinstance(source_packet, dict)
        _validate_comment_ref(
            validation,
            issue=59,
            comment_id=source_packet.get("comment_id"),
            ref=source_packet.get("ref"),
            expected_sha256=source_packet.get("sha256"),
            label="issue #59 source packet",
            fetcher=fetcher,
        )
    dependencies = payload.get("dependencies")
    validation.require(
        isinstance(dependencies, list) and len(dependencies) == 2, "issue #59: dependencies must be #57 and #58"
    )
    if isinstance(dependencies, list):
        for index, dependency in enumerate(dependencies):
            _strict_fields(
                validation,
                dependency,
                frozenset({"issue", "artifact", "sha256", "state"}),
                f"issue #59 dependency[{index}]",
            )
        validation.require(
            {item.get("issue") for item in dependencies if isinstance(item, dict)} == {57, 58},
            "issue #59: wrong foundation dependencies",
        )
        validation.require(
            all(item.get("state") == "accepted" for item in dependencies if isinstance(item, dict)),
            "issue #59: foundation dependency is not accepted",
        )
    _validate_scope_59(validation, payload.get("scope"))
    catalog = payload.get("catalog")
    if _strict_fields(
        validation,
        catalog,
        frozenset({"required_aliases", "canonical_questions", "resolution_policy"}),
        "issue #59 catalog",
    ):
        assert isinstance(catalog, dict)
        aliases = catalog.get("required_aliases")
        validation.require(
            isinstance(aliases, list) and len(aliases) == 8 and set(aliases) == EXPECTED_ALIASES,
            "issue #59: exact aliases changed",
        )
        questions = catalog.get("canonical_questions")
        validation.require(
            isinstance(questions, list) and len(questions) == 8, "issue #59: canonical question count must be eight"
        )
        if isinstance(questions, list):
            for index, question in enumerate(questions):
                _strict_fields(
                    validation, question, frozenset({"alias", "expected_output_kinds"}), f"issue #59 question[{index}]"
                )
            validation.require(
                {item.get("alias") for item in questions if isinstance(item, dict)} == EXPECTED_ALIASES,
                "issue #59: canonical questions do not cover exact aliases",
            )
        validation.require(
            catalog.get("resolution_policy")
            == "aliases resolve only inside the exact release-bound catalog and universe",
            "issue #59: alias resolution policy weakened",
        )
    semantics = payload.get("semantics")
    _strict_fields(
        validation,
        semantics,
        frozenset({"gppe", "peg", "analyst", "etf", "theme", "supply_chain", "tier_and_strategy"}),
        "issue #59 semantics",
    )
    evaluation = payload.get("evaluation")
    if _strict_fields(
        validation,
        evaluation,
        frozenset(
            {
                "development_golden_targets",
                "holdout_minimums",
                "known_reference",
                "public_golden_manifest",
                "protected_labels_in_repository",
                "fresh_holdout_after_failure_or_change",
            }
        ),
        "issue #59 evaluation",
    ):
        assert isinstance(evaluation, dict)
        targets = evaluation.get("development_golden_targets")
        validation.require(
            isinstance(targets, list) and len(targets) == 8 and set(targets) == EXPECTED_ALIASES,
            "issue #59: development goldens do not cover all targets",
        )
        validation.require(
            evaluation.get("protected_labels_in_repository") is False,
            "issue #59: protected labels cannot be in the repository",
        )
        validation.require(
            evaluation.get("fresh_holdout_after_failure_or_change") is True, "issue #59: fresh-holdout rule weakened"
        )
        _validate_public_goldens(
            validation, root=root, reference=evaluation.get("public_golden_manifest"), issue_state=payload.get("state")
        )
    attestations = payload.get("attestations")
    if _strict_fields(
        validation,
        attestations,
        frozenset({"product_owner", "independent_review", "holdout_custody", "known_reference"}),
        "issue #59 attestations",
    ):
        _validate_internal_attestations(validation, 59, attestations, payload.get("state"))
        product_attestation = attestations.get("product_owner") if isinstance(attestations, dict) else None
        if isinstance(product_attestation, dict) and product_attestation.get("status") == "accepted":
            expected_packet_sha = source_packet.get("sha256") if isinstance(source_packet, dict) else None
            validation.require(
                product_attestation.get("approved_packet_sha256") == expected_packet_sha,
                "issue #59: product-owner approval does not bind the exact source packet",
            )
    _validate_artifact_blockers(validation, 59, payload)


def _validate_public_goldens(validation: Validation, *, root: Path, reference: Any, issue_state: Any) -> None:
    label = "issue #59 public golden manifest"
    if not _strict_fields(validation, reference, frozenset({"path", "sha256", "case_count", "state"}), label):
        return
    assert isinstance(reference, dict)
    validation.require(reference.get("state") in {"candidate_partial", "accepted"}, f"{label}: invalid state")
    validation.require(
        isinstance(reference.get("case_count"), int) and reference["case_count"] >= 1, f"{label}: invalid case count"
    )
    path = _repo_file(validation, root, reference.get("path"), label)
    if path is None:
        return
    expected_sha = reference.get("sha256")
    validation.require(
        isinstance(expected_sha, str) and SHA256_RE.fullmatch(expected_sha) is not None, f"{label}: invalid SHA-256"
    )
    validation.require(_sha256_file(path) == expected_sha, f"{label}: file SHA-256 mismatch")
    try:
        manifest = _load_json(path)
    except json.JSONDecodeError as error:
        validation.require(False, f"{label}: invalid JSON: {error}")
        return
    manifest_fields = frozenset(
        {
            "schema_version",
            "artifact_type",
            "manifest_version",
            "issue",
            "source_packet",
            "evidence_class",
            "claim_ceiling",
            "cases",
        }
    )
    if not _strict_fields(validation, manifest, manifest_fields, label):
        return
    assert isinstance(manifest, dict)
    validation.require(manifest.get("schema_version") == 1, f"{label}: unsupported schema")
    validation.require(
        manifest.get("artifact_type") == "public-development-golden-manifest", f"{label}: wrong artifact type"
    )
    validation.require(manifest.get("issue") == 59, f"{label}: issue mismatch")
    cases = manifest.get("cases")
    validation.require(
        isinstance(cases, list) and len(cases) == reference.get("case_count"), f"{label}: case count mismatch"
    )
    case_keys: list[str] = []
    artifact_paths: list[str] = []
    if isinstance(cases, list):
        for case_index, case in enumerate(cases):
            case_label = f"{label} case[{case_index}]"
            if not _strict_fields(validation, case, frozenset({"case_key", "target", "artifacts"}), case_label):
                continue
            assert isinstance(case, dict)
            case_key = case.get("case_key")
            validation.require(isinstance(case_key, str) and bool(case_key), f"{case_label}: case key is missing")
            if isinstance(case_key, str):
                case_keys.append(case_key)
            target = case.get("target")
            validation.require(target in EXPECTED_ALIASES, f"{case_label}: target is not a release-bound alias")
            artifacts = case.get("artifacts")
            if not _strict_fields(
                validation, artifacts, frozenset({"input", "expected", "provenance"}), f"{case_label} artifacts"
            ):
                continue
            assert isinstance(artifacts, dict)
            for role, artifact_ref in artifacts.items():
                artifact_label = f"{case_label} {role}"
                if not _strict_fields(validation, artifact_ref, frozenset({"path", "sha256"}), artifact_label):
                    continue
                assert isinstance(artifact_ref, dict)
                relative_path = artifact_ref.get("path")
                if isinstance(relative_path, str):
                    artifact_paths.append(relative_path)
                artifact_path = _repo_file(validation, root, artifact_ref.get("path"), artifact_label)
                artifact_sha = artifact_ref.get("sha256")
                validation.require(
                    isinstance(artifact_sha, str) and SHA256_RE.fullmatch(artifact_sha) is not None,
                    f"{artifact_label}: invalid SHA-256",
                )
                if artifact_path is not None:
                    validation.require(
                        _sha256_file(artifact_path) == artifact_sha, f"{artifact_label}: file SHA-256 mismatch"
                    )
                    try:
                        child = _load_json(artifact_path)
                    except json.JSONDecodeError as error:
                        validation.require(False, f"{artifact_label}: invalid JSON: {error}")
                        continue
                    expected_type, content_key = GOLDEN_ROLE_SPECS[role]
                    if not _strict_fields(
                        validation,
                        child,
                        frozenset({"schema_version", "artifact_type", "target", "case_key", content_key}),
                        artifact_label,
                    ):
                        continue
                    assert isinstance(child, dict)
                    validation.require(child.get("schema_version") == 1, f"{artifact_label}: unsupported schema")
                    validation.require(
                        child.get("artifact_type") == expected_type, f"{artifact_label}: wrong artifact type"
                    )
                    validation.require(child.get("target") == target, f"{artifact_label}: target disagrees with case")
                    validation.require(
                        child.get("case_key") == case_key, f"{artifact_label}: case key disagrees with case"
                    )
                    validation.require(
                        isinstance(child.get(content_key), dict) and bool(child[content_key]),
                        f"{artifact_label}: content is missing",
                    )
                    validation.require(
                        not _contains_forbidden_public_golden_key(child),
                        f"{artifact_label}: public golden contains approval or protected-label material",
                    )
    validation.require(len(case_keys) == len(set(case_keys)), f"{label}: duplicate case keys")
    validation.require(len(artifact_paths) == len(set(artifact_paths)), f"{label}: child artifact path is reused")
    if issue_state == "accepted":
        validation.require(
            reference.get("state") == "accepted", f"{label}: accepted issue retains partial golden program"
        )
        targets = {case.get("target") for case in cases if isinstance(case, dict)} if isinstance(cases, list) else set()
        validation.require(len(targets) >= 8, f"{label}: accepted program does not cover eight independent targets")


def _validate_candidate_identity(validation: Validation, payload: dict[str, Any], issue: int) -> None:
    validation.require(payload.get("schema_version") == 1, f"issue #{issue}: unsupported artifact schema")
    validation.require(
        payload.get("artifact_type") == EXPECTED_ARTIFACT_TYPES[issue], f"issue #{issue}: wrong artifact type"
    )
    artifact_version = payload.get("artifact_version")
    state = payload.get("state")
    valid_version = (
        artifact_version == "candidate-v1"
        if state != "accepted"
        else bool(isinstance(artifact_version, str) and re.fullmatch(r"accepted-v[1-9][0-9]*", artifact_version))
    )
    validation.require(valid_version, f"issue #{issue}: artifact version does not match its state")
    validation.require(payload.get("issue") == issue, f"issue #{issue}: artifact issue mismatch")
    validation.require(
        state in {"candidate_unapproved", "candidate_incomplete", "accepted"},
        f"issue #{issue}: invalid state",
    )
    validation.require(
        artifact_version != "candidate-v1" or payload.get("state") != "accepted",
        f"issue #{issue}: candidate-v1 is proposal evidence and can never be accepted; materialize accepted-v1",
    )


def _validate_artifact_blockers(validation: Validation, issue: int, payload: dict[str, Any]) -> None:
    blockers = payload.get("blocking_reasons")
    state = payload.get("state")
    if state == "accepted":
        validation.require(blockers == [], f"issue #{issue}: accepted artifact retains blockers")
    else:
        validation.require(_nonempty_strings(blockers), f"issue #{issue}: candidate must preserve blocking reasons")


def _validate_internal_attestations(
    validation: Validation, issue: int, attestations: dict[str, Any], state: Any
) -> None:
    for key, attestation in attestations.items():
        validation.require(isinstance(attestation, dict), f"issue #{issue} attestation {key}: expected an object")
        if not isinstance(attestation, dict):
            continue
        _strict_fields(
            validation,
            attestation,
            INTERNAL_ATTESTATION_FIELDS[issue][key],
            f"issue #{issue} attestation {key}",
        )
        status = attestation.get("status")
        validation.require(status in {"missing", "accepted"}, f"issue #{issue} attestation {key}: invalid status")
        if status == "missing":
            for field, value in attestation.items():
                if field != "status":
                    validation.require(
                        value is None or value == () or value == [],
                        f"issue #{issue} attestation {key}: missing attestation fabricates {field}",
                    )
        else:
            validation.require(
                any(field.endswith("ref") and bool(value) for field, value in attestation.items()),
                f"issue #{issue} attestation {key}: accepted attestation lacks an exact ref",
            )
            for field, value in attestation.items():
                if field not in {"status", "conflicts"}:
                    validation.require(
                        value is not None and value != "" and value != [],
                        f"issue #{issue} attestation {key}: accepted attestation lacks {field}",
                    )
    if state == "accepted":
        validation.require(
            all(item.get("status") == "accepted" for item in attestations.values() if isinstance(item, dict)),
            f"issue #{issue}: accepted artifact has missing attestations",
        )


def _validate_issue60(validation: Validation, payload: Any, *, fetcher: CommentFetcher | None) -> None:
    if not _strict_fields(validation, payload, ISSUE60_FIELDS, "issue #60 artifact"):
        return
    assert isinstance(payload, dict)
    _validate_candidate_identity(validation, payload, 60)
    dependency = payload.get("depends_on")
    _strict_fields(validation, dependency, frozenset({"issue", "artifact", "sha256"}), "issue #60 dependency")
    if isinstance(dependency, dict):
        validation.require(dependency.get("issue") == 59, "issue #60: dependency must point one-way to #59")
    scope = payload.get("scope")
    if _strict_fields(
        validation,
        scope,
        frozenset({"universe_id", "series", "accession", "expected_issuers", "expected_instruments", "environments"}),
        "issue #60 scope",
    ):
        assert isinstance(scope, dict)
        validation.require(scope.get("universe_id") == "universe:topt-us-2026-03-31", "issue #60: wrong UniverseRef")
        validation.require(
            scope.get("series") == "S000088434" and scope.get("accession") == "000207169126012475",
            "issue #60: wrong TOPT filing",
        )
        validation.require(
            scope.get("expected_issuers") == 20 and scope.get("expected_instruments") == 21,
            "issue #60: exact TOPT counts changed",
        )
        environments = scope.get("environments")
        validation.require(
            isinstance(environments, list) and len(environments) == 5 and set(environments) == EXPECTED_ENVIRONMENTS,
            "issue #60: five-environment matrix changed",
        )
    probes = payload.get("observed_probe_evidence")
    validation.require(isinstance(probes, list) and len(probes) >= 2, "issue #60: bounded probe evidence is missing")
    if isinstance(probes, list):
        for index, probe in enumerate(probes):
            if not _strict_fields(
                validation,
                probe,
                frozenset({"kind", "ref", "comment_id", "sha256", "observations"}),
                f"issue #60 probe[{index}]",
            ):
                continue
            assert isinstance(probe, dict)
            _validate_comment_ref(
                validation,
                issue=60,
                comment_id=probe.get("comment_id"),
                ref=probe.get("ref"),
                expected_sha256=probe.get("sha256"),
                label=f"issue #60 probe[{index}]",
                fetcher=fetcher,
            )
    source_paths = payload.get("source_paths")
    validation.require(
        isinstance(source_paths, list) and len(source_paths) == len(EXPECTED_SOURCE_DOMAINS),
        "issue #60: source domain matrix is incomplete",
    )
    if isinstance(source_paths, list):
        for index, source_path in enumerate(source_paths):
            _strict_fields(
                validation,
                source_path,
                frozenset(
                    {
                        "domain",
                        "primary",
                        "fallback_policy",
                        "fallback",
                        "identifier_level",
                        "knowability_rule",
                        "capture_method",
                        "status",
                    }
                ),
                f"issue #60 source_paths[{index}]",
            )
        validation.require(
            {item.get("domain") for item in source_paths if isinstance(item, dict)} == EXPECTED_SOURCE_DOMAINS,
            "issue #60: exact source domains changed",
        )
    rights = payload.get("rights_requirements")
    if _strict_fields(
        validation,
        rights,
        frozenset({"permissions", "required_fields", "unknown_is_approval", "ai_interpretation_is_approval", "status"}),
        "issue #60 rights",
    ):
        assert isinstance(rights, dict)
        validation.require(rights.get("unknown_is_approval") is False, "issue #60: unknown rights cannot be approval")
        validation.require(
            rights.get("ai_interpretation_is_approval") is False, "issue #60: AI interpretation cannot be approval"
        )
    _strict_fields(
        validation,
        payload.get("budget_requirements"),
        frozenset({"dimensions", "periods", "required_fields", "status"}),
        "issue #60 budget",
    )
    runtime = payload.get("runtime_requirements")
    if _strict_fields(
        validation,
        runtime,
        frozenset(
            {
                "immutable_model_revision",
                "preflight_before_every_source_call",
                "scheduled_rights_and_budget_recheck",
                "full_catalog_budget_reconciliation",
                "disable_preserves_historical_reproducibility",
                "readiness_override_permitted",
                "status",
            }
        ),
        "issue #60 runtime",
    ):
        assert isinstance(runtime, dict)
        validation.require(
            runtime.get("readiness_override_permitted") is False, "issue #60: readiness override cannot be enabled"
        )
        for field in (
            "immutable_model_revision",
            "preflight_before_every_source_call",
            "scheduled_rights_and_budget_recheck",
            "full_catalog_budget_reconciliation",
            "disable_preserves_historical_reproducibility",
        ):
            validation.require(runtime.get(field) is True, f"issue #60: runtime invariant {field} was weakened")
    attestations = payload.get("attestations")
    if _strict_fields(
        validation,
        attestations,
        frozenset({"product_owner_source_plan", "rights_authority", "budget_authority", "live_probe_owner"}),
        "issue #60 attestations",
    ):
        _validate_internal_attestations(validation, 60, attestations, payload.get("state"))
    if payload.get("state") == "accepted":
        for label, section in (
            ("rights", rights),
            ("budget", payload.get("budget_requirements")),
            ("runtime", runtime),
        ):
            validation.require(
                isinstance(section, dict) and section.get("status") == "accepted",
                f"issue #60: accepted artifact retains unaccepted {label} requirements",
            )
        if isinstance(source_paths, list):
            validation.require(
                all(isinstance(item, dict) and item.get("status") == "accepted" for item in source_paths),
                "issue #60: accepted artifact retains incomplete source paths",
            )
    _validate_artifact_blockers(validation, 60, payload)


def _validate_issue61(validation: Validation, payload: Any) -> None:
    if not _strict_fields(validation, payload, ISSUE61_FIELDS, "issue #61 artifact"):
        return
    assert isinstance(payload, dict)
    _validate_candidate_identity(validation, payload, 61)
    dependencies = payload.get("dependencies")
    validation.require(
        isinstance(dependencies, list) and len(dependencies) == 2, "issue #61: dependencies must be #59 and #60"
    )
    if isinstance(dependencies, list):
        for index, dependency in enumerate(dependencies):
            _strict_fields(
                validation,
                dependency,
                frozenset({"issue", "artifact", "sha256", "required_state"}),
                f"issue #61 dependency[{index}]",
            )
        validation.require(
            {item.get("issue") for item in dependencies if isinstance(item, dict)} == {59, 60},
            "issue #61: wrong predecessor set",
        )
        validation.require(
            all(item.get("required_state") == "accepted" for item in dependencies if isinstance(item, dict)),
            "issue #61: predecessor required state was weakened",
        )
    boundary = payload.get("acceptance_boundary")
    if _strict_fields(
        validation,
        boundary,
        frozenset({"owns", "downstream_evidence_owners", "claims_production_readiness"}),
        "issue #61 acceptance boundary",
    ):
        assert isinstance(boundary, dict)
        validation.require(
            boundary.get("claims_production_readiness") is False,
            "issue #61: Gate 0 policy cannot claim Production readiness",
        )
    scope = payload.get("scope")
    if _strict_fields(
        validation, scope, frozenset({"universe_id", "fixed_baseline", "rolling_canary"}), "issue #61 scope"
    ):
        assert isinstance(scope, dict)
        validation.require(scope.get("universe_id") == "universe:topt-us-2026-03-31", "issue #61: wrong UniverseRef")
        fixed = scope.get("fixed_baseline")
        if _strict_fields(
            validation,
            fixed,
            frozenset(
                {
                    "series",
                    "accession",
                    "report_date",
                    "issuer_count",
                    "instrument_count",
                    "membership_rewrite_permitted",
                }
            ),
            "issue #61 fixed baseline",
        ):
            assert isinstance(fixed, dict)
            validation.require(
                fixed
                == {
                    "series": "S000088434",
                    "accession": "000207169126012475",
                    "report_date": "2026-03-31",
                    "issuer_count": 20,
                    "instrument_count": 21,
                    "membership_rewrite_permitted": False,
                },
                "issue #61: immutable TOPT baseline changed",
            )
        rolling = scope.get("rolling_canary")
        if _strict_fields(
            validation,
            rolling,
            frozenset(
                {"scope_id_must_differ_from_fixed_baseline", "membership_change_mode", "may_satisfy_fixed_baseline"}
            ),
            "issue #61 rolling canary",
        ):
            assert isinstance(rolling, dict)
            validation.require(
                rolling.get("scope_id_must_differ_from_fixed_baseline") is True,
                "issue #61: rolling canary scope may alias baseline",
            )
            validation.require(
                rolling.get("may_satisfy_fixed_baseline") is False,
                "issue #61: rolling canary may not satisfy fixed baseline",
            )
    applicability = payload.get("applicability")
    if _strict_fields(
        validation,
        applicability,
        frozenset(
            {
                "classifications",
                "producer_supplied_applicable_boolean",
                "required_coordinates",
                "effective_before_execution",
                "missing_duplicate_postdated_or_wrong_scope_cell",
                "post_result_scope_reduction",
            }
        ),
        "issue #61 applicability",
    ):
        assert isinstance(applicability, dict)
        validation.require(
            applicability.get("producer_supplied_applicable_boolean") is False,
            "issue #61: producer-supplied applicability is forbidden",
        )
        validation.require(
            applicability.get("effective_before_execution") is True, "issue #61: applicability must predate execution"
        )
    module_slos = payload.get("proposed_module_slos")
    validation.require(
        isinstance(module_slos, list) and len(module_slos) == 8, "issue #61: module SLOs must cover eight targets"
    )
    if isinstance(module_slos, list):
        expected_fields = frozenset(
            {
                "module",
                "minimum_subject_count",
                "minimum_usable_coverage",
                "maximum_unavailable_ratio",
                "maximum_stale_ratio",
                "maximum_unresolved_ratio",
                "maximum_unclassified_ratio",
                "maximum_low_confidence_ratio",
            }
        )
        for index, slo in enumerate(module_slos):
            _strict_fields(validation, slo, expected_fields, f"issue #61 module SLO[{index}]")
        validation.require(
            {item.get("module") for item in module_slos if isinstance(item, dict)} == EXPECTED_ALIASES,
            "issue #61: module SLO aliases changed",
        )
    _strict_fields(
        validation,
        payload.get("capture_slo"),
        frozenset({"row_complete_coordinates", "required_evidence", "missing_required_evidence"}),
        "issue #61 capture SLO",
    )
    refresh = payload.get("proposed_natural_refresh_requirements")
    validation.require(isinstance(refresh, list) and bool(refresh), "issue #61: natural-refresh policy is empty")
    if isinstance(refresh, list):
        for index, requirement in enumerate(refresh):
            _strict_fields(
                validation,
                requirement,
                frozenset(
                    {
                        "source_class",
                        "cadence",
                        "maximum_age",
                        "required_changed_partitions",
                        "required_publication_transitions",
                        "maximum_observation_window",
                    }
                ),
                f"issue #61 natural refresh[{index}]",
            )
    exclusions = payload.get("natural_refresh_exclusions")
    validation.require(
        isinstance(exclusions, list)
        and set(exclusions)
        == {"fixture_replay", "immediate_retry", "reparse", "synthetic_mutation", "unchanged_bytes"},
        "issue #61: natural-refresh negative controls changed",
    )
    consumers = payload.get("proposed_consumer_slos")
    validation.require(
        isinstance(consumers, list)
        and {item.get("surface") for item in consumers if isinstance(item, dict)}
        == {"app", "mcp", "chat", "report", "card"},
        "issue #61: consumer SLO surfaces changed",
    )
    if isinstance(consumers, list):
        for index, consumer in enumerate(consumers):
            _strict_fields(
                validation,
                consumer,
                frozenset(
                    {
                        "surface",
                        "authenticated",
                        "maximum_latency",
                        "maximum_rows",
                        "minimum_trace_complete_rate",
                        "maximum_error_rate",
                    }
                ),
                f"issue #61 consumer SLO[{index}]",
            )
    telemetry = payload.get("usage_telemetry_slo")
    if _strict_fields(
        validation,
        telemetry,
        frozenset(
            {
                "completeness_target",
                "maximum_lag",
                "minimum_retention",
                "maximum_reconciliation_difference",
                "idempotent_event_identity",
                "wrong_scope",
                "absent_event",
                "stages",
            }
        ),
        "issue #61 usage telemetry",
    ):
        assert isinstance(telemetry, dict)
        validation.require(
            telemetry.get("absent_event") == "fail_not_zero_use", "issue #61: absent telemetry cannot mean zero use"
        )
    compiler = payload.get("planned_demand_compiler")
    if _strict_fields(
        validation,
        compiler,
        frozenset(
            {
                "inputs",
                "output",
                "producer_may_add_or_remove_cells",
                "frequency_may_change_frozen_policy",
                "required_input_with_zero_observed_use",
            }
        ),
        "issue #61 planned-demand compiler",
    ):
        assert isinstance(compiler, dict)
        validation.require(
            compiler.get("producer_may_add_or_remove_cells") is False, "issue #61: producer cannot alter planned cells"
        )
        validation.require(
            compiler.get("frequency_may_change_frozen_policy") is False,
            "issue #61: frequency cannot mutate frozen policy",
        )
    attestations = payload.get("attestations")
    if _strict_fields(
        validation,
        attestations,
        frozenset(
            {"product_owner_thresholds_and_wall_clock", "independent_slo_review", "topt_baseline", "runbook_owners"}
        ),
        "issue #61 attestations",
    ):
        _validate_internal_attestations(validation, 61, attestations, payload.get("state"))
    _validate_artifact_blockers(validation, 61, payload)


def _validate_manifest_shape(validation: Validation, manifest: Any) -> None:
    if not isinstance(manifest, dict):
        validation.require(False, "Gate 0 manifest: expected an object")
        return
    version = manifest.get("manifest_version")
    expected_fields = SUCCESSOR_MANIFEST_FIELDS if isinstance(version, int) and version >= 5 else MANIFEST_FIELDS
    if not _strict_fields(validation, manifest, expected_fields, "Gate 0 manifest"):
        return
    validation.require(manifest.get("schema_version") == 1, "Gate 0 manifest: unsupported schema")
    validation.require(
        isinstance(version, int) and not isinstance(version, bool) and version >= 4,
        "Gate 0 manifest: unsupported manifest version",
    )
    if not isinstance(version, int) or isinstance(version, bool):
        return
    validation.require(
        manifest.get("manifest_id") == f"gate-0-batch-v{version}",
        "Gate 0 manifest: manifest ID does not match its version",
    )
    validation.require(manifest.get("gate_issue") == 56, "Gate 0 manifest: wrong Gate issue")
    validation.require(
        manifest.get("status") in {"candidate_blocked_external_attestation", "accepted"},
        "Gate 0 manifest: invalid status",
    )
    branch = manifest.get("integration_branch")
    if version == 4:
        validation.require(
            branch == "batch/gate-0-v4-semantic-data-closure",
            "Gate 0 manifest: wrong immutable v4 integration branch",
        )
    else:
        validation.require(
            isinstance(branch, str) and branch.startswith(f"batch/gate-0-v{version}-"),
            "Gate 0 manifest: successor integration branch does not match its version",
        )
    base_sha = manifest.get("integration_base_sha")
    validation.require(
        isinstance(base_sha, str) and GIT_SHA_RE.fullmatch(base_sha) is not None,
        "Gate 0 manifest: invalid integration base SHA",
    )
    validation.require(manifest.get("included_issues") == list(ISSUES), "Gate 0 manifest: included issues changed")
    validation.require(manifest.get("dependency_order") == list(ISSUES), "Gate 0 manifest: dependency order changed")
    expected_payload_sha = candidate_payload_sha256(manifest)
    validation.require(
        manifest.get("candidate_payload_sha256") == expected_payload_sha,
        "Gate 0 manifest: candidate payload SHA-256 mismatch",
    )
    paths = manifest.get("paths")
    expected_paths = V4_MANIFEST_PATHS if version == 4 else SUCCESSOR_MANIFEST_PATHS
    validation.require(paths == list(expected_paths), "Gate 0 manifest: authorized path set changed")
    tree_sha = manifest.get("candidate_tree_sha256")
    validation.require(
        isinstance(tree_sha, str) and SHA256_RE.fullmatch(tree_sha) is not None,
        "Gate 0 manifest: invalid candidate tree SHA-256",
    )
    merge_policy = manifest.get("merge_policy")
    merge_fields = frozenset(
        {
            "target_branch",
            "partial_gate_acceptance_allowed",
            "one_immutable_candidate_required",
            "candidate_change_invalidates_acceptance",
            "historical_foundation_already_merged",
            "historical_note",
        }
    )
    if _strict_fields(validation, merge_policy, merge_fields, "Gate 0 merge policy"):
        assert isinstance(merge_policy, dict)
        validation.require(merge_policy.get("target_branch") == "main", "Gate 0 merge policy: target must be main")
        validation.require(
            merge_policy.get("partial_gate_acceptance_allowed") is False,
            "Gate 0 merge policy: partial Gate acceptance cannot be allowed",
        )
        validation.require(
            merge_policy.get("one_immutable_candidate_required") is True,
            "Gate 0 merge policy: immutable candidate is required",
        )
        validation.require(
            merge_policy.get("candidate_change_invalidates_acceptance") is True,
            "Gate 0 merge policy: candidate drift must invalidate acceptance",
        )
        validation.require(
            merge_policy.get("historical_foundation_already_merged") == [57, 58],
            "Gate 0 merge policy: historical foundation changed",
        )
    acceptance = manifest.get("acceptance")
    _strict_fields(
        validation,
        acceptance,
        frozenset({"commands", "candidate_validation_claim", "acceptance_claim"}),
        "Gate 0 acceptance",
    )
    if isinstance(acceptance, dict):
        validation.require(_nonempty_strings(acceptance.get("commands")), "Gate 0 acceptance: commands are missing")


def _validate_successor_manifest(
    validation: Validation,
    *,
    root: Path,
    manifest: dict[str, Any],
) -> None:
    version = manifest.get("manifest_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 5:
        return
    predecessor_ref = manifest.get("predecessor_manifest")
    if not _strict_fields(
        validation,
        predecessor_ref,
        PREDECESSOR_MANIFEST_FIELDS,
        "Gate 0 predecessor manifest",
    ):
        return
    assert isinstance(predecessor_ref, dict)
    predecessor_version = version - 1
    predecessor_path = f"governance/gate0/manifest-v{predecessor_version}.json"
    validation.require(
        predecessor_ref.get("manifest_id") == f"gate-0-batch-v{predecessor_version}",
        "Gate 0 predecessor: manifest ID mismatch",
    )
    validation.require(
        predecessor_ref.get("manifest_version") == predecessor_version,
        "Gate 0 predecessor: version must be contiguous",
    )
    validation.require(predecessor_ref.get("path") == predecessor_path, "Gate 0 predecessor: path mismatch")
    predecessor_file = _repo_file(validation, root, predecessor_path, "Gate 0 predecessor")
    if predecessor_file is None:
        return
    predecessor_sha = _sha256_file(predecessor_file)
    validation.require(
        predecessor_ref.get("sha256") == predecessor_sha,
        "Gate 0 predecessor: file SHA-256 mismatch",
    )
    try:
        predecessor = _load_json(predecessor_file)
    except json.JSONDecodeError as error:
        validation.require(False, f"Gate 0 predecessor: invalid JSON: {error}")
        return
    if not isinstance(predecessor, dict):
        validation.require(False, "Gate 0 predecessor: expected an object")
        return
    validation.require(
        predecessor.get("manifest_id") == predecessor_ref.get("manifest_id")
        and predecessor.get("manifest_version") == predecessor_ref.get("manifest_version"),
        "Gate 0 predecessor: referenced identity does not match file content",
    )
    candidate_commit = predecessor_ref.get("candidate_commit_sha")
    validation.require(
        isinstance(candidate_commit, str) and GIT_SHA_RE.fullmatch(candidate_commit) is not None,
        "Gate 0 predecessor: invalid frozen candidate commit",
    )
    if isinstance(candidate_commit, str) and GIT_SHA_RE.fullmatch(candidate_commit) is not None:
        _validate_frozen_tree_proof(
            validation,
            root=root,
            reference=predecessor_ref.get("candidate_tree_proof"),
            predecessor_path=predecessor_path,
            predecessor_sha=predecessor_sha,
            predecessor=predecessor,
            predecessor_version=predecessor_version,
            candidate_commit=candidate_commit,
        )
        commit_available = frozen_commit_available(candidate_commit)
        if version > 5:
            validation.require(
                commit_available,
                "Gate 0 predecessor: frozen candidate commit is unavailable",
            )
        if commit_available:
            frozen_manifest = frozen_file_bytes(candidate_commit, predecessor_path)
            frozen_tree = frozen_candidate_tree_sha256(candidate_commit, predecessor.get("paths", []))
            validation.require(
                _sha256_bytes(frozen_manifest) == predecessor_ref.get("sha256"),
                "Gate 0 predecessor: frozen manifest bytes do not match the reference",
            )
            validation.require(
                frozen_tree == predecessor.get("candidate_tree_sha256"),
                "Gate 0 predecessor: frozen candidate tree does not match the manifest",
            )
    if version == 5:
        validation.require(
            candidate_commit == V4_FROZEN_COMMIT_SHA,
            "Gate 0 v5 predecessor: wrong v4 freeze commit",
        )
    for field in ("gate_issue", "included_issues", "dependency_order", "merge_policy"):
        validation.require(
            manifest.get(field) == predecessor.get(field),
            f"Gate 0 successor: predecessor field changed: {field}",
        )
    if version == 5:
        for field in ("artifacts", "external_attestations", "blocking_reasons"):
            validation.require(
                manifest.get(field) == predecessor.get(field),
                f"Gate 0 v5 successor: predecessor field changed: {field}",
            )
        validation.require(
            manifest.get("status") == predecessor.get("status") == "candidate_blocked_external_attestation",
            "Gate 0 v5 successor: external blockers must remain active",
        )
    elif predecessor.get("status") == "accepted":
        validation.require(
            False,
            "Gate 0 successor: an accepted predecessor is terminal",
        )

    bindings = manifest.get("integration_bindings")
    validation.require(
        isinstance(bindings, list) and len(bindings) == len(SUCCESSOR_BINDINGS),
        "Gate 0 successor: exact integration binding set is required",
    )
    if not isinstance(bindings, list):
        return
    by_role: dict[str, dict[str, Any]] = {}
    for index, binding in enumerate(bindings):
        label = f"Gate 0 integration binding[{index}]"
        if not _strict_fields(validation, binding, INTEGRATION_BINDING_FIELDS, label):
            continue
        assert isinstance(binding, dict)
        role = binding.get("role")
        validation.require(isinstance(role, str) and role not in by_role, f"{label}: duplicate or invalid role")
        if not isinstance(role, str) or role in by_role:
            continue
        by_role[role] = binding
        expected_path = SUCCESSOR_BINDINGS.get(role)
        validation.require(expected_path is not None, f"{label}: unknown role")
        validation.require(binding.get("path") == expected_path, f"{label}: path mismatch")
        if expected_path is None:
            continue
        bound_file = _repo_file(validation, root, expected_path, label)
        if bound_file is not None:
            validation.require(
                binding.get("sha256") == _sha256_file(bound_file),
                f"{label}: file SHA-256 mismatch",
            )
    validation.require(set(by_role) == set(SUCCESSOR_BINDINGS), "Gate 0 successor: integration roles changed")


def _validate_artifacts(
    validation: Validation,
    *,
    root: Path,
    manifest: dict[str, Any],
    fetcher: CommentFetcher | None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    artifact_refs = manifest.get("artifacts")
    validation.require(
        isinstance(artifact_refs, list) and len(artifact_refs) == 5,
        "Gate 0 manifest: exactly five artifacts are required",
    )
    refs: dict[int, dict[str, Any]] = {}
    payloads: dict[int, dict[str, Any]] = {}
    if not isinstance(artifact_refs, list):
        return refs, payloads
    for index, artifact in enumerate(artifact_refs):
        label = f"Gate 0 artifact[{index}]"
        if not isinstance(artifact, dict):
            validation.require(False, f"{label}: expected an object")
            continue
        issue = artifact.get("issue")
        expected_fields = FOUNDATION_ARTIFACT_FIELDS if issue in {57, 58} else CANDIDATE_ARTIFACT_FIELDS
        _strict_fields(validation, artifact, expected_fields, label)
        validation.require(issue in ISSUES, f"{label}: unmanaged issue")
        if issue not in ISSUES or issue in refs:
            validation.require(issue not in refs, f"{label}: duplicate issue")
            continue
        refs[issue] = artifact
        validation.require(artifact.get("kind") == EXPECTED_KINDS[issue], f"{label}: wrong artifact kind")
        expected_dependencies = EXPECTED_DEPENDENCIES[issue]
        if issue in CANDIDATE_ISSUES:
            validation.require(
                artifact.get("depends_on") == list(expected_dependencies), f"{label}: dependency edge changed"
            )
        else:
            validation.require(artifact.get("state") == "accepted", f"{label}: foundation is not accepted")
        expected_sha = artifact.get("sha256")
        validation.require(
            isinstance(expected_sha, str) and SHA256_RE.fullmatch(expected_sha) is not None,
            f"{label}: invalid artifact SHA-256",
        )
        path = _repo_file(validation, root, artifact.get("path"), label)
        if path is None:
            continue
        validation.require(_sha256_file(path) == expected_sha, f"{label}: file SHA-256 mismatch")
        try:
            payload = _load_json(path)
        except json.JSONDecodeError as error:
            validation.require(False, f"{label}: invalid JSON: {error}")
            continue
        if isinstance(payload, dict):
            payloads[issue] = payload
        if issue in {57, 58}:
            _validate_foundation(validation, issue=issue, path=path, payload=payload)
        else:
            validation.require(
                isinstance(payload, dict) and payload.get("state") == artifact.get("state"),
                f"{label}: manifest and artifact state disagree",
            )
            if issue == 59:
                _validate_issue59(validation, payload, root=root, fetcher=fetcher)
            elif issue == 60:
                _validate_issue60(validation, payload, fetcher=fetcher)
            else:
                _validate_issue61(validation, payload)
    validation.require(set(refs) == set(ISSUES), "Gate 0 manifest: artifact issue set changed")
    return refs, payloads


def _validate_dependency_hashes(
    validation: Validation,
    *,
    refs: dict[int, dict[str, Any]],
    payloads: dict[int, dict[str, Any]],
) -> None:
    issue59 = payloads.get(59)
    if issue59 is not None:
        dependencies = issue59.get("dependencies")
        if isinstance(dependencies, list):
            by_issue = {item.get("issue"): item for item in dependencies if isinstance(item, dict)}
            for issue in (57, 58):
                if issue in by_issue and issue in refs:
                    validation.require(
                        by_issue[issue].get("artifact") == refs[issue].get("path"),
                        f"issue #59: dependency #{issue} path mismatch",
                    )
                    validation.require(
                        by_issue[issue].get("sha256") == refs[issue].get("sha256"),
                        f"issue #59: dependency #{issue} hash mismatch",
                    )
    issue60 = payloads.get(60)
    if issue60 is not None and 59 in refs:
        dependency = issue60.get("depends_on")
        if isinstance(dependency, dict):
            validation.require(
                dependency.get("artifact") == refs[59].get("path"), "issue #60: #59 dependency path mismatch"
            )
            validation.require(
                dependency.get("sha256") == refs[59].get("sha256"), "issue #60: #59 dependency hash mismatch"
            )
    issue61 = payloads.get(61)
    if issue61 is not None:
        dependencies = issue61.get("dependencies")
        if isinstance(dependencies, list):
            by_issue = {item.get("issue"): item for item in dependencies if isinstance(item, dict)}
            for issue in (59, 60):
                if issue in by_issue and issue in refs:
                    validation.require(
                        by_issue[issue].get("artifact") == refs[issue].get("path"),
                        f"issue #61: dependency #{issue} path mismatch",
                    )
                    validation.require(
                        by_issue[issue].get("sha256") == refs[issue].get("sha256"),
                        f"issue #61: dependency #{issue} hash mismatch",
                    )
                    validation.require(
                        by_issue[issue].get("required_state") == "accepted",
                        f"issue #61: dependency #{issue} must require accepted state",
                    )


def _validate_external_attestations(
    validation: Validation,
    *,
    manifest: dict[str, Any],
    refs: dict[int, dict[str, Any]],
    fetcher: CommentFetcher | None,
) -> None:
    attestations = manifest.get("external_attestations")
    validation.require(
        isinstance(attestations, list) and len(attestations) == len(EXPECTED_ATTESTATIONS),
        "Gate 0 manifest: external attestation set is incomplete",
    )
    if not isinstance(attestations, list):
        return
    by_key: dict[str, dict[str, Any]] = {}
    live: dict[str, dict[str, Any]] = {}
    accepted_refs: set[str] = set()
    for index, attestation in enumerate(attestations):
        label = f"external attestation[{index}]"
        if not _strict_fields(validation, attestation, EXTERNAL_ATTESTATION_FIELDS, label):
            continue
        assert isinstance(attestation, dict)
        key = attestation.get("key")
        validation.require(key in EXPECTED_ATTESTATIONS, f"{label}: unknown key")
        validation.require(isinstance(key, str) and key not in by_key, f"{label}: duplicate key")
        if not isinstance(key, str) or key in by_key:
            continue
        by_key[key] = attestation
        issue = EXPECTED_ATTESTATIONS.get(key)
        validation.require(attestation.get("issue") == issue, f"{label}: issue mismatch")
        target_sha = attestation.get("target_sha256")
        validation.require(
            isinstance(target_sha, str) and SHA256_RE.fullmatch(target_sha) is not None,
            f"{label}: invalid target SHA-256",
        )
        expected_target = refs.get(issue, {}).get("sha256")
        validation.require(
            target_sha == expected_target, f"{label}: target SHA-256 does not bind the exact issue artifact"
        )
        status = attestation.get("status")
        validation.require(status in {"missing", "accepted"}, f"{label}: invalid status")
        ref = attestation.get("ref")
        if status == "missing":
            validation.require(ref is None, f"{label}: missing attestation must not fabricate a ref")
            continue
        validation.require(isinstance(ref, str), f"{label}: accepted attestation requires an exact comment ref")
        match = COMMENT_URL_RE.fullmatch(ref) if isinstance(ref, str) else None
        validation.require(match is not None, f"{label}: accepted ref is not an exact repository issue-comment URL")
        if match is None:
            continue
        validation.require(int(match.group("issue")) == issue, f"{label}: accepted ref names another issue")
        validation.require(ref not in accepted_refs, f"{label}: approval comment is reused by another role")
        accepted_refs.add(ref)
        if fetcher is not None:
            comment_id = int(match.group("comment"))
            try:
                comment = fetcher(comment_id)
            except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                validation.require(False, f"{label}: cannot load live approval comment: {error}")
                continue
            body = comment.get("body")
            validation.require(
                comment.get("id") == comment_id and comment.get("html_url") == ref,
                f"{label}: live approval identity mismatch",
            )
            validation.require(
                isinstance(body, str) and target_sha in body, f"{label}: live approval does not bind target SHA-256"
            )
            if isinstance(body, str):
                validation.require(
                    re.search(r"\b(approve|attest)\w*\b", body, re.IGNORECASE) is not None,
                    f"{label}: live comment is not an explicit approval or attestation",
                )
                for alternatives in ATTESTATION_BODY_TERMS[key]:
                    validation.require(
                        any(re.search(pattern, body, re.IGNORECASE) is not None for pattern in alternatives),
                        f"{label}: live approval omits required authority statement {alternatives!r}",
                    )
            user = comment.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            validation.require(
                isinstance(login, str) and not login.endswith("[bot]"),
                f"{label}: approval author must be a named human",
            )
            if key in {"issue59_product_owner", "issue61_product_owner_policy"}:
                validation.require(
                    login == PRODUCT_OWNER_LOGIN,
                    f"{label}: product-owner approval must come from {PRODUCT_OWNER_LOGIN}",
                )
            live[key] = comment
    validation.require(
        set(by_key) == set(EXPECTED_ATTESTATIONS), "Gate 0 manifest: exact external attestation keys changed"
    )
    if fetcher is not None:
        _validate_live_role_separation(validation, live)


def _validate_live_role_separation(validation: Validation, comments: dict[str, dict[str, Any]]) -> None:
    def author(key: str) -> str | None:
        user = comments.get(key, {}).get("user")
        return user.get("login") if isinstance(user, dict) else None

    product59 = author("issue59_product_owner")
    independent59 = author("issue59_independent_review")
    if product59 is not None and independent59 is not None:
        validation.require(product59 != independent59, "issue #59: independent reviewer cannot be the product owner")
    product61 = author("issue61_product_owner_policy")
    independent61 = author("issue61_independent_slo_review")
    if product61 is not None and independent61 is not None:
        validation.require(
            product61 != independent61, "issue #61: independent SLO reviewer cannot be the product owner"
        )
    independent_authors = {
        author("issue59_independent_review"),
        author("issue59_holdout_custody"),
        author("issue59_known_reference"),
        author("issue61_independent_slo_review"),
    }
    independent_authors.discard(None)
    validation.require(
        PRODUCT_OWNER_LOGIN not in independent_authors,
        "Gate 0: product owner cannot satisfy an independent review or custody role",
    )
    issue59_custodian = author("issue59_holdout_custody")
    if independent59 is not None and issue59_custodian is not None:
        validation.require(
            independent59 != issue59_custodian,
            "issue #59: semantic reviewer cannot also control holdout custody",
        )
    rights60 = author("issue60_rights_authority")
    budget60 = author("issue60_budget_authority")
    probe60 = author("issue60_live_probe_owner")
    if rights60 is not None and budget60 is not None:
        validation.require(rights60 != budget60, "issue #60: rights and budget authorities must be separate")
    if probe60 is not None:
        validation.require(
            probe60 not in {rights60, budget60},
            "issue #60: live-probe owner cannot self-attest rights or budget authority",
        )


def _validate_state_and_blockers(
    validation: Validation,
    *,
    manifest: dict[str, Any],
    refs: dict[int, dict[str, Any]],
) -> tuple[str, ...]:
    blockers = manifest.get("blocking_reasons")
    validation.require(
        isinstance(blockers, list) and all(isinstance(item, str) and bool(item.strip()) for item in blockers),
        "Gate 0 manifest: blocking reasons must be strings",
    )
    typed_blockers = tuple(blockers) if isinstance(blockers, list) else ()
    status = manifest.get("status")
    attestations = manifest.get("external_attestations")
    missing = (
        [item for item in attestations if isinstance(item, dict) and item.get("status") != "accepted"]
        if isinstance(attestations, list)
        else []
    )
    candidate_states = [refs.get(issue, {}).get("state") for issue in CANDIDATE_ISSUES]
    if status == "accepted":
        validation.require(not typed_blockers, "Gate 0 manifest: accepted state retains blocking reasons")
        validation.require(not missing, "Gate 0 manifest: accepted state has missing external attestations")
        validation.require(
            candidate_states == ["accepted", "accepted", "accepted"],
            "Gate 0 manifest: accepted state has unaccepted issue artifacts",
        )
    else:
        validation.require(bool(typed_blockers), "Gate 0 manifest: candidate must preserve blocking reasons")
        validation.require(
            bool(missing) or any(state != "accepted" for state in candidate_states),
            "Gate 0 manifest: blocked state has no unresolved evidence",
        )
        for issue in CANDIDATE_ISSUES:
            unresolved = refs.get(issue, {}).get("state") != "accepted" or any(
                item.get("issue") == issue for item in missing
            )
            if unresolved:
                validation.require(
                    any(reason.startswith(f"#{issue}") for reason in typed_blockers),
                    f"Gate 0 manifest: missing explicit blocker for issue #{issue}",
                )
    return typed_blockers


def validate_gate0_candidate(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    root: Path = ROOT,
    check_live_comments: bool = False,
    require_accepted: bool = False,
    comment_fetcher: CommentFetcher | None = None,
) -> ValidationResult:
    validation = Validation()
    path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    validation.require(path.is_file(), f"Gate 0 manifest does not exist: {path}")
    if not path.is_file():
        return ValidationResult(tuple(validation.errors), (), False)
    try:
        manifest = _load_json(path)
    except json.JSONDecodeError as error:
        return ValidationResult((f"Gate 0 manifest is invalid JSON: {error}",), (), False)
    _validate_manifest_shape(validation, manifest)
    if not isinstance(manifest, dict):
        return ValidationResult(tuple(validation.errors), (), False)
    _validate_successor_manifest(validation, root=root, manifest=manifest)
    try:
        manifest_relative = path.relative_to(root)
    except ValueError:
        validation.require(False, "Gate 0 manifest must be inside the repository root")
        return ValidationResult(tuple(validation.errors), (), False)
    patterns = manifest.get("paths")
    if isinstance(patterns, list) and all(isinstance(pattern, str) for pattern in patterns):
        try:
            authorized_files = manifest_authorized_files(root, patterns, manifest_path=manifest_relative)
            for pattern in patterns:
                validation.require(
                    any(path_matches(authorized_file, pattern) for authorized_file in authorized_files),
                    f"Gate 0 manifest: authorized path pattern matches no file: {pattern!r}",
                )
            if manifest.get("manifest_version") == 4:
                _validate_frozen_tree_proof(
                    validation,
                    root=root,
                    reference={"path": V4_FROZEN_PROOF_PATH, "sha256": V4_FROZEN_PROOF_SHA256},
                    predecessor_path=DEFAULT_MANIFEST.as_posix(),
                    predecessor_sha=_sha256_file(path),
                    predecessor=manifest,
                    predecessor_version=4,
                    candidate_commit=V4_FROZEN_COMMIT_SHA,
                )
                if frozen_commit_available(V4_FROZEN_COMMIT_SHA):
                    historical_tree = frozen_candidate_tree_sha256(V4_FROZEN_COMMIT_SHA, V4_MANIFEST_PATHS)
                    validation.require(
                        historical_tree == V4_FROZEN_TREE_SHA256,
                        "Gate 0 v4 manifest: frozen Git tree identity changed",
                    )
                    validation.require(
                        manifest.get("candidate_tree_sha256") == historical_tree,
                        "Gate 0 v4 manifest: stored tree does not match its frozen Git identity",
                    )
            else:
                validation.require(
                    manifest.get("candidate_tree_sha256")
                    == candidate_tree_sha256(root, patterns, manifest_path=manifest_relative),
                    "Gate 0 manifest: candidate tree SHA-256 mismatch",
                )
        except ValueError as error:
            validation.require(False, f"Gate 0 manifest: {error}")
    artifact_fetcher = comment_fetcher if check_live_comments else None
    if check_live_comments and artifact_fetcher is None:
        artifact_fetcher = _github_comment_fetcher
    attestation_fetcher = comment_fetcher if (check_live_comments or require_accepted) else None
    if (check_live_comments or require_accepted) and attestation_fetcher is None:
        attestation_fetcher = _github_comment_fetcher
    refs, payloads = _validate_artifacts(validation, root=root, manifest=manifest, fetcher=artifact_fetcher)
    _validate_dependency_hashes(validation, refs=refs, payloads=payloads)
    _validate_external_attestations(
        validation,
        manifest=manifest,
        refs=refs,
        fetcher=attestation_fetcher,
    )
    blockers = _validate_state_and_blockers(validation, manifest=manifest, refs=refs)
    accepted = manifest.get("status") == "accepted" and not blockers and not validation.errors
    if require_accepted:
        validation.require(accepted, "Gate 0 candidate is valid but not accepted")
    return ValidationResult(tuple(validation.errors), blockers, accepted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--check-live-comments", action="store_true")
    parser.add_argument("--require-accepted", action="store_true")
    args = parser.parse_args()
    manifest_path = args.manifest
    if manifest_path is None:
        candidates = sorted(
            (ROOT / "governance/gate0").glob("manifest-v*.json"),
            key=lambda path: int(path.stem.removeprefix("manifest-v")),
        )
        manifest_path = candidates[-1].relative_to(ROOT) if candidates else DEFAULT_MANIFEST
    result = validate_gate0_candidate(
        manifest_path,
        check_live_comments=args.check_live_comments,
        require_accepted=args.require_accepted,
    )
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if result.accepted:
        print("Gate 0 candidate chain is accepted")
    else:
        print(f"Gate 0 candidate chain is valid but blocked ({len(result.blockers)} blocking reasons)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
