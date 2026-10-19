from pydantic import AliasChoices, Field
from truealpha_runtime import CoreRuntimeSettings


class Settings(CoreRuntimeSettings):
    """Runtime settings layered on the core runtime contract.

    `llm-service` serves the MCP streamable-HTTP surface and `/health` over `mart`, and
    its container entrypoint runs `db/apply_migrations.sh`. It never accesses S3 object
    storage (AGENTS.md red line) and never calls an external LLM API.

    There is no fixture backend to select (#434 exit criterion 3, 2026-09-07): the MCP
    tools read the real `mart` and nothing else. The checked-in golden fixture lives in
    tests only (`truealpha_contracts.strategy_run_fixture`, injected through
    `build_mcp_server(repository=...)`), never behind a runtime flag a deployed process
    could flip. CLAUDE.md: fixture data lives in tests only and is never reachable from a
    deployed route.
    """

    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/truealpha",
        json_schema_extra={
            "source": "runtime",
            "group": "postgres",
            "provided_by": "truealpha/postgres:POSTGRES_PASSWORD",
            # app stack: reaches postgres over the compose network
            "composed_from": "postgresql://postgres:{POSTGRES_PASSWORD}@truealpha-postgres{env:ENV_SUFFIX}:5432/truealpha",
        },
    )
    migrations_database_url: str | None = Field(
        default=None,
        validation_alias="MIGRATIONS_DATABASE_URL",
        description="Optional PostgreSQL DSN override for schema migrations",
        json_schema_extra={"source": "runtime", "group": "postgres", "empty_ok": True},
    )
    migration_lock_budget_seconds: int = Field(
        default=25,
        validation_alias=AliasChoices("MIGRATION_LOCK_BUDGET_SECONDS", "TRUEALPHA_MIGRATION_LOCK_BUDGET_SECONDS"),
        description="Maximum seconds spent waiting on migration locks",
        json_schema_extra={"source": "code", "group": "postgres"},
    )
    migration_lock_timeout_seconds: int = Field(
        default=5,
        validation_alias=AliasChoices("MIGRATION_LOCK_TIMEOUT_SECONDS", "TRUEALPHA_MIGRATION_LOCK_TIMEOUT"),
        description="Migration lock timeout seconds",
        json_schema_extra={"source": "code", "group": "postgres"},
    )
    migration_statement_timeout_seconds: int = Field(
        default=60,
        validation_alias=AliasChoices("MIGRATION_STATEMENT_TIMEOUT_SECONDS", "TRUEALPHA_MIGRATION_STATEMENT_TIMEOUT"),
        description="Migration statement timeout seconds",
        json_schema_extra={"source": "code", "group": "postgres"},
    )

    # MCP streamable-HTTP transport security (FastMCP DNS-rebinding protection).
    # This MCP surface is service-identity only -- there is no browser session or
    # cookie/credential a rebinding attack could ride (see mcp_server.py), and it is
    # served behind Traefik, which already enforces the Host route. So app-layer host
    # pinning is off by default; a deployment that wants it can enable protection and
    # list its public host(s). Default-on FastMCP protection rejects any non-localhost
    # Host with "421 Invalid Host header", which is what blocked external callers.
    mcp_dns_rebinding_protection: bool = Field(default=False, json_schema_extra={"source": "code", "group": "mcp"})
    mcp_allowed_hosts: list[str] = Field(default_factory=list, json_schema_extra={"source": "code", "group": "mcp"})
    mcp_allowed_origins: list[str] = Field(default_factory=list, json_schema_extra={"source": "code", "group": "mcp"})


settings = Settings()
LlmServiceSettings = Settings
