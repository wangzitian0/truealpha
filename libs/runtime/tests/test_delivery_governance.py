import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _reference(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _rung_evidence(*, batch_id: str = "producer-batch", head_sha: str = "2" * 40) -> dict:
    content = {
        "schema_version": 1,
        "batch_id": batch_id,
        "manifest_sha256": "3" * 64,
        "accepted_rung": "E2",
        "base_sha": "1" * 40,
        "producer_head_sha": head_sha,
        "commands": [
            {
                "command": "make check",
                "exit_code": 0,
                "output_sha256": "4" * 64,
            }
        ],
        "negative_controls": ["release remains isolated"],
        "stable_handoff": False,
        "created_at": "2026-07-13T00:00:00Z",
    }
    return {
        "evidence_id": f"rung-evidence:{batch_id}:{governance.canonical_sha256(content)}",
        **content,
    }


def _validate_handoff(tmp_path, monkeypatch, mutation=None):
    producer_head = "2" * 40
    evidence_path = tmp_path / "governance" / "evidence" / "producer-e2.json"
    _write_json(evidence_path, _rung_evidence(head_sha=producer_head))
    evidence = [_reference(tmp_path, evidence_path)]
    content = {
        "schema_version": 1,
        "revision": 1,
        "state": "accepted",
        "producer": {
            "batch_id": "producer-batch",
            "issue": 99,
            "owner": "implementer",
            "head_sha": producer_head,
        },
        "schema_epoch": "v1",
        "readiness_ceiling": "E2",
        "evidence": evidence,
        "allowed_consumers": ["consumer-batch"],
        "allowed_environments": ["local"],
        "retention": "permanent",
        "verification": {
            "reviewer": "reviewer",
            "evidence_sha256": governance.canonical_sha256(evidence),
            "accepted_at": "2026-07-13T00:00:00Z",
            "attestation_ref": "https://example.test/attestation",
        },
        "revocation": {"reason": None, "revoked_at": None, "superseded_by": None},
    }
    if mutation is not None:
        mutation(content)
    handoff = {
        "handoff_id": f"handoff:producer:{governance.canonical_sha256(content)}",
        **content,
    }
    handoff_path = tmp_path / "governance" / "handoffs" / "producer.json"
    _write_json(handoff_path, handoff)
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(governance, "git_commit_exists", lambda _commit: True)
    validation = governance.Validation()
    governance.validate_handoff_dependency(
        validation,
        "consumer-batch",
        {"corpus": {"environment": "local fixture"}},
        {"issue": 99, "handoff_manifest": _reference(tmp_path, handoff_path)},
    )
    return validation


def test_handoff_accepts_bound_rung_evidence(tmp_path, monkeypatch):
    validation = _validate_handoff(tmp_path, monkeypatch)

    assert validation.errors == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda handoff: handoff["verification"].update(reviewer="implementer"),
            "handoff reviewer must be independent",
        ),
        (
            lambda handoff: handoff.update(state="revoked"),
            "HandoffManifest is not accepted",
        ),
        (
            lambda handoff: handoff.update(allowed_consumers=["another-batch"]),
            "handoff does not allow this consumer",
        ),
        (
            lambda handoff: handoff.update(allowed_environments=["staging"]),
            "handoff does not allow environment local",
        ),
    ],
)
def test_handoff_rejects_untrusted_state(tmp_path, monkeypatch, mutation, expected):
    validation = _validate_handoff(tmp_path, monkeypatch, mutation)

    assert any(expected in error for error in validation.errors)


def test_rung_evidence_rejects_missing_command_reports():
    evidence = _rung_evidence()
    evidence["commands"] = []
    validation = governance.Validation()

    governance.validate_rung_evidence(
        validation,
        owner="test",
        evidence=evidence,
        producer_batch="producer-batch",
        producer_head="2" * 40,
    )

    assert "test: command evidence is missing" in validation.errors


def test_repository_paths_fail_closed_on_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(governance, "ROOT", tmp_path)

    assert governance.repo_path("../outside.json") is None
    assert governance.repo_path("/tmp/outside.json") is None
    assert not governance.path_matches_pattern("../outside.json", "../**")


