from pydantic import Field
from truealpha_runtime import CoreRuntimeSettings, database_url_field
from truealpha_runtime.config import POSTGRES_PROVIDED_BY

#: app stack: postgres is reached over the compose network
_APP_STACK_DSN = "postgresql://postgres:{POSTGRES_PASSWORD}@truealpha-postgres{env:ENV_SUFFIX}:5432/truealpha"


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

    database_url: str = database_url_field(composed_from=_APP_STACK_DSN)
    # The migration DSN is the same server as DATABASE_URL. infra2 supplies it with the same
    # provider and the same composition (its supplement `10.app/env.manifest.json`), and two
    # declarations of one variable must agree (#1390). Locally the field stays optional.
    migrations_database_url: str | None = Field(
        default=None,
        validation_alias="MIGRATIONS_DATABASE_URL",
        description="PostgreSQL DSN for schema migrations (superuser; the server of DATABASE_URL)",
        json_schema_extra={
            "source": "runtime",
            "group": "postgres",
            "provided_by": POSTGRES_PROVIDED_BY,
            "composed_from": _APP_STACK_DSN,
        },
    )

    # MCP streamable-HTTP transport security (FastMCP DNS-rebinding protection).
    # This MCP surface is service-identity only -- there is no browser session or
    # cookie/credential a rebinding attack could ride (see mcp_server.py), and it is
    # served behind Traefik, which already enforces the Host route. So app-layer host
    # pinning is off by default; a deployment that wants it can enable protection and
    # list its public host(s). Default-on FastMCP protection rejects any non-localhost
    # Host with "421 Invalid Host header", which is what blocked external callers.
    mcp_dns_rebinding_protection: bool = Field(default=False, json_schema_extra={"source": "code", "group": "mcp"})
    # Empty lists are acceptable while the protection is off (#1377).
    mcp_allowed_hosts: list[str] = Field(
        default_factory=list, json_schema_extra={"source": "code", "empty_ok": True, "group": "mcp"}
    )
    mcp_allowed_origins: list[str] = Field(
        default_factory=list, json_schema_extra={"source": "code", "empty_ok": True, "group": "mcp"}
    )


settings = Settings()
