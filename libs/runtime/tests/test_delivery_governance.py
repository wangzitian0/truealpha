import hashlib
import importlib.util
import json
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[3] / "tools" / "check_delivery_governance.py"
SPEC = importlib.util.spec_from_file_location("truealpha_delivery_governance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
governance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = governance
SPEC.loader.exec_module(governance)


def _valid_evidence():
    return {
        "schema_version": 1,
        "evidence_id": "capability-evidence:issue-99:v1",
        "issue": 99,
        "state": "accepted",
        "accepted_rung": "E2",
        "producer_commit": "0" * 40,
        "source_pr": 100,
        "accepted_by": "reviewer",
        "accepted_at": "2026-07-13T00:00:00Z",
        "commands": ["make check"],
        "attestation_ref": "https://example.test/attestation",
        "git_objects": [{"path": "artifact.json", "oid": "1" * 40}],
        "claim_ceiling": "E2 contract handoff",
        "residual_risks": [],
    }


def _validate_evidence(tmp_path, monkeypatch, evidence):
    evidence_dir = tmp_path / "governance" / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence_path = evidence_dir / "issue-99.v1.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(governance, "git_object", lambda _commit, _path: "1" * 40)
    validation = governance.Validation()
    governance.validate_capability_evidence(
        validation,
        99,
        "E2",
        {"path": "governance/evidence/issue-99.v1.json", "sha256": digest},
    )
    return validation


def test_capability_evidence_requires_nonempty_git_objects(tmp_path, monkeypatch):
    evidence = _valid_evidence()
    evidence["git_objects"] = []

    validation = _validate_evidence(tmp_path, monkeypatch, evidence)

    assert "issue #99: evidence git_objects are missing" in validation.errors


def test_capability_evidence_rejects_non_object_git_entry(tmp_path, monkeypatch):
    evidence = _valid_evidence()
    evidence["git_objects"] = ["artifact.json"]

    validation = _validate_evidence(tmp_path, monkeypatch, evidence)

    assert "issue #99: git_objects[0] must be an object" in validation.errors


def test_capability_evidence_rejects_missing_oid(tmp_path, monkeypatch):
    evidence = _valid_evidence()
    evidence["git_objects"] = [{"path": "missing.json"}]

    validation = _validate_evidence(tmp_path, monkeypatch, evidence)

    assert "issue #99: git_objects[0] oid is invalid" in validation.errors


def test_capability_evidence_rejects_commands_string(tmp_path, monkeypatch):
    evidence = _valid_evidence()
    evidence["commands"] = "make check"

    validation = _validate_evidence(tmp_path, monkeypatch, evidence)

    assert "issue #99: evidence commands must be a non-empty string list" in validation.errors


def test_capability_evidence_accepts_resolved_git_objects(tmp_path, monkeypatch):
    validation = _validate_evidence(tmp_path, monkeypatch, _valid_evidence())

    assert validation.errors == []


def test_gate_order_is_independent_of_json_object_order():
    gates = {
        29: {"status": "queued"},
        56: {"status": "active"},
    }
    validation = governance.Validation()

    order = governance.validate_gate_order(validation, gates, [56, 29])

    assert order == [56, 29]
    assert validation.errors == []


def test_empty_gate_order_fails_without_raising():
    validation = governance.Validation()

    order = governance.validate_gate_order(validation, {56: {"status": "active"}}, [])

    assert order == []
    assert "gate_order must not be empty" in validation.errors


def test_gate_order_rejects_duplicates_and_missing_gate_ids():
    validation = governance.Validation()

    order = governance.validate_gate_order(
        validation,
        {56: {"status": "active"}, 29: {"status": "queued"}},
        [56, 56],
    )

    assert order == []
    assert "gate_order must not contain duplicates" in validation.errors
    assert "gate_order must contain every Gate ID exactly once" in validation.errors


def test_gate_order_rejects_boolean_and_string_ids():
    validation = governance.Validation()

    order = governance.validate_gate_order(
        validation,
        {56: {"status": "active"}, 29: {"status": "queued"}},
        [True, "29"],
    )

    assert order == []
    assert "gate_order entries must be integer issue IDs" in validation.errors


def test_manifest_paths_reject_forbidden_integration_overlap():
    validation = governance.Validation()

    governance.validate_manifest_paths(
        validation,
        "D0",
        {
            "writable": ["apps/data-engine/src/data_engine/batches/**"],
            "read_only": [],
            "forbidden": ["db/**"],
            "integration_surface": ["db/migrations/**"],
            "lease_owner": None,
        },
    )

    assert any("forbidden path 'db/**' overlaps integration surface" in error for error in validation.errors)


def test_path_overlap_detects_broad_and_narrow_globs():
    assert governance.path_patterns_overlap("libs/factors/**", "libs/factors/src/**")
    assert not governance.path_patterns_overlap("libs/factors/**", "apps/data-engine/**")


def test_manifest_paths_reject_mid_segment_wildcards():
    validation = governance.Validation()

    governance.validate_manifest_paths(
        validation,
        "S0",
        {
            "writable": ["libs/factors/*.py"],
            "read_only": [],
            "forbidden": [],
            "integration_surface": [],
            "lease_owner": None,
        },
    )

    assert "S0: path pattern 'libs/factors/*.py' must be exact or end with '/**'" in validation.errors
