from typing import Literal

from truealpha_runtime import RuntimeSettings


class Settings(RuntimeSettings):
    """Runtime settings layered on the shared runtime contract.

    `strategy_run_backend` defaults to `mart` -- the real
    `mart.strategy_runs`/`strategy_decisions` read (#362, retiring the
    checked-in golden fixture as the default consumer path). A real writer now
    populates the mart (the #414 replay writer and the #417 materialization
    asset), so the MCP `strategy_run` tool reads captured evidence, returning an
    honest `StrategyRunUnavailable(reason="no_runs_recorded")` only when the mart
    has genuinely not been materialized yet. `fixture` remains available for
    tests and offline previews, but is no longer the default.
    """

    strategy_run_backend: Literal["fixture", "mart"] = "mart"

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
