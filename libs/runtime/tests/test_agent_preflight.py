from __future__ import annotations

import importlib.util
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


def test_workspace_identity_rejects_remote_derived_name():
    with pytest.raises(preflight.PreflightError, match="unsupported checkout"):
        preflight.workspace_identity(Path("/tmp/truealpha"))


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


def test_gone_upstream_detection_is_exact():
    assert preflight.upstream_is_gone("[gone]\n")
    assert not preflight.upstream_is_gone("behind 1")
