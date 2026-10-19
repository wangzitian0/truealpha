"""Environment manifests are true (#759): fresh, past the offline gate, and release values derive from the repo."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import EnvironmentManifest
from pydantic_core import PydanticUndefined
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


def _carries_no_value(default: object) -> bool:
    """True for no default, None, a blank string or an empty collection.

    A number or a boolean always carries a value."""
    if default is None or default is PydanticUndefined:
        return True
    if isinstance(default, str):
        return not default.strip()
    if isinstance(default, (list, tuple, set, frozenset, dict)):
        return not default
    return False


def test_every_code_setting_that_runs_on_its_default_has_a_value() -> None:
    """#1377: a `code` setting is a default of the settings model, never stored.

    `FieldSource.CODE` in `infra2_sdk/runtime/config_schema.py` states this contract. The
    deployment does not supply a code setting unless the manifest marks it `injected`. The
    model default is then the value that runs. #1366 made `SEC_USER_AGENT` a code setting with
    the default "". Every SEC request on staging then raised a RuntimeError.

    An injected setting is out of scope: boot validation requires the deployment to supply it.
    An `empty_ok` setting declares that an empty value is acceptable. The hand-written app-web
    manifest has no settings model. Its defaults are TypeScript literals, so this check cannot
    read them."""
    problems: list[str] = []
    for spec in env_manifest.SPECS:
        model = spec.resolve_model()
        for field in env_manifest.load(spec.path).fields:
            if field.source != "code" or field.injected or field.empty_ok:
                continue
            default = model.model_fields[field.field].get_default(call_default_factory=True, validated_data={})
            if _carries_no_value(default):
                problems.append(
                    f"{spec.path}: {field.env} has source={field.source!r}, injected=false and empty_ok=false, "
                    f"but its default is {default!r}. A code setting is a default of the settings model, "
                    "never stored (infra2_sdk/runtime/config_schema.py). The deployment does not supply it, "
                    "so the empty default is the value that runs. Set a non-empty default, inject the value, "
                    "or declare empty_ok."
                )
    assert problems == [], "\n".join(problems)


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
    # #1377: the SEC contact email lives in Vault, out of this public repository.
    sec = by_env["SEC_USER_AGENT"]
    assert (sec.source, sec.injected, sec.empty_ok, sec.scope) == ("human", True, False, "project")
    llm = by_env["LLM_MODEL"]
    assert (llm.source, llm.injected, llm.empty_ok, llm.scope) == ("code", False, False, "env")
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
    assert "missing: SEC_USER_AGENT" in out and "missing: GIT_COMMIT_SHA" in out
    assert "missing: RELEASE_ID" in out
    assert "missing: LLM_MODEL" not in out


def test_llm_service_manifest_excludes_s3_and_unused_llm_variables_and_declares_migrations() -> None:
    """#1367: llm-service inherits CoreRuntimeSettings (no S3_* or unused LLM_* fields) and
    owns MIGRATIONS_DATABASE_URL consumed by its container entrypoint."""
    llm_manifest = env_manifest.load("apps/llm-service/required-env.generated.json")
    llm_envs = {field.env for field in llm_manifest.fields}
    assert not any(env.startswith(("S3_", "LLM_")) for env in llm_envs), llm_envs
    assert "MIGRATIONS_DATABASE_URL" in llm_envs

    de_manifest = env_manifest.load("apps/data-engine/required-env.generated.json")
    de_envs = {field.env for field in de_manifest.fields}
    assert "MIGRATIONS_DATABASE_URL" not in de_envs
    assert "S3_DOCUMENTS_PREFIX" not in de_envs


def test_llm_service_migrations_url_is_supplied_like_the_database_url() -> None:
    """#1390: the migration DSN is the server of DATABASE_URL, and infra2 supplies both the same way.

    infra2 declares MIGRATIONS_DATABASE_URL in its own supplement for the app stack, with the
    provider and the composition of DATABASE_URL. It refuses two declarations of one variable that
    differ in source, provided_by, composed_from, store_key, empty_ok or scope. #1367 declared the
    field here without a provider, so a sync of this manifest into infra2 could not render a template.
    One DSN, one declaration: the two entries agree on every key that infra2 compares."""
    fields = {field.env: field for field in env_manifest.load("apps/llm-service/required-env.generated.json").fields}
    database, migrations = fields["DATABASE_URL"], fields["MIGRATIONS_DATABASE_URL"]
    assert database.provided_by and database.composed_from, "DATABASE_URL lost its provider or its composition"
    for key in ("source", "provided_by", "composed_from", "store_key", "empty_ok", "scope"):
        assert getattr(migrations, key) == getattr(database, key), (
            f"MIGRATIONS_DATABASE_URL.{key} is {getattr(migrations, key)!r} and DATABASE_URL.{key} is "
            f"{getattr(database, key)!r}; infra2 supplies both the same way (#1390)"
        )


def _strip_ts_comments(text: str) -> str:
    """Strip block and line comments from TypeScript source before scanning process.env reads."""
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$|(?<=\s)//.*$", "", without_block)


def test_app_web_manifest_declares_every_process_env_variable_read_by_server_code() -> None:
    """#1367, #1375 (G2), #1389: app-web server code and required-env.manifest.json match
    bidirectionally after stripping comments."""
    raw = json.loads((ROOT / "apps/app-web/required-env.manifest.json").read_text(encoding="utf-8"))
    manifest = EnvironmentManifest.from_dict(raw)
    declared = {field.env for field in manifest.fields}

    pattern = re.compile(
        r"""process\.env\.([A-Z0-9_]+)|process\.env\[\s*["']([A-Z0-9_]+)["']\s*\]|(?:env|envPositiveSeconds|requireEnv)\(\s*["']([A-Z0-9_]+)["']"""
    )
    read_in_src: set[str] = set()
    for path in (ROOT / "apps/app-web/src").rglob("*.ts*"):
        text = _strip_ts_comments(path.read_text(encoding="utf-8"))
        for match in pattern.finditer(text):
            name = match.group(1) or match.group(2) or match.group(3)
            if name:
                read_in_src.add(name)

    assert read_in_src == declared, (
        f"app-web env drift: undeclared={sorted(read_in_src - declared)}, "
        f"unused_in_src={sorted(declared - read_in_src)}"
    )


def test_app_web_s3_contract_matches_python_runtime_settings() -> None:
    """#1375 (G3), #1389 (F4, F6): S3 metadata and fallback defaults in app-web match Python
    RuntimeSettings, shared 10.app fields match llm-service, and docker-compose.yml passes no
    dead S3 variables to llm-service."""
    from truealpha_runtime import RuntimeSettings

    web_raw = json.loads((ROOT / "apps/app-web/required-env.manifest.json").read_text(encoding="utf-8"))
    web_by_env = {field.env: field for field in EnvironmentManifest.from_dict(web_raw).fields}
    de_by_env = {field.env: field for field in env_manifest.load("apps/data-engine/required-env.generated.json").fields}
    llm_by_env = {
        field.env: field for field in env_manifest.load("apps/llm-service/required-env.generated.json").fields
    }

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

    for name in ("APP_ENV", "GIT_COMMIT_SHA", "DATABASE_URL"):
        web_f = web_by_env[name]
        llm_f = llm_by_env[name]
        assert (
            web_f.source,
            web_f.group,
            web_f.empty_ok,
            web_f.provided_by,
            web_f.composed_from,
        ) == (
            llm_f.source,
            llm_f.group,
            llm_f.empty_ok,
            llm_f.provided_by,
            llm_f.composed_from,
        ), name
    assert llm_by_env["DATABASE_CONNECT_TIMEOUT_SECONDS"].group == "postgres"
    assert de_by_env["DATABASE_CONNECT_TIMEOUT_SECONDS"].group == "postgres"

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

    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    llm_compose = compose["services"]["llm-service"]
    llm_compose_env = llm_compose.get("environment") or {}
    assert not any(str(key).startswith("S3_") for key in llm_compose_env), llm_compose_env
    assert "minio-init" not in (llm_compose.get("depends_on") or {})


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
