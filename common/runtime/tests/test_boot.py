"""truealpha_runtime.boot.assert_environment (#759): the manifest gates the entrypoint, by name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from truealpha_runtime.boot import assert_environment

ROOT = Path(__file__).resolve().parents[3]
LLM_MANIFEST = ROOT / "apps/llm-service/required-env.generated.json"
DATA_ENGINE_MANIFEST = ROOT / "apps/data-engine/required-env.generated.json"


def test_a_deployed_process_refuses_to_boot_without_what_the_deployment_injects() -> None:
    environ = {"DATABASE_URL": "postgresql://postgres:hunter2@db:5432/truealpha"}
    with pytest.raises(RuntimeError) as failure:
        assert_environment(LLM_MANIFEST, environ=environ, require_injected=True)
    message = str(failure.value)
    for name in ("APP_ENV", "GIT_COMMIT_SHA", "MCP_BEARER_TOKEN"):
        assert name in message
    assert "S3_ENDPOINT" not in message
    assert "hunter2" not in message and "postgresql://" not in message, "names only, never values"

    with pytest.raises(RuntimeError) as de_failure:
        assert_environment(DATA_ENGINE_MANIFEST, environ=environ, require_injected=True)
    assert "S3_ENDPOINT" in str(de_failure.value)


def test_a_local_process_tolerates_absent_injected_values() -> None:
    assert assert_environment(LLM_MANIFEST, environ={}, require_injected=False) is None


def test_an_empty_injected_value_counts_as_missing() -> None:
    environ = {"APP_ENV": "staging", "GIT_COMMIT_SHA": "", "MCP_BEARER_TOKEN": "test-mcp-token"}
    with pytest.raises(RuntimeError, match="GIT_COMMIT_SHA"):
        assert_environment(LLM_MANIFEST, environ=environ, require_injected=True)
    environ["GIT_COMMIT_SHA"] = "v0.0.50"
    assert_environment(LLM_MANIFEST, environ=environ, require_injected=True)
    # #42: an empty MCP credential is as missing as an absent one.
    environ["MCP_BEARER_TOKEN"] = ""
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        assert_environment(LLM_MANIFEST, environ=environ, require_injected=True)


def test_a_required_field_is_missing_in_every_tier(tmp_path: Path) -> None:
    manifest = {
        "contract_version": 2,
        "source": "x",
        "fields": [{"field": "token", "env": "TOKEN", "source": "runtime", "required": True, "sensitive": True}],
    }
    path = tmp_path / "required-env.generated.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="TOKEN"):
        assert_environment(path, environ={}, require_injected=False)
    assert_environment(path, environ={"TOKEN": "t"}, require_injected=False)


def test_a_manifest_the_image_does_not_ship_is_its_own_red(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not in the image"):
        assert_environment(tmp_path / "required-env.generated.json", environ={}, require_injected=True)
