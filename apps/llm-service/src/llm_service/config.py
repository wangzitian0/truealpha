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

    # MCP streamable-HTTP transport security (FastMCP DNS-rebinding protection).
    # This MCP surface is service-identity only -- there is no browser session or
    # cookie/credential a rebinding attack could ride (see mcp_server.py), and it is
    # served behind Traefik, which already enforces the Host route. So app-layer host
    # pinning is off by default; a deployment that wants it can enable protection and
    # list its public host(s). Default-on FastMCP protection rejects any non-localhost
    # Host with "421 Invalid Host header", which is what blocked external callers.
    mcp_dns_rebinding_protection: bool = False
    mcp_allowed_hosts: list[str] = []
    mcp_allowed_origins: list[str] = []


settings = Settings()
