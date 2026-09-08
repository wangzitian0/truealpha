"""Environment manifests are true (#759): fresh, past the offline gate, and release values derive from the repo."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import EnvironmentManifest
from truealpha_runtime.testing import load_tool

ROOT = Path(__file__).resolve().parents[3]
env_manifest = load_tool("env_manifest")
release_identity = load_tool("release_identity")

# What Vault holds for BOTH environments, typed by hand and unchanged since the lane went
# live: the content hash of a constant payload. Since #784 the repository measures the
# release instead, so this value is what the identity must no longer be — infra2 still reads
# Vault, and the hand-off is a follow-up there.
CONSTANT_ERA_RELEASE_MANIFEST_ID = "release-manifest:c2df9469104c19bee89d8104e3df0041d2740e1e34b1b93aa3e66baf150cf57f"
_RELEASE_ID = re.compile(r"^release-manifest:[0-9a-f]{64}$")


def test_generated_manifests_are_fresh_and_all_pass_the_offline_gate() -> None:
    assert env_manifest.check() == []


def test_every_secret_declares_a_producer() -> None:
    for path, manifest in env_manifest.all_manifests().items():
        for field in manifest.fields:
            if field.sensitive:
                assert field.source in {"human", "runtime"}, f"{path}: {field.env} is sensitive but {field.source}"


def test_data_engine_manifest_declares_the_identity_the_deployment_injects() -> None:
    """#784: the three release-identity values reach the process from infra2's compose under
    these exact names. Declared here so the deployment can be reconciled against the manifest
    and so `truealpha_runtime.boot.assert_environment` can require them — before #784 they
    were read straight out of `os.environ` and appeared in no contract at all."""
    manifest = env_manifest.load("apps/data-engine/required-env.generated.json")
    by_env = {field.env: field for field in manifest.fields}
    for name, source in (
        ("TRUEALPHA_RELEASE_MANIFEST_ID", "release"),
        ("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", "release"),
        ("TRUEALPHA_CONFIGURATION_SHA256", "release"),
        ("TRUEALPHA_CAPTURE_APPROVED_BY", "decision"),
    ):
        field = by_env[name]
        # release and decision values come from the deployment, never from a secret store.
        assert (field.source, field.injected, field.store_backed) == (source, True, False), name


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


def test_release_values_are_measured_for_a_named_release() -> None:
    """#784: the deploy-time values are OF a release. The id the tool prints for one tag must
    differ from the id it prints for another — the constant it replaced was the same string
    for every tag, both environments and every run this lane ever produced."""
    production = release_identity.release_values("production", version_ref="v0.0.49")
    assert production["CAPTURE_APPROVED_BY"] == "zitian"
    assert _RELEASE_ID.fullmatch(production["RELEASE_MANIFEST_ID"])
    assert production["RELEASE_MANIFEST_ID"] != CONSTANT_ERA_RELEASE_MANIFEST_ID
    assert (
        release_identity.release_values("production", version_ref="v0.0.50")["RELEASE_MANIFEST_ID"]
        != production["RELEASE_MANIFEST_ID"]
    )
    # The approval is per environment; the release identity is not.
    staging = release_identity.release_values("staging", version_ref="v0.0.49")
    assert staging["CAPTURE_APPROVED_BY"] == "wangzitian0"
    assert staging["RELEASE_MANIFEST_ID"] == production["RELEASE_MANIFEST_ID"]
    with pytest.raises(ValueError, match="unknown environment"):
        release_identity.approval("preview")


def test_the_release_identity_tool_refuses_to_speak_for_an_unnamed_build(capsys) -> None:
    """A value computed from no release is not one a deployment should write down."""
    with pytest.raises(SystemExit):
        release_identity.main(["--env", "production"])
    assert "--version-ref" in capsys.readouterr().err


def test_validate_env_reports_missing_injected_values(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    assert env_manifest.main(["--validate-env", "apps/data-engine/required-env.generated.json"]) == 1
    out = capsys.readouterr().out
    assert "missing: SEC_USER_AGENT" in out and "missing: GIT_COMMIT_SHA" in out
