"""Environment manifests are true (#759): fresh, past the offline gate, and release values derive from the repo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import EnvironmentManifest
from truealpha_runtime.testing import load_tool

ROOT = Path(__file__).resolve().parents[3]
env_manifest = load_tool("env_manifest")
release_identity = load_tool("release_identity")

# What Vault held on 2026-09-07 for both environments, typed by hand until #759. The value
# is a content hash of a constant payload, so the repository must reproduce it exactly.
DEPLOYED_RELEASE_MANIFEST_ID = "release-manifest:c2df9469104c19bee89d8104e3df0041d2740e1e34b1b93aa3e66baf150cf57f"


def test_generated_manifests_are_fresh_and_all_pass_the_offline_gate() -> None:
    assert env_manifest.check() == []


def test_every_secret_declares_a_producer() -> None:
    for path, manifest in env_manifest.all_manifests().items():
        for field in manifest.fields:
            if field.sensitive:
                assert field.source in {"human", "runtime"}, f"{path}: {field.env} is sensitive but {field.source}"


def test_data_engine_manifest_carries_the_datahub_vendor_keys() -> None:
    manifest = env_manifest.load("apps/data-engine/required-env.generated.json")
    by_env = {field.env: field for field in manifest.fields}
    for name in ("TWELVE_DATA_API_KEY", "OPENFIGI_API_KEY"):
        assert (by_env[name].source, by_env[name].scope, by_env[name].empty_ok) == ("human", "project", True)
    assert by_env["SEC_USER_AGENT"].source == "human" and by_env["SEC_USER_AGENT"].injected
    assert by_env["GIT_COMMIT_SHA"].source == "release" and by_env["GIT_COMMIT_SHA"].injected
    assert by_env["DATABASE_URL"].provided_by == "truealpha/postgres:POSTGRES_PASSWORD"
    assert by_env["DATABASE_URL"].composed_env == ("TA_POSTGRES_PORT",)
    assert by_env["MOOMOO_MONTHLY_CALL_BUDGET"].source == "code"
    assert manifest.contract_version == 2


def test_app_web_manifest_is_a_valid_contract() -> None:
    raw = json.loads((ROOT / "apps/app-web/required-env.manifest.json").read_text(encoding="utf-8"))
    manifest = EnvironmentManifest.from_dict(raw)
    assert validate_manifest_offline(manifest) == []
    assert {field.env for field in manifest.fields} >= {"DATABASE_URL", "SECRET_KEY", "ACCESS_TOKEN_EXPIRE_MINUTES"}


def test_release_values_derive_from_the_repository() -> None:
    assert release_identity.release_values("production") == {
        "RELEASE_MANIFEST_ID": DEPLOYED_RELEASE_MANIFEST_ID,
        "CAPTURE_APPROVED_BY": "zitian",
    }
    assert release_identity.release_values("staging")["CAPTURE_APPROVED_BY"] == "wangzitian0"
    with pytest.raises(ValueError, match="unknown environment"):
        release_identity.approval("preview")


def test_validate_env_reports_missing_injected_values(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    assert env_manifest.main(["--validate-env", "apps/data-engine/required-env.generated.json"]) == 1
    out = capsys.readouterr().out
    assert "missing: SEC_USER_AGENT" in out and "missing: GIT_COMMIT_SHA" in out
