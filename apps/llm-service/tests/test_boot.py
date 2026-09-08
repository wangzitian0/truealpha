"""llm-service refuses to boot on an environment its manifest says is incomplete (#759)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from llm_service import main
from llm_service.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_image_ships_the_manifest_where_the_service_reads_it() -> None:
    """The Dockerfile's runtime stage copies only what it names; the manifest must be one
    of those things, at the path `main` reads relative to the image's working directory."""
    dockerfile = (REPO_ROOT / "apps/llm-service/Dockerfile").read_text(encoding="utf-8").splitlines()
    assert f"COPY {main.ENV_MANIFEST_PATH} ./{main.ENV_MANIFEST_PATH}" in dockerfile
    assert (REPO_ROOT / main.ENV_MANIFEST_PATH).is_file()


def test_a_deployed_boot_refuses_a_missing_injected_value_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the app's own lifespan, not the helper: the wiring is the deliverable. The
    refusal happens before the MCP session manager starts, so nothing is left half-open."""
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.setattr(main, "settings", Settings(_env_file=None, app_env="staging"))
    with pytest.raises(RuntimeError, match="S3_ENDPOINT") as failure:
        with TestClient(main.app):
            pass
    assert "postgresql://" not in str(failure.value), "names only, never values"


def test_a_local_boot_tolerates_what_nothing_injects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    monkeypatch.setattr(main, "settings", Settings(_env_file=None, app_env="dev"))
    main.refuse_to_boot_on_a_missing_environment()


def test_an_empty_environment_value_does_not_shadow_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inherited `env_ignore_empty`: a blank DATABASE_URL is the default, not "" (#759)."""
    monkeypatch.setenv("DATABASE_URL", "")
    assert Settings(_env_file=None).database_url == Settings.model_fields["database_url"].default
