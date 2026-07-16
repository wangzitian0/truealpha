from __future__ import annotations

import copy
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = REPO_ROOT / "libs/runtime/tests/fixtures/infra_boundary.v1.json"
MODULE_PATH = REPO_ROOT / "tools/app_deploy_request.py"
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/deploy-release.yml"
SPEC = importlib.util.spec_from_file_location("truealpha_app_deploy_request", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
renderer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = renderer
SPEC.loader.exec_module(renderer)


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _merged(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merged(result[key], value)
        else:
            result[key] = value
    return result


def test_sdk_release_identity_is_exactly_pinned(corpus: dict) -> None:
    binding = corpus["sdk_binding"]
    assert importlib.metadata.version(binding["distribution"]) == binding["version"]

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependency = f"{binding['distribution']} @ {binding['wheel_url']}"
    assert dependency in pyproject["dependency-groups"]["dev"]

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == binding["distribution"])
    assert package["version"] == binding["version"]
    assert package["source"] == {"url": binding["wheel_url"]}
    assert package["wheels"] == [
        {
            "url": binding["wheel_url"],
            "hash": f"sha256:{binding['wheel_sha256']}",
        }
    ]


def test_valid_staging_request_round_trips_and_is_canonical(corpus: dict) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    request = renderer.request_from_mapping(valid["request"])
    assert request.to_dict() == valid["request"]
    assert renderer.canonical_json(request) == (
        json.dumps(valid["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    )


def test_preview_tag_request_is_supported(corpus: dict) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]
    raw = _merged(valid, {"deploy_type": "preview/tag"})

    request = renderer.request_from_mapping(raw)

    assert request.deploy_type.value == "preview/tag"


def test_production_request_requires_exact_evidence(corpus: dict) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]
    raw = _merged(
        valid,
        {
            "deploy_type": "prod",
            "evidence": {
                "staging_run_url": "https://github.com/wangzitian0/infra2/actions/runs/23456789",
                "reviewed_change_url": "https://github.com/wangzitian0/truealpha/pull/330",
            },
        },
    )

    request = renderer.request_from_mapping(raw)

    assert request.deploy_type.value == "prod"
    assert request.evidence.staging_run_url.endswith("/23456789")
    assert request.evidence.reviewed_change_url.endswith("/330")


@pytest.mark.parametrize("deploy_type", ["preview/branch", "preview/pr", "preview/commit", "canary"])
def test_sender_only_supports_release_promotion_types(corpus: dict, deploy_type: str) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]
    with pytest.raises(ValueError, match="deploy_type must be preview/tag, staging, or prod"):
        renderer.request_from_mapping(_merged(valid, {"deploy_type": deploy_type}))


@pytest.mark.parametrize("version_ref", ["main", "870bd4c", "v0.0.1-rc.1"])
def test_release_request_requires_stable_semver_tag(corpus: dict, version_ref: str) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]
    with pytest.raises(ValueError, match="version_ref must be a stable vX.Y.Z tag"):
        renderer.request_from_mapping(_merged(valid, {"version_ref": version_ref}))


@pytest.mark.parametrize(
    ("evidence_override", "message"),
    [
        ({"staging_run_url": ""}, "production evidence.staging_run_url is required"),
        ({"reviewed_change_url": ""}, "production evidence.reviewed_change_url is required"),
        (
            {"staging_run_url": "https://github.com/wangzitian0/truealpha/actions/runs/2"},
            "staging_run_url must point to the infra2 receiver run",
        ),
        (
            {"reviewed_change_url": "https://github.com/wangzitian0/truealpha/issues/330"},
            "reviewed_change_url must point to a TrueAlpha pull request",
        ),
    ],
)
def test_production_evidence_fails_closed(corpus: dict, evidence_override: dict, message: str) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]
    raw = _merged(
        valid,
        {
            "deploy_type": "prod",
            "evidence": {
                "staging_run_url": "https://github.com/wangzitian0/infra2/actions/runs/23456789",
                "reviewed_change_url": "https://github.com/wangzitian0/truealpha/pull/330",
                **evidence_override,
            },
        },
    )
    with pytest.raises(ValueError, match=message):
        renderer.request_from_mapping(raw)


