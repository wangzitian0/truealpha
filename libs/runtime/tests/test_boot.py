"""truealpha_runtime.boot.assert_environment (#759): the manifest gates the entrypoint, by name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from truealpha_runtime.boot import assert_environment

ROOT = Path(__file__).resolve().parents[3]
LLM_MANIFEST = ROOT / "apps/llm-service/required-env.generated.json"


def test_a_deployed_process_refuses_to_boot_without_what_the_deployment_injects() -> None:
    environ = {"DATABASE_URL": "postgresql://postgres:hunter2@db:5432/truealpha"}
    with pytest.raises(RuntimeError) as failure:
        assert_environment(LLM_MANIFEST, environ=environ, require_injected=True)
    message = str(failure.value)
    for name in ("APP_ENV", "GIT_COMMIT_SHA", "S3_ENDPOINT"):
        assert name in message
    assert "hunter2" not in message and "postgresql://" not in message, "names only, never values"


def test_a_local_process_tolerates_absent_injected_values() -> None:
    assert assert_environment(LLM_MANIFEST, environ={}, require_injected=False) is None


def test_an_empty_injected_value_counts_as_missing() -> None:
    environ = {"APP_ENV": "staging", "GIT_COMMIT_SHA": "v0.0.50", "S3_ENDPOINT": ""}
    with pytest.raises(RuntimeError, match="S3_ENDPOINT"):
        assert_environment(LLM_MANIFEST, environ=environ, require_injected=True)
    environ["S3_ENDPOINT"] = "http://platform-minio:9000"
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