def _pr_context(
    tmp_path,
    monkeypatch,
    *,
    before: str = "queued",
    after: str = "prepared",
    changed_paths: tuple[str, ...] | None = None,
    corpus_sha: str | None = None,
):
    base_sha = "a" * 40
    head_sha = "b" * 40
    graph_path = tmp_path / "governance" / "vision-issue-graph.json"
    manifest_path = tmp_path / "governance" / "batches" / "D0.json"
    corpus_path = tmp_path / "fixtures" / "corpus.json"
    _write_json(corpus_path, {"rows": [1, 2, 3]})
    base_manifest = {
        "revision": 1,
        "status": before,
        "last_accepted_rung": None,
        "target_rung": "E0",
        "terminal_rung": "E1",
    }
    is_acceptance = before == "prepared" and after == "active"
    manifest = {
        "revision": 2,
        "status": after,
        "last_accepted_rung": "E0" if is_acceptance else None,
        "target_rung": "E1" if is_acceptance else "E0",
        "terminal_rung": "E1",
        "activation": {"base_sha": base_sha},
        "corpus": {
            "manifest_path": "fixtures/corpus.json",
            "sha256": corpus_sha or hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        },
        "paths": {
            "writable": ["src/**", "fixtures/**"],
            "read_only": ["reference/**"],
            "forbidden": ["db/**"],
            "integration_surface": [],
            "lease_owner": None,
        },
        "acceptance": {
            "commands": ["pytest -q"],
            "negative_controls": ["release isolation"],
        },
    }
    _write_json(manifest_path, manifest)
    base_graph = {
        "batches": {
            "D0": {
                "manifest": "governance/batches/D0.json",
                "status": before,
                "target_rung": "E0",
            }
        }
    }
    graph = {
        "batches": {
            "D0": {
                "manifest": "governance/batches/D0.json",
                "status": after,
                "target_rung": manifest["target_rung"],
            }
        }
    }
    _write_json(graph_path, graph)
    default_paths = (
        "fixtures/corpus.json",
        "governance/batches/D0.json",
        "governance/vision-issue-graph.json",
    )
    if is_acceptance:
        default_paths = (
            "governance/batches/D0.json",
            "governance/vision-issue-graph.json",
            "src/feature.py",
        )
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(governance, "GRAPH_PATH", graph_path)
    monkeypatch.setattr(governance, "git_commit_exists", lambda _commit: True)
    monkeypatch.setattr(governance, "git_merge_base", lambda _base, _head: base_sha)
    monkeypatch.setattr(governance, "git_changed_paths", lambda _base, _head: changed_paths or default_paths)
    monkeypatch.setattr(
        governance,
        "git_json",
        lambda _commit, path: base_graph if path == "governance/vision-issue-graph.json" else base_manifest,
    )
    return graph, base_sha, head_sha


def _gate0_pr_context(
    tmp_path,
    monkeypatch,
    *,
    accepted: bool = False,
    changed_paths: tuple[str, ...] | None = None,
    base_sha: str | None = None,
    integration_base_sha: str | None = None,
    manifest_id: str = "gate-0-batch-v5",
    manifest_version: int = 5,
    paths: tuple[str, ...] = ("governance/gate0/**",),
    validation_calls: list[dict[str, bool]] | None = None,
):
    base_sha = base_sha or "a" * 40
    head_sha = "b" * 40
    manifest_path = tmp_path / "governance" / "gate0" / "manifest-v4.json"
    candidate_path = tmp_path / "governance" / "gate0" / "candidate.json"
    _write_json(candidate_path, {"candidate": True})
    base_manifest = {
        "manifest_id": manifest_id,
        "manifest_version": manifest_version,
        "status": "accepted" if accepted else "candidate_blocked_external_attestation",
        "integration_base_sha": "c" * 40,
        "candidate_tree_sha256": "d" * 64,
        "paths": list(paths),
        "blocking_reasons": [] if accepted else ["external attestation missing"],
    }
    _write_json(
        manifest_path,
        {
            **base_manifest,
            "integration_base_sha": integration_base_sha or base_sha,
            "candidate_tree_sha256": "e" * 64,
        },
    )
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(governance, "git_commit_exists", lambda _commit: True)
    monkeypatch.setattr(governance, "git_merge_base", lambda _base, _head: base_sha)
    monkeypatch.setattr(governance, "git_json", lambda _commit, _path: base_manifest)
    monkeypatch.setattr(
        governance,
        "git_changed_paths",
        lambda _base, _head: (
            changed_paths
            or (
                "governance/gate0/manifest-v4.json",
                "governance/gate0/candidate.json",
            )
        ),
    )

    def validate_candidate(_path, *, root, check_live_comments, require_accepted):
        assert root == tmp_path
        if validation_calls is not None:
            validation_calls.append(
                {
                    "check_live_comments": check_live_comments,
                    "require_accepted": require_accepted,
                }
            )
        errors = () if accepted or not require_accepted else ("Gate 0 candidate is valid but not accepted",)
        return type("GateResult", (), {"errors": errors})()

    monkeypatch.setattr(governance, "validate_gate0_candidate", validate_candidate)
    return {"batches": {}}, base_sha, head_sha