@pytest.mark.parametrize("contract_version", [2, True])
def test_contract_version_must_be_v1(corpus: dict, contract_version: object) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    raw = _merged(valid["request"], {"contract_version": contract_version})
    with pytest.raises(ValueError, match="contract_version must be 1"):
        renderer.request_from_mapping(raw)


@pytest.mark.parametrize(
    "case_id",
    [
        "invalid-service",
        "invalid-source-repository",
        "invalid-source-sha",
        "invalid-source-evidence-host",
        "invalid-source-evidence-repository",
    ],
)
def test_frozen_negative_cases_fail_closed(corpus: dict, case_id: str) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    case = next(case for case in corpus["cases"] if case["id"] == case_id)
    with pytest.raises(ValueError, match=re.escape(case["error"])):
        renderer.request_from_mapping(_merged(valid["request"], case["override"]))


@pytest.mark.parametrize("version_ref", [None, True, 1])
def test_version_ref_requires_a_string(corpus: dict, version_ref: object) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    with pytest.raises(ValueError, match="version_ref must be a string"):
        renderer.request_from_mapping(_merged(valid["request"], {"version_ref": version_ref}))


@pytest.mark.parametrize("source_sha", [None, True, "A" * 40, "a" * 39, "a" * 41])
def test_source_sha_requires_lowercase_commit_identity(corpus: dict, source_sha: object) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    with pytest.raises(ValueError, match="source_sha must be a lowercase 40-hex commit sha"):
        renderer.request_from_mapping(_merged(valid["request"], {"source_sha": source_sha}))


def test_run_id_must_match_truealpha_actions_url(corpus: dict) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    raw = _merged(valid["request"], {"evidence": {"source_run_id": "87654321"}})
    with pytest.raises(ValueError, match="source_run_id must match"):
        renderer.request_from_mapping(raw)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"evidence": None}, "evidence must be an object"),
        ({"evidence": {"source_run_url": ""}}, "source_run_url is required"),
        ({"evidence": {"source_run_id": ""}}, "source_run_id is required"),
    ],
)
def test_required_evidence_fields_fail_closed(corpus: dict, override: dict, message: str) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")
    with pytest.raises(ValueError, match=message):
        renderer.request_from_mapping(_merged(valid["request"], override))


def test_cli_exposes_no_authority_or_dispatch_switches(corpus: dict) -> None:
    valid = next(case for case in corpus["cases"] if case["expected"] == "accepted")["request"]
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--request-id",
            valid["request_id"],
            "--version-ref",
            valid["version_ref"],
            "--source-sha",
            valid["source_sha"],
            "--source-run-url",
            valid["evidence"]["source_run_url"],
            "--source-run-id",
            valid["evidence"]["source_run_id"],
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == valid
    assert result.stderr == ""
    assert "repository_dispatch" not in MODULE_PATH.read_text(encoding="utf-8")


def test_release_workflow_dispatches_only_the_rendered_sdk_request() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "options: [preview/tag, staging, prod]" in workflow
    assert "python tools/app_deploy_request.py" in workflow
    assert "event_type: app-deploy-request" in workflow
    assert '"client_payload": $request' in workflow
    assert "actions/workflows/app-deploy-request.yml/runs" in workflow
    assert 'receiver_conclusion" != "success"' in workflow
    assert "secrets.INFRA2_PAT" in workflow
    assert "DOKPLOY_API_KEY" not in workflow
    assert "IAC_WEBHOOK_SECRET" not in workflow


def test_infra2_source_boundary_is_absent(corpus: dict) -> None:
    boundary = corpus["repository_boundary"]
    for relative_path in boundary["required_absent_paths"]:
        assert not (REPO_ROOT / relative_path).exists()

    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "repo" not in dockerignore
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "extend-exclude" not in pyproject["tool"]["ruff"]
    assert "from `repo/`" not in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "bump the `repo` submodule pointer" not in (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
