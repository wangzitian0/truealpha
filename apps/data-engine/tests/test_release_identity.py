"""The data engine's release identity is measured from the artifact it runs as (#784, #712).

Every assertion here is red against the code this replaced: `live_release_manifest_id()`
hashed `{"kind": "production-topt-live-release"}`, so the id was the same 64-hex value for
every tag and both environments, the image shipped neither of the two files the measurement
reads, and the deployed readers took their identity values out of `os.environ` where no
manifest declared them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from data_engine import release_identity
from data_engine.config import Settings
from infra2_sdk.runtime.config_schema import configuration_fingerprint
from infra2_sdk.runtime.identity import canonical_sha256 as estate_canonical_sha256
from truealpha_contracts.common import canonical_sha256

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "apps/data-engine/Dockerfile"

#: What Vault held for both environments on 2026-09-07, and what every run this lane ever
#: produced was stamped with: the hash of a constant payload. Pinned so the day the
#: measurement silently collapses back to a constant, this file says so.
CONSTANT_ERA_ID = "release-manifest:c2df9469104c19bee89d8104e3df0041d2740e1e34b1b93aa3e66baf150cf57f"


def _measure(**overrides):
    arguments = {
        "git_commit_sha": "v0.0.49",
        "migrations_path": REPO_ROOT / "db/migrations",
        "env_manifest_path": REPO_ROOT / "apps/data-engine/required-env.generated.json",
    }
    arguments.update(overrides)
    return release_identity.measure(**arguments)


# --- the image contains what the measurement and the entrypoint read ---------------------


def test_the_image_ships_the_manifest_where_boot_validation_and_the_identity_read_it() -> None:
    """The Dockerfile's runtime stage copies only what it names, and both readers resolve
    these paths against the image's working directory (`llm_service.main` does the same for
    its own manifest, `apps/llm-service/tests/test_boot.py`).

    Red before #784: the data-engine image shipped no `required-env.generated.json` at all,
    so infra2's entrypoint could not run `truealpha_runtime.boot.assert_environment` for it
    and the absence was invisible from outside the container.
    """
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    assert f"COPY {release_identity.ENV_MANIFEST_PATH} ./{release_identity.ENV_MANIFEST_PATH}" in lines
    assert f"COPY {release_identity.MIGRATIONS_PATH} ./{release_identity.MIGRATIONS_PATH}" in lines
    # `./x` only means the repository path when the process runs from the image root.
    assert "WORKDIR /app" in lines
    assert (REPO_ROOT / release_identity.ENV_MANIFEST_PATH).is_file()
    assert (REPO_ROOT / release_identity.MIGRATIONS_PATH).is_dir()


def test_a_missing_input_is_a_named_failure_never_a_default(tmp_path: Path) -> None:
    """An identity computed from whatever happened to be present would be a DIFFERENT
    identity, silently. Both failures name the file, the way `truealpha_runtime.boot` does."""
    with pytest.raises(RuntimeError, match="environment manifest"):
        _measure(env_manifest_path=tmp_path / "required-env.generated.json")
    with pytest.raises(RuntimeError, match="migration set"):
        _measure(migrations_path=tmp_path / "migrations")
    (tmp_path / "empty").mkdir()
    with pytest.raises(RuntimeError, match="no migration"):
        _measure(migrations_path=tmp_path / "empty")


# --- what it measures --------------------------------------------------------------------


def test_the_identity_measures_this_repositorys_own_migration_set() -> None:
    on_disk = sorted(path.name for path in (REPO_ROOT / "db/migrations").glob("*.sql"))
    payload = _measure().payload
    assert payload["migration_ids"] == [name.removesuffix(".sql") for name in on_disk]
    assert payload["git_commit_sha"] == "v0.0.49"
    assert payload["environment_contract_sha256"] == release_identity.environment_contract_sha256(
        REPO_ROOT / "apps/data-engine/required-env.generated.json"
    )


def test_two_tags_give_two_identities() -> None:
    assert _measure(git_commit_sha="v0.0.49").manifest_id != _measure(git_commit_sha="v0.0.50").manifest_id
    assert _measure(git_commit_sha="v0.0.49").manifest_id == _measure(git_commit_sha="v0.0.49").manifest_id


def test_a_release_that_ships_another_schema_has_another_identity(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(REPO_ROOT / "db/migrations", migrations)
    before = _measure(migrations_path=migrations).manifest_id
    assert before == _measure().manifest_id, "a copy of the same files is the same release"
    (migrations / "20260909T0000_datahub_next.sql").write_text("-- next\n", encoding="utf-8")
    assert _measure(migrations_path=migrations).manifest_id != before


def test_a_release_that_changes_the_environment_contract_has_another_identity(tmp_path: Path) -> None:
    source = REPO_ROOT / "apps/data-engine/required-env.generated.json"
    manifest = json.loads(source.read_text(encoding="utf-8"))
    target = tmp_path / "required-env.generated.json"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    assert _measure(env_manifest_path=target).manifest_id == _measure().manifest_id, "same contract, same release"

    manifest["fields"].append(
        {"field": "new_vendor_key", "env": "NEW_VENDOR_KEY", "source": "human", "sensitive": True, "empty_ok": True}
    )
    target.write_text(json.dumps(manifest), encoding="utf-8")
    assert _measure(env_manifest_path=target).manifest_id != _measure().manifest_id


def test_the_contract_fingerprint_hashes_the_declarations_not_the_deployed_values() -> None:
    """The deploy runner computes this id from a checkout with none of the deployed values
    in hand and must reach the same answer as the container; and a value that changed
    between staging and production would give one release two identities."""
    manifest = release_identity.environment_contract(REPO_ROOT / "apps/data-engine/required-env.generated.json")
    expected = configuration_fingerprint(
        manifest,
        {
            field.env: json.dumps(field.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            for field in manifest.fields
        },
    )
    assert (
        release_identity.environment_contract_sha256(REPO_ROOT / "apps/data-engine/required-env.generated.json")
        == expected
    )


# --- shape, hash agreement, and the constant it replaced ---------------------------------


def test_the_identity_keeps_the_shape_infra2_asserts_before_it_deploys() -> None:
    identity = _measure()
    assert identity.manifest_id == f"release-manifest:{identity.content_sha256}"
    # truealpha/truealpha/20.data_engine/deploy.py: `^release-manifest:[0-9a-f]{64}$`.
    assert len(identity.content_sha256) == 64 and identity.content_sha256.islower()
    assert int(identity.content_sha256, 16) >= 0


def test_this_repositorys_canonical_hash_is_the_estates() -> None:
    """`truealpha_contracts.common.canonical_sha256` and
    `infra2_sdk.runtime.identity.canonical_sha256` must agree on the release payload: infra2
    hashes release identities with the SDK's, this repository mints them with its own, and a
    divergence would mean two systems disagreeing about what one release is called."""
    payload = _measure().payload
    assert canonical_sha256(payload) == estate_canonical_sha256(payload)


def test_the_identity_is_no_longer_the_hash_of_a_constant() -> None:
    """The value Vault holds for both environments today, and that every run carried."""
    assert _measure().manifest_id != CONSTANT_ERA_ID
    assert _measure(git_commit_sha="unknown").manifest_id != CONSTANT_ERA_ID


# --- the deployed reader takes the release ref from settings -----------------------------


def test_the_release_ref_comes_from_settings_not_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red before #784, when this value was `os.environ.get("GIT_COMMIT_SHA")` at the call
    site: patching the settings object the manifest declares changed nothing."""
    monkeypatch.setenv("GIT_COMMIT_SHA", "v0.0.00-from-the-environment")
    monkeypatch.setattr(release_identity.settings, "git_commit_sha", "v0.0.49")
    assert (
        release_identity.measure(
            migrations_path=REPO_ROOT / "db/migrations",
            env_manifest_path=REPO_ROOT / "apps/data-engine/required-env.generated.json",
        ).payload["git_commit_sha"]
        == "v0.0.49"
    )


def test_the_settings_model_resolves_the_names_the_compose_actually_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`truealpha/truealpha/20.data_engine/compose.yaml` sets the TRUEALPHA_-prefixed names;
    the manifest and the model must speak those, not tidier ones nothing injects."""
    digest = "sha256:" + "d" * 64
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", digest)
    monkeypatch.setenv("TRUEALPHA_RELEASE_MANIFEST_ID", CONSTANT_ERA_ID)
    monkeypatch.setenv("TRUEALPHA_CAPTURE_APPROVED_BY", "zitian")
    monkeypatch.setenv("TRUEALPHA_CONFIGURATION_SHA256", "a" * 64)
    settings = Settings(_env_file=None)
    assert settings.data_engine_image_digest == digest
    assert settings.release_manifest_id == CONSTANT_ERA_ID
    assert settings.capture_approved_by == "zitian"
    assert settings.configuration_sha256 == "a" * 64