def test_preparation_pr_freezes_exact_corpus(tmp_path, monkeypatch):
    graph, base_sha, head_sha = _pr_context(tmp_path, monkeypatch)
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert validation.errors == []
    assert advance is not None and advance.accepted_rung is None


def test_preparation_pr_cannot_smuggle_implementation(tmp_path, monkeypatch):
    changed = (
        "fixtures/corpus.json",
        "governance/batches/D0.json",
        "governance/vision-issue-graph.json",
        "src/feature.py",
    )
    graph, base_sha, head_sha = _pr_context(tmp_path, monkeypatch, changed_paths=changed)
    validation = governance.Validation()

    governance.validate_pr_advance(validation, graph=graph, base_sha=base_sha, head_sha=head_sha)

    assert any("preparation PR may only freeze" in error for error in validation.errors)


def test_active_pr_accepts_exactly_one_rung(tmp_path, monkeypatch):
    graph, base_sha, head_sha = _pr_context(
        tmp_path,
        monkeypatch,
        before="prepared",
        after="active",
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(validation, graph=graph, base_sha=base_sha, head_sha=head_sha)

    assert validation.errors == []
    assert advance is not None and advance.accepted_rung == "E0"


def test_pr_rejects_stale_merge_base(tmp_path, monkeypatch):
    graph, base_sha, head_sha = _pr_context(tmp_path, monkeypatch)
    monkeypatch.setattr(governance, "git_merge_base", lambda _base, _head: "c" * 40)
    validation = governance.Validation()

    governance.validate_pr_advance(validation, graph=graph, base_sha=base_sha, head_sha=head_sha)

    assert "PR is stale: merge-base does not equal the declared current base" in validation.errors


def test_pr_rejects_fabricated_coordinates():
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph={"batches": {}},
        base_sha="not-a-sha",
        head_sha="also-not-a-sha",
    )

    assert advance is None
    assert "PR base/head must be full Git SHAs" in validation.errors


def test_pr_rejects_multiple_batch_manifests(monkeypatch):
    monkeypatch.setattr(governance, "git_commit_exists", lambda _commit: True)
    monkeypatch.setattr(governance, "git_merge_base", lambda base, _head: base)
    monkeypatch.setattr(
        governance,
        "git_changed_paths",
        lambda _base, _head: ("governance/batches/A.json", "governance/batches/B.json"),
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph={"batches": {}},
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert advance is None
    assert "PR must advance exactly one capability-batch manifest" in validation.errors


def test_gate0_aggregate_rejects_blocked_candidate_by_default(tmp_path, monkeypatch):
    calls: list[dict[str, bool]] = []
    graph, base_sha, head_sha = _gate0_pr_context(tmp_path, monkeypatch, validation_calls=calls)
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert any("Gate 0 candidate is valid but not accepted" in error for error in validation.errors)
    assert calls == [{"check_live_comments": True, "require_accepted": True}]


def test_draft_gate0_aggregate_allows_valid_blocked_candidate(tmp_path, monkeypatch):
    calls: list[dict[str, bool]] = []
    graph, base_sha, head_sha = _gate0_pr_context(tmp_path, monkeypatch, validation_calls=calls)
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
        allow_blocked_gate_candidate=True,
    )

    assert validation.errors == []
    assert advance is not None
    assert advance.kind == "gate_candidate"
    assert advance.accepted_rung is None
    assert calls == [{"check_live_comments": False, "require_accepted": False}]


def test_gate0_aggregate_accepts_complete_candidate_without_draft_escape(tmp_path, monkeypatch):
    calls: list[dict[str, bool]] = []
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        accepted=True,
        validation_calls=calls,
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert validation.errors == []
    assert advance is not None and advance.kind == "gate_candidate"
    assert calls == [{"check_live_comments": True, "require_accepted": True}]


def test_gate0_aggregate_rejects_stale_integration_base(tmp_path, monkeypatch):
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        accepted=True,
        integration_base_sha="c" * 40,
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert any("integration_base_sha does not match the PR base" in error for error in validation.errors)


def test_gate0_aggregate_rejects_path_outside_manifest_authorization(tmp_path, monkeypatch):
    changed_paths = (
        "governance/gate0/manifest-v4.json",
        "db/migrations/9999-smuggled.sql",
    )
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        accepted=True,
        changed_paths=changed_paths,
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert any("outside manifest authorization" in error for error in validation.errors)


def test_gate0_aggregate_cannot_mix_capability_batch_manifest(tmp_path, monkeypatch):
    changed_paths = (
        "governance/batches/D0.json",
        "governance/gate0/manifest-v4.json",
    )
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        accepted=True,
        changed_paths=changed_paths,
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert "PR cannot mix the Gate 0 aggregate manifest with capability-batch manifests" in validation.errors


