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
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.strategy_run import StrategyRunReadRepository, StrategyRunReport, StrategyRunUnavailable
from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository
from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository
from truealpha_contracts.topt_read import (
    PostgresToptGppeRepository,
    ToptGppeReport,
    ToptGppeUnavailable,
)

from llm_service.config import settings

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


def _default_repository() -> StrategyRunReadRepository:
    """#361: `settings.strategy_run_backend` selects the fixture or the real
    mart-backed repository; defaults to `fixture` (see that setting's own
    docstring for why flipping it isn't safe until #26 lands a writer)."""
    if settings.strategy_run_backend == "mart":
        return PostgresStrategyRunRepository(database_url=settings.database_url)
    return FixtureStrategyRunRepository()


def build_mcp_server(
    *,
    repository: StrategyRunReadRepository | None = None,
    topt_repository: PostgresToptGppeRepository | None = None,
) -> FastMCP:
    """Builds the MCP server. Caller-supplied repositories are for tests only."""
    # `streamable_http_path="/"`: main.py mounts this app at "/mcp"; FastMCP's own
    # default streamable path is also "/mcp", which double-nested the real endpoint at
    # "/mcp/mcp". Serving at the mount root keeps it a single "/mcp".
    # transport_security: see Settings.mcp_dns_rebinding_protection for why host pinning
    # is off by default for this service-identity-only surface behind Traefik.
    server = FastMCP(
        "truealpha-mcp",
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )
    active_repository: StrategyRunReadRepository = repository if repository is not None else _default_repository()
    active_topt = (
        topt_repository
        if topt_repository is not None
        else PostgresToptGppeRepository(database_url=settings.database_url)
    )

    @server.tool(name="strategy_run", description="Read the latest large_model_value_v0 Core Strategy run.")
    def strategy_run(request: StrategyRunToolRequest) -> StrategyRunReport | StrategyRunUnavailable:
        return active_repository.get_latest(strategy_id=request.strategy_id, context=_service_access_context())

    @server.tool(
        name="topt_gppe",
        description="Read the current production TOPT gross-profit-per-employee results and quality report from mart.",
    )
    def topt_gppe() -> ToptGppeReport | ToptGppeUnavailable:
        return active_topt.latest()

    return server


mcp = build_mcp_server()
