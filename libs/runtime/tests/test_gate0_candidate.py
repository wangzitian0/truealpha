import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools" / "check_gate0_candidate.py"
SPEC = importlib.util.spec_from_file_location("truealpha_gate0_candidate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate0 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate0
SPEC.loader.exec_module(gate0)

MANIFEST_PATH = Path("governance/gate0/manifest-v4.json")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def candidate_root(tmp_path):
    for pattern in gate0.EXPECTED_MANIFEST_PATHS:
        if pattern.endswith("/**"):
            relative = Path(pattern.removesuffix("/**"))
            shutil.copytree(REPO_ROOT / relative, tmp_path / relative, dirs_exist_ok=True)
        else:
            source = REPO_ROOT / pattern
            destination = tmp_path / pattern
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    evidence_dir = tmp_path / "governance" / "evidence"
    evidence_dir.mkdir(parents=True)
    for filename in ("issue-57.v1.json", "issue-58.v2.json"):
        shutil.copy2(REPO_ROOT / "governance" / "evidence" / filename, evidence_dir / filename)
    return tmp_path


def _manifest(root: Path):
    return _load(root / MANIFEST_PATH)


def _artifact_ref(manifest, issue: int):
    return next(item for item in manifest["artifacts"] if item["issue"] == issue)


def _refresh_chain(root: Path) -> None:
    manifest_path = root / MANIFEST_PATH
    manifest = _load(manifest_path)

    for issue in (57, 58):
        ref = _artifact_ref(manifest, issue)
        ref["sha256"] = _sha256(root / ref["path"])

    issue59_ref = _artifact_ref(manifest, 59)
    issue59_path = root / issue59_ref["path"]
    issue59 = _load(issue59_path)
    foundation_refs = {item["issue"]: item for item in manifest["artifacts"] if item["issue"] in {57, 58}}
    for dependency in issue59["dependencies"]:
        dependency["artifact"] = foundation_refs[dependency["issue"]]["path"]
        dependency["sha256"] = foundation_refs[dependency["issue"]]["sha256"]
    _write(issue59_path, issue59)
    issue59_ref["sha256"] = _sha256(issue59_path)

    issue60_ref = _artifact_ref(manifest, 60)
    issue60_path = root / issue60_ref["path"]
    issue60 = _load(issue60_path)
    issue60["depends_on"]["artifact"] = issue59_ref["path"]
    issue60["depends_on"]["sha256"] = issue59_ref["sha256"]
    _write(issue60_path, issue60)
    issue60_ref["sha256"] = _sha256(issue60_path)

    issue61_ref = _artifact_ref(manifest, 61)
    issue61_path = root / issue61_ref["path"]
    issue61 = _load(issue61_path)
    predecessor_refs = {59: issue59_ref, 60: issue60_ref}
    for dependency in issue61["dependencies"]:
        dependency["artifact"] = predecessor_refs[dependency["issue"]]["path"]
        dependency["sha256"] = predecessor_refs[dependency["issue"]]["sha256"]
    _write(issue61_path, issue61)
    issue61_ref["sha256"] = _sha256(issue61_path)

    target_by_issue = {
        59: issue59_ref["sha256"],
        60: issue60_ref["sha256"],
        61: issue61_ref["sha256"],
    }
    for attestation in manifest["external_attestations"]:
        attestation["target_sha256"] = target_by_issue[attestation["issue"]]
    manifest["candidate_payload_sha256"] = gate0.candidate_payload_sha256(manifest)
    manifest["candidate_tree_sha256"] = gate0.candidate_tree_sha256(root, manifest["paths"])
    _write(manifest_path, manifest)


def _validate(root: Path, **kwargs):
    return gate0.validate_gate0_candidate(MANIFEST_PATH, root=root, **kwargs)


def _create_v5_successor(root: Path) -> Path:
    for relative in set(gate0.SUCCESSOR_MANIFEST_PATHS) - set(gate0.V4_MANIFEST_PATHS):
        source = REPO_ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    predecessor_path = root / MANIFEST_PATH
    manifest = _load(predecessor_path)
    manifest.update(
        {
            "manifest_id": "gate-0-batch-v5",
            "manifest_version": 5,
            "integration_branch": "batch/gate-0-v5-governed-access-successor",
            "paths": list(gate0.SUCCESSOR_MANIFEST_PATHS),
            "predecessor_manifest": {
                "manifest_id": "gate-0-batch-v4",
                "manifest_version": 4,
                "path": MANIFEST_PATH.as_posix(),
                "sha256": _sha256(predecessor_path),
            },
            "integration_bindings": [
                {"role": role, "path": path, "sha256": _sha256(root / path)}
                for role, path in gate0.SUCCESSOR_BINDINGS.items()
            ],
        }
    )
    manifest["candidate_payload_sha256"] = gate0.candidate_payload_sha256(manifest)
    successor_path = root / "governance/gate0/manifest-v5.json"
    _write(successor_path, manifest)
    manifest["candidate_tree_sha256"] = gate0.candidate_tree_sha256(
        root,
        manifest["paths"],
        manifest_path=Path("governance/gate0/manifest-v5.json"),
    )
    _write(successor_path, manifest)
    return successor_path


def test_checked_in_candidate_is_valid_but_blocked(candidate_root):
    result = _validate(candidate_root)

    assert result.valid
    assert not result.accepted
    assert len(result.blockers) == 4


def test_checked_in_v4_resolves_the_exact_historical_git_tree() -> None:
    result = gate0.validate_gate0_candidate(MANIFEST_PATH, root=REPO_ROOT)

    assert result.valid
    assert gate0.resolve_frozen_candidate_commit(
        REPO_ROOT,
        gate0.V4_MANIFEST_PATHS,
        gate0.V4_FROZEN_TREE_SHA256,
        manifest_path=MANIFEST_PATH,
    ) is not None


def test_checked_in_v4_rejects_missing_historical_git_tree(monkeypatch) -> None:
    monkeypatch.setattr(gate0, "resolve_frozen_candidate_commit", lambda *args, **kwargs: None)

    result = gate0.validate_gate0_candidate(MANIFEST_PATH, root=REPO_ROOT)

    assert "Gate 0 v4 manifest: exact frozen Git tree is not reachable" in result.errors


def test_v5_successor_is_valid_and_preserves_v4(candidate_root):
    v4_tree = gate0.candidate_tree_sha256(candidate_root, gate0.V4_MANIFEST_PATHS)
    successor = _create_v5_successor(candidate_root)

    v4_result = _validate(candidate_root)
    v5_result = gate0.validate_gate0_candidate(successor.relative_to(candidate_root), root=candidate_root)

    assert v4_result.valid
    assert v5_result.valid
    assert not v5_result.accepted
    assert gate0.candidate_tree_sha256(candidate_root, gate0.V4_MANIFEST_PATHS) == v4_tree


def test_v5_successor_rejects_predecessor_byte_drift(candidate_root):
    successor = _create_v5_successor(candidate_root)
    predecessor = candidate_root / MANIFEST_PATH
    predecessor.write_bytes(predecessor.read_bytes() + b"\n")

    result = gate0.validate_gate0_candidate(successor.relative_to(candidate_root), root=candidate_root)

    assert "Gate 0 predecessor: file SHA-256 mismatch" in result.errors


def test_v5_successor_rejects_bound_integration_drift(candidate_root):
    successor = _create_v5_successor(candidate_root)
    architecture = candidate_root / "init.md"
    architecture.write_bytes(architecture.read_bytes() + b"\n")

    result = gate0.validate_gate0_candidate(successor.relative_to(candidate_root), root=candidate_root)

    assert "Gate 0 integration binding[0]: file SHA-256 mismatch" in result.errors


def test_v5_successor_cannot_drop_inherited_blockers(candidate_root):
    successor = _create_v5_successor(candidate_root)
    manifest = _load(successor)
    manifest["blocking_reasons"] = manifest["blocking_reasons"][:-1]
    manifest["candidate_payload_sha256"] = gate0.candidate_payload_sha256(manifest)
    _write(successor, manifest)

    result = gate0.validate_gate0_candidate(successor.relative_to(candidate_root), root=candidate_root)

    assert (
        "Gate 0 successor: blocked predecessor field changed: blocking_reasons"
        in result.errors
    )


def test_require_accepted_rejects_valid_blocked_candidate(candidate_root):
    result = _validate(candidate_root, require_accepted=True)

    assert "Gate 0 candidate is valid but not accepted" in result.errors


def test_require_accepted_forces_live_external_attestation_validation(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    attestation = manifest["external_attestations"][0]
    attestation["status"] = "accepted"
    attestation["ref"] = "https://github.com/wangzitian0/truealpha/issues/59#issuecomment-999"
    _write(path, manifest)
    calls = []

    def fetch(comment_id):
        calls.append(comment_id)
        return {
            "id": comment_id,
            "html_url": attestation["ref"],
            "body": "approve",
            "user": {"login": "fake-author"},
        }

    result = _validate(candidate_root, require_accepted=True, comment_fetcher=fetch)

    assert calls == [999]
    assert any("live approval does not bind target SHA-256" in error for error in result.errors)
    assert any("product-owner approval must come from wangzitian0" in error for error in result.errors)


def test_candidate_v1_cannot_be_accepted_by_status_flip(candidate_root):
    manifest = _manifest(candidate_root)
    path = candidate_root / _artifact_ref(manifest, 59)["path"]
    artifact = _load(path)
    artifact["state"] = "accepted"
    artifact["blocking_reasons"] = []
    _write(path, artifact)
    _refresh_chain(candidate_root)
    manifest_path = candidate_root / MANIFEST_PATH
    manifest = _load(manifest_path)
    _artifact_ref(manifest, 59)["state"] = "accepted"
    manifest["candidate_payload_sha256"] = gate0.candidate_payload_sha256(manifest)
    _write(manifest_path, manifest)

    result = _validate(candidate_root)

    assert any("candidate-v1 is proposal evidence and can never be accepted" in error for error in result.errors)


def test_issue59_external_attestations_bind_materialized_artifact(candidate_root):
    _refresh_chain(candidate_root)
    manifest = _manifest(candidate_root)
    issue59 = _artifact_ref(manifest, 59)

    assert {item["target_sha256"] for item in manifest["external_attestations"] if item["issue"] == 59} == {
        issue59["sha256"]
    }


def test_unexpected_artifact_field_fails_closed(candidate_root):
    manifest = _manifest(candidate_root)
    path = candidate_root / _artifact_ref(manifest, 59)["path"]
    artifact = _load(path)
    artifact["manual_ready_override"] = True
    _write(path, artifact)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert any(
        "issue #59 artifact: fields differ" in error and "manual_ready_override" in error for error in result.errors
    )


def test_artifact_byte_drift_is_rejected(candidate_root):
    _refresh_chain(candidate_root)
    manifest = _manifest(candidate_root)
    path = candidate_root / _artifact_ref(manifest, 60)["path"]
    path.write_bytes(path.read_bytes() + b"\n")

    result = _validate(candidate_root)

    assert "Gate 0 artifact[3]: file SHA-256 mismatch" in result.errors


def test_candidate_payload_hash_cannot_be_manually_flipped(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    manifest["candidate_payload_sha256"] = "0" * 64
    _write(path, manifest)

    result = _validate(candidate_root)

    assert "Gate 0 manifest: candidate payload SHA-256 mismatch" in result.errors


def test_candidate_tree_binds_non_artifact_authorized_files(candidate_root):
    successor = _create_v5_successor(candidate_root)
    architecture = candidate_root / "docs/architecture-contract-closure.md"
    architecture.write_bytes(architecture.read_bytes() + b"\n")

    result = gate0.validate_gate0_candidate(successor.relative_to(candidate_root), root=candidate_root)

    assert "Gate 0 manifest: candidate tree SHA-256 mismatch" in result.errors


def test_successor_acceptance_may_advance_fan_in_state(candidate_root) -> None:
    successor_path = _create_v5_successor(candidate_root)
    successor = _load(successor_path)
    successor["status"] = "accepted"
    successor["blocking_reasons"] = []
    for artifact in successor["artifacts"]:
        artifact["state"] = "accepted"
    for index, attestation in enumerate(successor["external_attestations"], start=1):
        attestation["status"] = "accepted"
        attestation["ref"] = f"https://github.com/wangzitian0/truealpha/issues/{attestation['issue']}#issuecomment-{index}"
    _write(successor_path, successor)

    validation = gate0.Validation()
    gate0._validate_successor_manifest(validation, root=candidate_root, manifest=successor)

    assert not any("predecessor field changed" in error for error in validation.errors)
    assert "Gate 0 successor: external blockers must remain active" not in validation.errors


def test_manifest_bytes_are_excluded_from_candidate_tree(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    before = gate0.candidate_tree_sha256(candidate_root, manifest["paths"])
    path.write_bytes(path.read_bytes() + b"\n")

    after = gate0.candidate_tree_sha256(candidate_root, manifest["paths"])

    assert after == before


def test_mutable_delivery_graph_is_excluded_from_candidate_tree(candidate_root):
    _refresh_chain(candidate_root)
    manifest = _load(candidate_root / MANIFEST_PATH)
    before = gate0.candidate_tree_sha256(candidate_root, manifest["paths"])
    graph = candidate_root / "governance/vision-issue-graph.json"
    graph.write_text('{"schema_version": 1}\n', encoding="utf-8")

    graph.write_text('{"schema_version": 1, "issues": {"229": {}}}\n', encoding="utf-8")
    after = gate0.candidate_tree_sha256(candidate_root, manifest["paths"])

    assert after == before


def test_authorized_path_set_cannot_be_broadened(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    manifest["paths"].append("libs/**")
    manifest["candidate_tree_sha256"] = gate0.candidate_tree_sha256(candidate_root, manifest["paths"])
    _write(path, manifest)

    result = _validate(candidate_root)

    assert "Gate 0 manifest: authorized path set changed" in result.errors


def test_foundation_must_be_real_accepted_terminal_evidence(candidate_root):
    _refresh_chain(candidate_root)
    manifest = _manifest(candidate_root)
    foundation_path = candidate_root / _artifact_ref(manifest, 57)["path"]
    foundation = _load(foundation_path)
    foundation["state"] = "candidate_unapproved"
    _write(foundation_path, foundation)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert "issue #57 foundation: evidence is not accepted" in result.errors


def test_reverse_or_skipped_dependency_edge_is_rejected(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    _artifact_ref(manifest, 60)["depends_on"] = [61]
    manifest["candidate_payload_sha256"] = gate0.candidate_payload_sha256(manifest)
    _write(path, manifest)

    result = _validate(candidate_root)

    assert "Gate 0 artifact[3]: dependency edge changed" in result.errors


def test_scope_shrink_and_alphabet_share_class_collapse_are_rejected(candidate_root):
    manifest = _manifest(candidate_root)
    path = candidate_root / _artifact_ref(manifest, 59)["path"]
    artifact = _load(path)
    artifact["scope"]["minimums"]["instruments"] = 20
    artifact["scope"]["selected_instrument_cusips"].remove("02079K305")
    artifact["scope"]["selected_instruments"] = [
        item for item in artifact["scope"]["selected_instruments"] if item["cusip"] != "02079K305"
    ]
    _write(path, artifact)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert "issue #59: exact scope minimums changed" in result.errors
    assert "issue #59: exact 21-instrument scope changed" in result.errors
    assert "issue #59: Alphabet share classes were collapsed" in result.errors


def test_alias_removal_cannot_narrow_the_candidate(candidate_root):
    manifest = _manifest(candidate_root)
    path = candidate_root / _artifact_ref(manifest, 59)["path"]
    artifact = _load(path)
    artifact["catalog"]["required_aliases"].remove("supply-chain")
    _write(path, artifact)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert "issue #59: exact aliases changed" in result.errors


def test_missing_attestation_cannot_contain_plausible_identity(candidate_root):
    manifest = _manifest(candidate_root)
    path = candidate_root / _artifact_ref(manifest, 59)["path"]
    artifact = _load(path)
    artifact["attestations"]["independent_review"]["reviewer"] = "reviewer:plausible-human"
    _write(path, artifact)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert any("missing attestation fabricates reviewer" in error for error in result.errors)


def test_accepted_external_attestation_requires_exact_comment_url(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    attestation = manifest["external_attestations"][0]
    attestation["status"] = "accepted"
    attestation["ref"] = "fixture:synthetic-product-owner"
    _write(path, manifest)

    result = _validate(candidate_root)

    assert any("accepted ref is not an exact repository issue-comment URL" in error for error in result.errors)


def test_live_comment_hash_mismatch_is_rejected(candidate_root):
    _refresh_chain(candidate_root)

    def wrong_comment(comment_id):
        return {
            "id": comment_id,
            "html_url": f"https://github.com/wangzitian0/truealpha/issues/59#issuecomment-{comment_id}",
            "body": "mutated",
            "user": {"login": "human-reviewer"},
        }

    result = _validate(candidate_root, check_live_comments=True, comment_fetcher=wrong_comment)

    assert any("live comment SHA-256 mismatch" in error for error in result.errors)


def test_candidate_must_preserve_issue_specific_blockers(candidate_root):
    _refresh_chain(candidate_root)
    path = candidate_root / MANIFEST_PATH
    manifest = _load(path)
    manifest["blocking_reasons"] = ["the candidate is blocked"]
    _write(path, manifest)

    result = _validate(candidate_root)

    assert "Gate 0 manifest: missing explicit blocker for issue #59" in result.errors
    assert "Gate 0 manifest: missing explicit blocker for issue #60" in result.errors
    assert "Gate 0 manifest: missing explicit blocker for issue #61" in result.errors


def test_public_golden_manifest_and_case_hashes_are_transitive(candidate_root):
    _refresh_chain(candidate_root)
    golden = candidate_root / "governance/gate0/public-goldens/gppe/boundary-1000000.expected.json"
    golden.write_bytes(golden.read_bytes() + b"\n")

    result = _validate(candidate_root)

    assert any("public golden manifest case" in error and "file SHA-256 mismatch" in error for error in result.errors)


def test_public_golden_case_target_cannot_be_relabelled(candidate_root):
    manifest = _manifest(candidate_root)
    issue59_path = candidate_root / _artifact_ref(manifest, 59)["path"]
    issue59 = _load(issue59_path)
    golden_reference = issue59["evaluation"]["public_golden_manifest"]
    golden_path = candidate_root / golden_reference["path"]
    golden_manifest = _load(golden_path)
    golden_manifest["cases"][0]["target"] = "analyst-backtest"
    _write(golden_path, golden_manifest)
    golden_reference["sha256"] = _sha256(golden_path)
    _write(issue59_path, issue59)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert any("target disagrees with case" in error for error in result.errors)


def test_public_golden_child_path_cannot_be_reused(candidate_root):
    manifest = _manifest(candidate_root)
    issue59_path = candidate_root / _artifact_ref(manifest, 59)["path"]
    issue59 = _load(issue59_path)
    golden_reference = issue59["evaluation"]["public_golden_manifest"]
    golden_path = candidate_root / golden_reference["path"]
    golden_manifest = _load(golden_path)
    first_input = golden_manifest["cases"][0]["artifacts"]["input"]
    golden_manifest["cases"][1]["artifacts"]["input"] = first_input
    _write(golden_path, golden_manifest)
    golden_reference["sha256"] = _sha256(golden_path)
    _write(issue59_path, issue59)
    _refresh_chain(candidate_root)

    result = _validate(candidate_root)

    assert any("child artifact path is reused" in error for error in result.errors)