def test_accepted_gate0_candidate_rejects_future_authorization_control_changes(tmp_path, monkeypatch):
    checker = tmp_path / "tools" / "check_gate0_candidate.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("# future validator\n", encoding="utf-8")
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        accepted=True,
        changed_paths=("governance/gate0/manifest-v4.json", "tools/check_gate0_candidate.py"),
        paths=("governance/gate0/**", "tools/check_gate0_candidate.py"),
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert any("accepted candidate modifies authorization controls" in error for error in validation.errors)


def test_gate0_v4_also_rejects_accepted_bootstrap_control_changes(tmp_path, monkeypatch):
    checker = tmp_path / "tools" / "check_gate0_candidate.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("# bootstrap validator\n", encoding="utf-8")
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        accepted=True,
        changed_paths=("governance/gate0/manifest-v4.json", "tools/check_gate0_candidate.py"),
        manifest_id="gate-0-batch-v4",
        manifest_version=4,
        paths=("governance/gate0/**", "tools/check_gate0_candidate.py"),
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert any("accepted candidate modifies authorization controls" in error for error in validation.errors)


def test_ready_blocked_gate0_candidate_may_rebind_reviewed_authorization_controls(tmp_path, monkeypatch):
    checker = tmp_path / "tools" / "check_delivery_governance.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("# reviewed validator\n", encoding="utf-8")
    calls: list[dict[str, bool]] = []
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        changed_paths=("governance/gate0/manifest-v4.json", "tools/check_delivery_governance.py"),
        paths=("governance/gate0/**", "tools/check_delivery_governance.py"),
        validation_calls=calls,
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert validation.errors == []
    assert advance is not None and advance.kind == "gate_candidate"
    assert calls == [{"check_live_comments": False, "require_accepted": False}]


def test_ready_blocked_gate0_control_rebind_rejects_candidate_payload_drift(tmp_path, monkeypatch):
    checker = tmp_path / "tools" / "check_delivery_governance.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("# reviewed validator\n", encoding="utf-8")
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        changed_paths=("governance/gate0/manifest-v4.json", "tools/check_delivery_governance.py"),
        paths=("governance/gate0/**", "tools/check_delivery_governance.py"),
    )
    manifest_path = tmp_path / "governance" / "gate0" / "manifest-v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["blocking_reasons"] = []
    _write_json(manifest_path, manifest)
    validation = governance.Validation()

    governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert any("may change only its base/tree binding" in error for error in validation.errors)


def test_draft_gate0_candidate_may_iterate_on_authorization_controls(tmp_path, monkeypatch):
    checker = tmp_path / "tools" / "check_gate0_candidate.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("# draft validator\n", encoding="utf-8")
    graph, base_sha, head_sha = _gate0_pr_context(
        tmp_path,
        monkeypatch,
        changed_paths=("governance/gate0/manifest-v4.json", "tools/check_gate0_candidate.py"),
        paths=("governance/gate0/**", "tools/check_gate0_candidate.py"),
    )
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
        allow_blocked_gate_candidate=True,
    )

    assert validation.errors == []
    assert advance is not None


