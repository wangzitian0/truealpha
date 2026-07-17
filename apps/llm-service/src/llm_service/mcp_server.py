"""The MCP endpoint — see #348.

Registers exactly one read-only tool, `strategy_run`, over #347's provisional
`StrategyRunReadRepository`. This is narrower than #42's eventual five-tool
surface (factor history, entity comparison, ranking, output explanation,
strategy run) over all seven modules' `ResearchQueryService`; it proves the
MCP transport/registration path on the smallest useful surface first. See
#348's "Relationship to the formal consumption epic" for why this does not
claim #42's acceptance criteria.

No browser session exists for MCP callers today, so `AccessContext` is
derived server-side via `AuthenticationMethod.SERVICE_IDENTITY` rather than
accepted from the client — the tool schema has no role/tenant/tier argument.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.strategy_run import StrategyRunReadRepository, StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository

_SERVICE_PRINCIPAL_ID = "principal:llm-service-mcp"
_SERVICE_TENANT_ID = "tenant:truealpha"
_CONTEXT_LIFETIME = timedelta(minutes=5)


class StrategyRunToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    strategy_id: str


def _service_access_context() -> AccessContext:
    """A fresh, short-lived SERVICE_IDENTITY context; never derived from client input."""
    issued_at = datetime.now(UTC)
    return AccessContext(
        context_id=f"ctx:mcp-service:{issued_at.timestamp()}",
        principal_id=_SERVICE_PRINCIPAL_ID,
        tenant_id=_SERVICE_TENANT_ID,
        session_id=f"session:mcp-service:{issued_at.timestamp()}",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=issued_at,
        expires_at=issued_at + _CONTEXT_LIFETIME,
    )


def build_mcp_server(*, repository: StrategyRunReadRepository | None = None) -> FastMCP:
    """Builds the MCP server. A caller-supplied repository is for tests only."""
    server = FastMCP("truealpha-mcp")
    active_repository: StrategyRunReadRepository = repository or FixtureStrategyRunRepository()

    @server.tool(name="strategy_run", description="Read the latest large_model_value_v0 Core Strategy run.")
    def strategy_run(request: StrategyRunToolRequest) -> StrategyRunReport | StrategyRunUnavailable:
        return active_repository.get_latest(strategy_id=request.strategy_id, context=_service_access_context())

    return server


mcp = build_mcp_server()
