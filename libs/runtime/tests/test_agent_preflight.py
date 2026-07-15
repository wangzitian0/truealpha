from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[3] / "tools" / "agent_preflight.py"
SPEC = importlib.util.spec_from_file_location("truealpha_agent_preflight", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def test_workspace_identity_uses_checkout_directory():
    assert preflight.workspace_identity(Path("/tmp/truealpha-factors")) == (
        "truealpha-factors",
        "[truealpha-factors]",
    )


def test_workspace_identity_accepts_any_valid_checkout_directory():
    assert preflight.workspace_identity(Path("/tmp/truealpha")) == ("truealpha", "[truealpha]")


def test_workspace_identity_rejects_invalid_prefix_syntax():
    with pytest.raises(preflight.PreflightError, match="cannot form a valid workspace prefix"):
        preflight.workspace_identity(Path("/tmp/TrueAlpha Factors"))


def test_matching_work_claim_prs_uses_exact_structured_fields():
    pull_requests = [
        {"number": 1, "body": "Work-Issue: #228\nWork-Key: standalone-228", "headRefName": "agent/228"},
        {"number": 2, "body": "Notes mention #228 only", "headRefName": "agent/other"},
        {"number": 3, "body": "Work-Issue: #1228", "headRefName": "agent/1228"},
        {"number": 4, "body": "Work-Issue: #229\nWork-Key: standalone-228", "headRefName": "agent/duplicate"},
    ]

    assert preflight.matching_work_claim_prs(pull_requests, 228, "standalone-228") == [
        pull_requests[0],
        pull_requests[3],
    ]


def test_expected_work_key_comes_from_batch_manifest(tmp_path):
    batch_dir = tmp_path / "governance" / "batches"
    batch_dir.mkdir(parents=True)
    (batch_dir / "D4.json").write_text(
        '{"batch_id":"D4-datahub-interface","issue":210,"target_rung":"E1"}',
        encoding="utf-8",
    )

    assert preflight.expected_work_key(tmp_path, 210) == "D4-datahub-interface:E1"
    assert preflight.expected_work_key(tmp_path, 228) == "standalone-228"


def test_expected_work_key_fails_closed_on_malformed_manifest(tmp_path):
    batch_dir = tmp_path / "governance" / "batches"
    batch_dir.mkdir(parents=True)
    (batch_dir / "broken.json").write_text("{", encoding="utf-8")

    with pytest.raises(preflight.PreflightError, match="broken.json"):
        preflight.expected_work_key(tmp_path, 228)


def test_gone_upstream_detection_is_exact():
    assert preflight.upstream_is_gone("[gone]\n")
    assert not preflight.upstream_is_gone("behind 1")


def test_gone_upstream_repair_returns_the_new_branch(tmp_path, monkeypatch):
    commands = []

    def fake_run(*args, cwd, check=True):
        commands.append(args)
        stdout = "[gone]\n" if args[1] == "for-each-ref" else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(preflight, "run", fake_run)

    assert preflight.clean_gone_upstream(tmp_path, "agent/old", True) == "main"
    assert ("git", "switch", "main") in commands
    assert ("git", "merge", "--ff-only", "origin/main") in commands
