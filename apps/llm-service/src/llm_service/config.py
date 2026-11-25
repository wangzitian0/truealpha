from typing import Any

from infra2_sdk.runtime.environment import RuntimeEnvKey
from pydantic import Field, SecretStr
from truealpha_runtime import RuntimeSettings
from truealpha_runtime.config import DEFAULT_DATABASE_URL, POSTGRES_PROVIDED_BY, _schema_field

#: app stack: runtime requests connect as non-superuser app_service_login (#432 Stage B)
_APP_STACK_DSN = (
    "postgresql://app_service_login:{APP_SERVICE_DB_PASSWORD}@truealpha-postgres{env:ENV_SUFFIX}:5432/truealpha"
)
_MIGRATIONS_STACK_DSN = "postgresql://postgres:{POSTGRES_PASSWORD}@truealpha-postgres{env:ENV_SUFFIX}:5432/truealpha"


class Settings(RuntimeSettings):
    """Runtime settings layered on the runtime contract.

    `llm-service` serves the OpenAPI 3.1 API, MCP streamable-HTTP surface, `/health`,
    and S3-backed document storage over `mart` and `app`, and its container entrypoint
    runs `db/apply_migrations.sh`. It never calls an external LLM API.

    There is no fixture backend to select (#434 exit criterion 3, 2026-09-07): the MCP
    tools and OpenAPI endpoints read the real `mart` and `app` schemas and nothing else.
    AGENTS.md: fixture data lives in tests only and is never reachable from a deployed route.
    """

    # The deployment injects the hash of the configuration it rendered (infra2_sdk shared env).
    configuration_sha256: str = _schema_field(
        "", "release", "release", validation_alias=RuntimeEnvKey.CONFIGURATION_SHA256.value
    )
    database_url: str = _schema_field(DEFAULT_DATABASE_URL, "code", "postgres", composed_from=_APP_STACK_DSN)
    app_service_db_password: SecretStr = Field(
        default=SecretStr(""),
        description="app_service_login database password for runtime DATABASE_URL (#432 Stage B)",
        json_schema_extra={"source": "runtime", "sensitive": True, "empty_ok": True, "group": "postgres"},
    )
    # Migrations require superuser DDL privileges; infra2 supplies MIGRATIONS_DATABASE_URL from
    # truealpha/postgres:POSTGRES_PASSWORD (#1390). Locally the field stays optional.
    migrations_database_url: str | None = Field(
        default=None,
        validation_alias="MIGRATIONS_DATABASE_URL",
        description="PostgreSQL DSN for schema migrations (superuser; the server of DATABASE_URL)",
        json_schema_extra={
            "source": "runtime",
            "group": "postgres",
            "provided_by": POSTGRES_PROVIDED_BY,
            "composed_from": _MIGRATIONS_STACK_DSN,
        },
    )

    secret_key: SecretStr = Field(
        default=SecretStr(""),
        description="HS256 session signing key",
        json_schema_extra={"source": "runtime", "injected": True, "sensitive": True, "group": "auth"},
    )
    access_token_expire_minutes: int = Field(
        default=60 * 24 * 7,
        ge=1,
        description="Session token lifetime in minutes",
        json_schema_extra={"source": "code", "group": "auth"},
    )
    s3_documents_prefix: str = Field(
        default="documents",
        description="Object key prefix for stored documents",
        json_schema_extra={"source": "code", "group": "s3"},
    )

    # The credential the MCP route requires (#42, init.md rule 21). A client sends it as
    # `Authorization: Bearer <token>`; `mcp_auth.BearerTokenGate` answers 401 to any other
    # request. The deployment generates the value and mirrors it to 1Password, so the
    # owner can read it for an MCP client. `injected`: a deployed process refuses to boot
    # without it (#759). A local or CI process boots without it, and the gate then answers
    # 401 to every request: an unconfigured MCP route is closed, never open.
    mcp_bearer_token: SecretStr | None = Field(
        default=None,
        description="Bearer token that every request to the MCP route must present",
        json_schema_extra={"source": "runtime", "injected": True, "mirror_to_1password": True, "group": "mcp"},
    )

    # MCP streamable-HTTP transport security (FastMCP DNS-rebinding protection).
    # The MCP credential is a bearer header, not a cookie, so a browser does not attach it
    # to a rebound request. The surface is served behind Traefik, which already enforces
    # the Host route. So app-layer host pinning is off by default; a deployment that wants
    # it can enable protection and list its public host(s). Default-on FastMCP protection
    # rejects any non-localhost Host with "421 Invalid Host header", which is what blocked
    # external callers.
    mcp_dns_rebinding_protection: bool = Field(default=False, json_schema_extra={"source": "code", "group": "mcp"})
    # Empty lists are acceptable while the protection is off (#1377).
    mcp_allowed_hosts: list[str] = Field(
        default_factory=list, json_schema_extra={"source": "code", "empty_ok": True, "group": "mcp"}
    )
    mcp_allowed_origins: list[str] = Field(
        default_factory=list, json_schema_extra={"source": "code", "empty_ok": True, "group": "mcp"}
    )
    # infra2_sdk.fastapi.init_service reads these three names from the process environment (main.py).
    # An empty endpoint keeps telemetry off.
    otel_exporter_otlp_endpoint: str = _schema_field("", "code", "otel", empty_ok=True)
    otel_service_name: str = _schema_field("truealpha-llm-service", "code", "otel")
    otel_resource_attributes: str = _schema_field("", "code", "otel", empty_ok=True)


class _LazySettings:
    """Defer Settings() validation until first access so cold imports stay side-effect free (#1375)."""

    def __init__(self) -> None:
        object.__setattr__(self, "_instance", None)

    def _get(self) -> Settings:
        inst = object.__getattribute__(self, "_instance")
        if inst is None:
            inst = Settings()
            object.__setattr__(self, "_instance", inst)
        return inst  # type: ignore[no-any-return]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._get(), name, value)


settings: Any = _LazySettings()