def test_gate0_candidate_rejects_symlinked_candidate_file(tmp_path, monkeypatch):
    graph, base_sha, head_sha = _gate0_pr_context(tmp_path, monkeypatch, accepted=True)
    candidate = tmp_path / "governance" / "gate0" / "candidate.json"
    candidate.unlink()
    candidate.symlink_to("manifest-v4.json")
    validation = governance.Validation()

    advance = governance.validate_pr_advance(
        validation,
        graph=graph,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert advance is None
    assert any("candidate path is a symlink" in error for error in validation.errors)


def test_non_governance_code_pr_requires_batch_manifest(monkeypatch):
    monkeypatch.setattr(governance, "git_commit_exists", lambda _commit: True)
    monkeypatch.setattr(governance, "git_merge_base", lambda base, _head: base)
    monkeypatch.setattr(governance, "git_changed_paths", lambda _base, _head: ("apps/data-engine/feature.py",))
    validation = governance.Validation()

    governance.validate_pr_advance(
        validation,
        graph={"batches": {}},
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert "non-governance PR must advance exactly one capability-batch manifest" in validation.errors


def test_corpus_hash_must_match_bytes(tmp_path, monkeypatch):
    graph, base_sha, head_sha = _pr_context(tmp_path, monkeypatch, corpus_sha="0" * 64)
    validation = governance.Validation()

    governance.validate_pr_advance(validation, graph=graph, base_sha=base_sha, head_sha=head_sha)

    assert "D0: corpus bytes do not match the frozen hash" in validation.errors


@pytest.mark.parametrize(
    ("changed_path", "expected"),
    [
        ("db/001.sql", "changed forbidden path"),
        ("reference/input.json", "changed read-only path"),
        ("outside/file.py", "changed path is outside writable scope"),
    ],
)
def test_pr_paths_reject_unauthorized_changes(changed_path, expected):
    validation = governance.Validation()
    manifest = {
        "paths": {
            "writable": ["src/**"],
            "read_only": ["reference/**"],
            "forbidden": ["db/**"],
            "integration_surface": [],
        }
    }

    governance.validate_pr_paths(
        validation,
        batch_id="D0",
        manifest_path="governance/batches/D0.json",
        manifest=manifest,
        changed_paths=(changed_path,),
        base_sha="a" * 40,
    )

    assert any(expected in error for error in validation.errors)


def test_global_shared_surface_requires_declared_lease():
    validation = governance.Validation()

    governance.validate_pr_paths(
        validation,
        batch_id="D0",
        manifest_path="governance/batches/D0.json",
        manifest={
            "paths": {
                "writable": ["db/**"],
                "read_only": [],
                "forbidden": [],
                "integration_surface": [],
            }
        },
        changed_paths=("db/migrations/0019.sql",),
        base_sha="a" * 40,
    )

    assert "D0: integration lease: file reference must be an object" in validation.errors


def _validate_lease(tmp_path, monkeypatch, mutation=None):
    base_sha = "a" * 40
    content = {
        "schema_version": 1,
        "batch_id": "D0",
        "owner": "integrator",
        "state": "active",
        "paths": ["shared/**"],
        "base_sha": base_sha,
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    if mutation is not None:
        mutation(content)
    lease = {
        "lease_id": f"integration-lease:{governance.canonical_sha256(content)}",
        **content,
    }
    lease_path = tmp_path / "governance" / "leases" / "D0.json"
    _write_json(lease_path, lease)
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    validation = governance.Validation()
    governance.validate_integration_lease(
        validation,
        batch_id="D0",
        manifest={
            "paths": {
                "lease_owner": "integrator",
                "lease_manifest": _reference(tmp_path, lease_path),
            }
        },
        changed_integration_paths=("shared/export.py",),
        base_sha=base_sha,
    )
    return validation


def test_live_integration_lease_authorizes_shared_path(tmp_path, monkeypatch):
    validation = _validate_lease(tmp_path, monkeypatch)

    assert validation.errors == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda lease: lease.update(base_sha="b" * 40), "integration lease base SHA is stale"),
        (
            lambda lease: lease.update(expires_at="2020-01-01T00:00:00+00:00"),
            "integration lease is expired",
        ),
        (lambda lease: lease.update(paths=["other/**"]), "integration lease does not cover"),
    ],
)
def test_integration_lease_rejects_stale_or_uncovered_state(tmp_path, monkeypatch, mutation, expected):
    validation = _validate_lease(tmp_path, monkeypatch, mutation)

    assert any(expected in error for error in validation.errors)


def test_acceptance_commands_emit_head_bound_evidence(tmp_path, monkeypatch):
    head_sha = "b" * 40
    manifest_path = tmp_path / "governance" / "batches" / "D0.json"
    _write_json(manifest_path, {"manifest": "bytes"})
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(
        governance,
        "run_git",
        lambda *_args: subprocess.CompletedProcess([], 0, f"{head_sha}\n", ""),
    )
    advance = governance.PullRequestAdvance(
        batch_id="D0",
        manifest_path="governance/batches/D0.json",
        base_manifest={},
        manifest={
            "activation": {"base_sha": "a" * 40},
            "acceptance": {
                "commands": ["pytest -q"],
                "negative_controls": ["release isolation"],
            },
        },
        accepted_rung="E0",
        changed_paths=(),
    )
    validation = governance.Validation()
    output_path = tmp_path / "evidence.json"

    governance.execute_acceptance_commands(
        validation,
        advance=advance,
        head_sha=head_sha,
        output_path=output_path,
        runner=lambda command: subprocess.CompletedProcess([command], 0, "passed", ""),
    )

    evidence = json.loads(output_path.read_text(encoding="utf-8"))
    assert validation.errors == []
    assert evidence["producer_head_sha"] == head_sha
    assert evidence["commands"][0]["output_sha256"] == hashlib.sha256(b"passed\n").hexdigest()
    evidence_validation = governance.Validation()
    governance.validate_rung_evidence(
        evidence_validation,
        owner="D0",
        evidence=evidence,
        producer_batch="D0",
        producer_head=head_sha,
    )
    assert evidence_validation.errors == []


