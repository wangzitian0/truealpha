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
    """#784, #1321: the three release-identity values reach the process under canonical names.
    Declared here so the deployment can be reconciled against the manifest and so
    `truealpha_runtime.boot.assert_environment` can require them."""
    manifest = env_manifest.load("apps/data-engine/required-env.generated.json")
    by_env = {field.env: field for field in manifest.fields}
    for name, source in (
        ("RELEASE_ID", "release"),
        ("IMAGE_DIGEST", "release"),
        ("CONFIGURATION_SHA256", "release"),
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
    for name in ("SEC_USER_AGENT", "LLM_MODEL"):
        field = by_env[name]
        assert (field.source, field.injected, field.empty_ok, field.scope) == ("code", False, False, "env"), name
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


def test_the_release_identity_tool_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Copilot review on #786 (Medium): the tool anchored `governance/approvals/<env>.yaml` on
    ROOT but let the measurement fall through to CWD-relative defaults. A deploy runner that
    shells it out without `cd`ing first would then either fail with a misleading "not in the
    image" or measure some other tree and print a well-formed id for the wrong release."""
    from_repo_root = release_identity.release_values("production", version_ref="v0.0.49")
    monkeypatch.chdir(tmp_path)
    assert release_identity.release_values("production", version_ref="v0.0.49") == from_repo_root


def test_the_release_identity_tool_refuses_to_speak_for_an_unnamed_build(capsys) -> None:
    """A value computed from no release is not one a deployment should write down."""
    with pytest.raises(SystemExit):
        release_identity.main(["--env", "production"])
    assert "--version-ref" in capsys.readouterr().err


def test_validate_env_reports_missing_injected_values(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("RELEASE_ID", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    assert env_manifest.main(["--validate-env", "apps/data-engine/required-env.generated.json"]) == 1
    out = capsys.readouterr().out
    assert "missing: RELEASE_ID" in out and "missing: GIT_COMMIT_SHA" in out
    assert "missing: SEC_USER_AGENT" not in out and "missing: LLM_MODEL" not in out


def test_llm_service_manifest_excludes_s3_and_unused_llm_variables_and_declares_migrations() -> None:
    """#1367: llm-service inherits CoreRuntimeSettings (no S3_* or unused LLM_* fields) and
    owns the four migration variables consumed by its container entrypoint."""
    llm_manifest = env_manifest.load("apps/llm-service/required-env.generated.json")
    llm_envs = {field.env for field in llm_manifest.fields}
    assert not any(env.startswith(("S3_", "LLM_")) for env in llm_envs), llm_envs
    migration_vars = {
        "MIGRATIONS_DATABASE_URL",
        "MIGRATION_LOCK_BUDGET_SECONDS",
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        "MIGRATION_STATEMENT_TIMEOUT_SECONDS",
    }
    assert migration_vars <= llm_envs

    de_manifest = env_manifest.load("apps/data-engine/required-env.generated.json")
    de_envs = {field.env for field in de_manifest.fields}
    assert not (migration_vars & de_envs), de_envs
    assert "S3_DOCUMENTS_PREFIX" not in de_envs


def test_app_web_manifest_declares_every_process_env_variable_read_by_server_code() -> None:
    """#1367, #1375 (G2): app-web server code and required-env.manifest.json match bidirectionally,
    and apps/app-web/src/server defines a single shared pg.Pool and DATABASE_URL reader."""
    raw = json.loads((ROOT / "apps/app-web/required-env.manifest.json").read_text(encoding="utf-8"))
    manifest = EnvironmentManifest.from_dict(raw)
    declared = {field.env for field in manifest.fields}

    pattern = re.compile(
        r"""process\.env\.([A-Z0-9_]+)|(?:env|envPositiveSeconds|requireEnv)\(\s*["']([A-Z0-9_]+)["']"""
    )
    read_in_src: set[str] = set()
    for path in (ROOT / "apps/app-web/src").rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            name = match.group(1) or match.group(2)
            if name:
                read_in_src.add(name)

    assert read_in_src == declared, (
        f"app-web env drift: undeclared={sorted(read_in_src - declared)}, "
        f"unused_in_src={sorted(declared - read_in_src)}"
    )

    pool_creations = 0
    db_url_reads = 0
    for path in (ROOT / "apps/app-web/src/server").rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        pool_creations += len(re.findall(r"\bnew\s+Pool\s*\(", text))
        db_url_reads += len(re.findall(r"process\.env\.DATABASE_URL\b", text))
    assert pool_creations == 1, f"expected 1 pg.Pool construction in app-web/src/server, found {pool_creations}"
    assert db_url_reads == 1, f"expected 1 DATABASE_URL read in app-web/src/server, found {db_url_reads}"


def test_app_web_s3_contract_matches_python_runtime_settings() -> None:
    """#1375 (G3): S3 metadata in app-web's manifest and fallback defaults in object-store.ts
    match Python RuntimeSettings."""
    from truealpha_runtime import RuntimeSettings

    web_raw = json.loads((ROOT / "apps/app-web/required-env.manifest.json").read_text(encoding="utf-8"))
    web_by_env = {field.env: field for field in EnvironmentManifest.from_dict(web_raw).fields}
    de_by_env = {field.env: field for field in env_manifest.load("apps/data-engine/required-env.generated.json").fields}

    shared_s3_vars = (
        "S3_ENDPOINT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "S3_REGION",
        "S3_RAW_PREFIX",
        "S3_CONNECT_TIMEOUT_SECONDS",
    )
    for name in shared_s3_vars:
        web_f = web_by_env[name]
        de_f = de_by_env[name]
        assert (web_f.source, web_f.group, web_f.injected, web_f.empty_ok, web_f.sensitive) == (
            de_f.source,
            de_f.group,
            de_f.injected,
            de_f.empty_ok,
            de_f.sensitive,
        ), name

    object_store_text = (ROOT / "apps/app-web/src/server/documents/object-store.ts").read_text(encoding="utf-8")
    for required_var in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        assert f'requireEnv("{required_var}")' in object_store_text

    str_defaults = dict(re.findall(r'env\(\s*"(S3_[A-Z0-9_]+)"\s*,\s*"([^"]+)"\s*\)', object_store_text))
    assert str_defaults["S3_BUCKET"] == RuntimeSettings.model_fields["s3_bucket"].default
    assert str_defaults["S3_REGION"] == RuntimeSettings.model_fields["s3_region"].default
    assert str_defaults["S3_RAW_PREFIX"] == RuntimeSettings.model_fields["s3_raw_prefix"].default

    int_defaults = {
        name: int(val)
        for name, val in re.findall(r'envPositiveSeconds\(\s*"(S3_[A-Z0-9_]+)"\s*,\s*(\d+)\s*\)', object_store_text)
    }
    assert (
        int_defaults["S3_CONNECT_TIMEOUT_SECONDS"] == RuntimeSettings.model_fields["s3_connect_timeout_seconds"].default
    )


def test_apply_migrations_sh_defaults_match_llm_service_settings() -> None:
    """#1375 (G3): migration timeout defaults in db/apply_migrations.sh match llm_service.config.Settings."""
    from llm_service.config import Settings as LlmSettings

    script = (ROOT / "db/apply_migrations.sh").read_text(encoding="utf-8")
    lock_timeout_match = re.search(
        r"\$\{TRUEALPHA_MIGRATION_LOCK_TIMEOUT:-\$\{MIGRATION_LOCK_TIMEOUT_SECONDS:-(\d+)s\}\}",
        script,
    )
    statement_timeout_match = re.search(
        r"\$\{TRUEALPHA_MIGRATION_STATEMENT_TIMEOUT:-\$\{MIGRATION_STATEMENT_TIMEOUT_SECONDS:-(\d+)s\}\}",
        script,
    )
    lock_budget_match = re.search(
        r"\$\{TRUEALPHA_MIGRATION_LOCK_BUDGET_SECONDS:-\$\{MIGRATION_LOCK_BUDGET_SECONDS:-(\d+)\}\}",
        script,
    )
    assert lock_timeout_match is not None
    assert statement_timeout_match is not None
    assert lock_budget_match is not None

    assert int(lock_timeout_match.group(1)) == LlmSettings.model_fields["migration_lock_timeout_seconds"].default
    assert (
        int(statement_timeout_match.group(1)) == LlmSettings.model_fields["migration_statement_timeout_seconds"].default
    )
    assert int(lock_budget_match.group(1)) == LlmSettings.model_fields["migration_lock_budget_seconds"].default


def test_env_example_contains_no_undeclared_variables() -> None:
    """#1367: .env.example must not advertise dead variables that no service manifest or compose file uses."""
    declared = {
        name
        for manifest in env_manifest.all_manifests().values()
        for field in manifest.fields
        for name in (field.env, *field.aliases)
    }
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose_interpolated = set(re.findall(r"\$\{([A-Z0-9_]+)", compose_text))
    allowed = declared | compose_interpolated | {"COMPOSE_PROJECT_NAME"}

    example_keys: set[str] = set()
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        example_keys.add(key)

    assert "JWT_ALGORITHM" not in example_keys
    assert example_keys <= allowed, f"dead .env.example keys: {sorted(example_keys - allowed)}"


def test_db_connect_passes_configured_database_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1367: DATABASE_CONNECT_TIMEOUT_SECONDS must reach psycopg.connect in both Python services."""
    import psycopg
    from data_engine import db as de_db
    from llm_service import main as llm_main

    observed: list[tuple[str, object]] = []

    def fake_connect(conninfo: str, **kwargs: object) -> object:
        observed.append((conninfo, kwargs.get("connect_timeout")))
        raise psycopg.OperationalError("probe only")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(de_db.settings, "database_connect_timeout_seconds", 11)
    with pytest.raises(psycopg.OperationalError):
        de_db.connect()
    assert observed[-1][1] == 11

    monkeypatch.setattr(llm_main.settings, "database_connect_timeout_seconds", 13)
    llm_main.health()
    assert observed[-1][1] == 13
