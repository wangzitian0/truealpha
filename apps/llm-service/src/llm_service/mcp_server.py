"""The MCP endpoint — see #348.

Registers three read-only tools: `strategy_run` (#347's provisional
`StrategyRunReadRepository`), `topt_gppe` (#405/#433's TOPT GPPE + quality
read), and `research_report` (#369's deterministic report assembler). This is
narrower than #42's eventual five-tool surface (factor history, entity
comparison, ranking, output explanation, strategy run) over all seven
modules' `ResearchQueryService`; it proves the MCP transport/registration
path on the smallest useful surface first. See #348's "Relationship to the
formal consumption epic" for why this does not claim #42's acceptance
criteria.

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
from truealpha_contracts.research_report import (
    ReportSectionKind,
    ResearchReadPort,
    ResearchReport,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
)
from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository
from truealpha_contracts.research_report_mart import MartResearchReadRepository
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


class ResearchReportToolRequest(BaseModel):
    # No strict=True (unlike StrategyRunToolRequest, whose sole field is already a bare
    # str): this model's enum/tuple/datetime fields need normal Pydantic coercion from the
    # JSON-shaped tool-call arguments (a JSON string into ResearchReportKind/datetime, a
    # JSON array into a tuple) — strict mode rejects those as wrong-type instances outright
    # rather than coercing them, verified empirically against a real MCP tool call.
    model_config = ConfigDict(extra="forbid")

    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...]
    cutoff_at: datetime
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    title: str | None = None


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
    """#362: `settings.strategy_run_backend` selects the fixture or the real
    mart-backed repository; now defaults to `mart` (a real writer populates it
    via #414/#417) — `fixture` remains selectable for tests/offline previews."""
    if settings.strategy_run_backend == "mart":
        return PostgresStrategyRunRepository(database_url=settings.database_url)
    return FixtureStrategyRunRepository()


def _default_research_report_repository() -> ResearchReadPort:
    """#369: mirrors `_default_repository`'s `strategy_run_backend` flag exactly, since
    `MartResearchReadRepository`/`FixtureResearchReadRepository` wrap the same underlying
    strategy-run data source this flag already governs — both tools flip to real mart
    reads together, not independently."""
    if settings.strategy_run_backend == "mart":
        return MartResearchReadRepository(database_url=settings.database_url)
    return FixtureResearchReadRepository()


def build_mcp_server(
    *,
    repository: StrategyRunReadRepository | None = None,
    topt_repository: PostgresToptGppeRepository | None = None,
    research_repository: ResearchReadPort | None = None,
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
    active_research: ResearchReadPort = (
        research_repository if research_repository is not None else _default_research_report_repository()
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

    @server.tool(
        name="research_report",
        description=(
            "Assemble a deterministic research report (company, ETF, or theme ranking) by selecting "
            "already-materialized sections and trace links over mart outputs. Computes no new metric."
        ),
    )
    def research_report(request: ResearchReportToolRequest) -> ResearchReport:
        report_request = ResearchReportRequest(
            report_kind=request.report_kind,
            target_entity_ids=request.target_entity_ids,
            cutoff_at=request.cutoff_at,
            section_kinds=request.section_kinds,
            strategy_id=request.strategy_id,
            title=request.title,
        )
        return build_research_report(report_request, active_research, context=_service_access_context())

    return server


mcp = build_mcp_server()