def test_gate0_acceptance_executes_manifest_commands_on_exact_head(tmp_path, monkeypatch):
    head_sha = "b" * 40
    manifest_path = tmp_path / "governance" / "gate0" / "manifest-v4.json"
    _write_json(manifest_path, {"manifest": "bytes"})
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(
        governance,
        "run_git",
        lambda *_args: subprocess.CompletedProcess([], 0, f"{head_sha}\n", ""),
    )
    advance = governance.PullRequestAdvance(
        batch_id="gate-0-v4",
        manifest_path="governance/gate0/manifest-v4.json",
        base_manifest={},
        manifest={
            "status": "accepted",
            "integration_base_sha": "a" * 40,
            "acceptance": {"commands": ["make gate0-candidate-acceptance", "make check"]},
        },
        accepted_rung=None,
        changed_paths=(),
        kind="gate_candidate",
    )
    validation = governance.Validation()
    output_path = tmp_path / "gate-evidence.json"
    commands: list[str] = []

    def run(command):
        commands.append(command)
        return subprocess.CompletedProcess([command], 0, "passed", "")

    governance.execute_acceptance_commands(
        validation,
        advance=advance,
        head_sha=head_sha,
        output_path=output_path,
        runner=run,
    )

    evidence = json.loads(output_path.read_text(encoding="utf-8"))
    assert validation.errors == []
    assert commands == ["make gate0-candidate-acceptance", "make check"]
    assert evidence["evidence_id"].startswith("gate-evidence:gate-0-v4:")
    assert evidence["base_sha"] == "a" * 40
    assert evidence["producer_head_sha"] == head_sha


def test_gate0_acceptance_rejects_mismatched_worktree_head(tmp_path, monkeypatch):
    manifest_path = tmp_path / "governance" / "gate0" / "manifest-v4.json"
    _write_json(manifest_path, {})
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(
        governance,
        "run_git",
        lambda *_args: subprocess.CompletedProcess([], 0, f"{'c' * 40}\n", ""),
    )
    advance = governance.PullRequestAdvance(
        batch_id="gate-0-v4",
        manifest_path="governance/gate0/manifest-v4.json",
        base_manifest={},
        manifest={
            "status": "accepted",
            "integration_base_sha": "a" * 40,
            "acceptance": {"commands": ["make check"]},
        },
        accepted_rung=None,
        changed_paths=(),
        kind="gate_candidate",
    )
    validation = governance.Validation()
    output_path = tmp_path / "gate-evidence.json"

    governance.execute_acceptance_commands(
        validation,
        advance=advance,
        head_sha="b" * 40,
        output_path=output_path,
        runner=lambda command: pytest.fail(f"unexpected command: {command}"),
    )

    assert any("acceptance commands are not running on the exact PR head" in error for error in validation.errors)
    assert not output_path.exists()


def test_blocked_draft_gate0_candidate_does_not_run_terminal_acceptance(tmp_path, monkeypatch):
    manifest_path = tmp_path / "governance" / "gate0" / "manifest-v4.json"
    _write_json(manifest_path, {})
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    advance = governance.PullRequestAdvance(
        batch_id="gate-0-v4",
        manifest_path="governance/gate0/manifest-v4.json",
        base_manifest={},
        manifest={
            "status": "candidate_blocked_external_attestation",
            "acceptance": {"commands": ["false"]},
        },
        accepted_rung=None,
        changed_paths=(),
        kind="gate_candidate",
    )
    validation = governance.Validation()
    output_path = tmp_path / "gate-evidence.json"

    governance.execute_acceptance_commands(
        validation,
        advance=advance,
        head_sha="b" * 40,
        output_path=output_path,
        runner=lambda command: pytest.fail(f"unexpected command: {command}"),
    )

    assert validation.errors == []
    assert not output_path.exists()


def test_failed_acceptance_command_emits_no_evidence(tmp_path, monkeypatch):
    head_sha = "b" * 40
    manifest_path = tmp_path / "governance" / "batches" / "D0.json"
    _write_json(manifest_path, {})
    monkeypatch.setattr(governance, "ROOT", tmp_path)
    monkeypatch.setattr(
        governance,
        "run_git",
        lambda *_args: subprocess.CompletedProcess([], 0, f"{head_sha}\n", ""),
    )
    advance = governance.PullRequestAdvance(
        batch_id="D0",
        manifest_path="governance/batches/D0.json",
        base_manifest={},
        manifest={
            "activation": {"base_sha": "a" * 40},
            "acceptance": {"commands": ["false"], "negative_controls": ["release isolation"]},
        },
        accepted_rung="E0",
        changed_paths=(),
    )
    validation = governance.Validation()
    output_path = tmp_path / "evidence.json"

    governance.execute_acceptance_commands(
        validation,
        advance=advance,
        head_sha=head_sha,
        output_path=output_path,
        runner=lambda command: subprocess.CompletedProcess([command], 1, "", "failed"),
    )

    assert "D0: acceptance command failed: false" in validation.errors
    assert not output_path.exists()


