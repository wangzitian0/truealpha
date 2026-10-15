from infra2_sdk.runtime.llm import LlmSettings
from pydantic import Field, SecretStr
from truealpha_runtime import RuntimeSettings


class Settings(RuntimeSettings):
    """Runtime settings layered on the shared runtime contract.

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

    llm_provider: str = Field(default="zai", json_schema_extra={"source": "code", "group": "llm"})
    llm_base_url: str = Field(
        default="https://open.bigmodel.cn/api/paas/v4", json_schema_extra={"source": "code", "group": "llm"}
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        json_schema_extra={"source": "human", "scope": "project", "empty_ok": True, "sensitive": True, "group": "llm"},
    )
    llm_model: str = Field(default="glm-5.3-flash", json_schema_extra={"source": "code", "group": "llm"})
    llm_timeout_seconds: float = Field(default=90.0, json_schema_extra={"source": "code", "group": "llm"})

    def to_llm_settings(self) -> LlmSettings:
        return LlmSettings(
            provider=self.llm_provider,
            base_url=self.llm_base_url,
            api_key=self.llm_api_key.get_secret_value() if self.llm_api_key else None,
            primary_model=self.llm_model,
            json_timeout_seconds=self.llm_timeout_seconds,
        )


settings = Settings()
LlmServiceSettings = Settings