def test_completed_batch_can_rerun_its_exact_terminal_acceptance():
    terminal = {
        "status": "done",
        "last_accepted_rung": "E1",
        "target_rung": "E1",
        "terminal_rung": "E1",
    }
    validation = governance.Validation()

    accepted_rung = governance.validate_status_transition(
        validation,
        batch_id="D0",
        base_manifest=terminal,
        manifest=terminal,
    )

    assert validation.errors == []
    assert accepted_rung == "E1"


def test_blocked_terminal_batch_can_accept_target_and_finish():
    blocked = {
        "status": "blocked",
        "last_accepted_rung": "E2",
        "target_rung": "E3",
        "terminal_rung": "E3",
    }
    done = {
        **blocked,
        "status": "done",
        "last_accepted_rung": "E3",
    }
    validation = governance.Validation()

    accepted_rung = governance.validate_status_transition(
        validation,
        batch_id="D0",
        base_manifest=blocked,
        manifest=done,
    )

    assert validation.errors == []
    assert accepted_rung == "E3"


def test_blocked_nonterminal_batch_cannot_skip_to_done():
    blocked = {
        "status": "blocked",
        "last_accepted_rung": "E1",
        "target_rung": "E2",
        "terminal_rung": "E3",
    }
    invalid = {
        **blocked,
        "status": "done",
        "last_accepted_rung": "E2",
        "target_rung": "E3",
    }
    validation = governance.Validation()

    governance.validate_status_transition(
        validation,
        batch_id="D0",
        base_manifest=blocked,
        manifest=invalid,
    )

    assert "D0: only terminal-rung acceptance may mark the batch done" in validation.errors


@pytest.mark.parametrize("field", ["last_accepted_rung", "target_rung", "terminal_rung"])
def test_corrective_terminal_rerun_cannot_change_rungs(field):
    terminal = {
        "status": "done",
        "last_accepted_rung": "E1",
        "target_rung": "E1",
        "terminal_rung": "E1",
    }
    changed = {**terminal, field: "E2"}
    validation = governance.Validation()

    governance.validate_status_transition(
        validation,
        batch_id="D0",
        base_manifest=terminal,
        manifest=changed,
    )

    assert f"D0: corrective terminal rerun cannot change {field}" in validation.errors


def test_new_batch_registration_is_queued_and_administrative_only():
    base_graph = {"batches": {"D0": {"status": "done"}}}
    graph = {"batches": {**base_graph["batches"], "D1": {"status": "queued", "target_rung": "E0"}}}
    manifest = {
        "revision": 1,
        "status": "queued",
        "last_accepted_rung": None,
        "target_rung": "E0",
        "activation": {"base_sha": None},
        "owners": {"reviewer": None},
    }
    validation = governance.Validation()

    governance.validate_new_batch_registration(
        validation,
        batch_id="D1",
        graph=graph,
        base_graph=base_graph,
        manifest_path="governance/batches/D1.json",
        manifest=manifest,
        changed_paths=("governance/batches/D1.json", "governance/vision-issue-graph.json"),
    )

    assert validation.errors == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda manifest: manifest.update(status="active"), "must register as queued"),
        (lambda manifest: manifest.update(revision=2), "must start at revision 1"),
        (lambda manifest: manifest["activation"].update(base_sha="a" * 40), "cannot pin"),
        (lambda manifest: manifest["owners"].update(reviewer="reviewer"), "cannot pre-assign"),
    ],
)
def test_new_batch_registration_rejects_implementation_state(mutation, expected):
    base_graph = {"batches": {}}
    graph = {"batches": {"D1": {"status": "queued", "target_rung": "E0"}}}
    manifest = {
        "revision": 1,
        "status": "queued",
        "last_accepted_rung": None,
        "target_rung": "E0",
        "activation": {"base_sha": None},
        "owners": {"reviewer": None},
    }
    mutation(manifest)
    validation = governance.Validation()

    governance.validate_new_batch_registration(
        validation,
        batch_id="D1",
        graph=graph,
        base_graph=base_graph,
        manifest_path="governance/batches/D1.json",
        manifest=manifest,
        changed_paths=("governance/batches/D1.json", "governance/vision-issue-graph.json"),
    )

    assert any(expected in error for error in validation.errors)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("activation", [], "cannot pin"),
        ("owners", "unassigned", "cannot pre-assign"),
    ],
)
def test_new_batch_registration_rejects_non_object_approval_sections(field, value, expected):
    manifest = {
        "revision": 1,
        "status": "queued",
        "last_accepted_rung": None,
        "target_rung": "E0",
        "activation": {"base_sha": None},
        "owners": {"reviewer": None},
    }
    manifest[field] = value
    validation = governance.Validation()

    governance.validate_new_batch_registration(
        validation,
        batch_id="D1",
        graph={"batches": {"D1": {"status": "queued", "target_rung": "E0"}}},
        base_graph={"batches": {}},
        manifest_path="governance/batches/D1.json",
        manifest=manifest,
        changed_paths=("governance/batches/D1.json", "governance/vision-issue-graph.json"),
    )

    assert any(expected in error for error in validation.errors)


def test_new_batch_registration_rejects_implementation_files():
    validation = governance.Validation()

    governance.validate_new_batch_registration(
        validation,
        batch_id="D1",
        graph={"batches": {"D1": {"status": "queued", "target_rung": "E0"}}},
        base_graph={"batches": {}},
        manifest_path="governance/batches/D1.json",
        manifest={
            "revision": 1,
            "status": "queued",
            "last_accepted_rung": None,
            "target_rung": "E0",
            "activation": {"base_sha": None},
            "owners": {"reviewer": None},
        },
        changed_paths=(
            "apps/data-engine/src/data_engine/mvp_models.py",
            "governance/batches/D1.json",
            "governance/vision-issue-graph.json",
        ),
    )

    assert any("registration may only add" in error for error in validation.errors)


def test_prepared_batch_uses_queued_mirror_and_rejects_contradictory_labels():
    graph = {
        "root_issue": 68,
        "root_accepted_evidence": None,
        "gates": {
            "29": {
                "status": "active",
                "milestone": "D0",
                "acceptance_issues": [],
            }
        },
        "issues": {},
        "artifact_edges": [],
        "batches": {
            "D0": {
                "issue": 79,
                "owner_gate": 29,
                "status": "prepared",
                "target_rung": "E0",
                "manifest": "governance/batches/D0.json",
                "sha256": "a" * 64,
            }
        },
    }
    live = [
        {
            "number": 68,
            "state": "OPEN",
            "body": "",
            "labels": [{"name": "scope:vision"}],
            "milestone": None,
        },
        {
            "number": 29,
            "state": "OPEN",
            "body": "",
            "labels": [{"name": "scope:vision"}, {"name": "gate:active"}],
            "milestone": {"title": "D0"},
        },
        {
            "number": 79,
            "state": "OPEN",
            "body": "governance/batches/D0.json sha256:" + "a" * 64,
            "labels": [
                {"name": "scope:vision"},
                {"name": "batch:queued"},
                {"name": "batch:active"},
                {"name": "rung:code"},
                {"name": "readiness:provisional"},
            ],
            "milestone": {"title": "D0"},
        },
    ]
    validation = governance.Validation()

    governance.validate_github(validation, graph, live)

    assert "#79: batch must carry exactly one batch status" in validation.errors


def test_batch_mirror_atomically_tracks_manifest_status_and_hash():
    batch = {
        "manifest": "governance/batches/D0.json",
        "sha256": "a" * 64,
        "status": "prepared",
        "target_rung": "E0",
    }

    first = governance.render_batch_issue_body("Human-owned issue context.\n", "D0", batch)
    updated = governance.render_batch_issue_body(
        first,
        "D0",
        {**batch, "sha256": "b" * 64, "status": "active", "target_rung": "E1"},
    )

    assert "Human-owned issue context." in updated
    assert first.count(governance.BATCH_MIRROR_START) == 1
    assert updated.count(governance.BATCH_MIRROR_START) == 1
    assert "sha256:" + "b" * 64 in updated
    assert "sha256:" + "a" * 64 not in updated
    assert "Canonical status: `active`" in updated
    assert "Target rung: `E1`" in updated


def test_prepared_batch_status_mirrors_as_queued_until_merge():
    assert governance.batch_status_labels({"status": "prepared", "target_rung": "E0"}) == {
        "batch:queued",
        "rung:code",
        "readiness:provisional",
    }


def test_workflow_authorizes_every_pull_request_against_exact_head():
    workflow = (MODULE_PATH.parents[1] / ".github" / "workflows" / "ci-governance.yml").read_text(encoding="utf-8")

    assert "pull_request:\n" in workflow
    assert "--pr-base-sha" in workflow
    assert "--pr-head-sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "uses: astral-sh/setup-uv@v5" in workflow
    assert "uv sync --all-packages --frozen" in workflow
    assert "--execute-acceptance" in workflow
    assert "render_batch_issue_body" in workflow
    assert 'json.dumps({"body": desired_body, "labels": desired})' in workflow
