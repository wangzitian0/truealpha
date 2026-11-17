"""The MCP endpoint registering read-only mart tools behind BearerTokenGate (#348, #758, #1104, #42)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from truealpha_runtime.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_runtime.issuer_modules_mart import (
    AnalystRatings,
    IssuerModuleReader,
    IssuerModuleUnavailable,
    PostgresIssuerModuleReader,
    SupplyChainExposure,
)
from truealpha_runtime.research_cards import CardKind, ResearchCard
from truealpha_runtime.research_report import (
    ReportSectionKind,
    ResearchReadPort,
    ResearchReport,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
    render_report_markdown,
)
from truealpha_runtime.research_report_mart import (
    MartResearchReadRepository,
    MetricNodeValueReport,
    MetricNodeValueUnavailable,
    PostgresMetricNodeValueRepository,
)
from truealpha_runtime.strategy_run import StrategyRunReadRepository, StrategyRunReport, StrategyRunUnavailable
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository

from llm_service.config import settings
from llm_service.services.research_service import (
    CardExportRequest,
    PostgresBacktestReader,
    PostgresCompanyProfileReader,
    PostgresEtfProfileReader,
    PostgresThemePurityLeaderboardReader,
    backtest_run_limitations,
    export_or_render_card,
    normalize_theme_slug,
    resolve_fund_resolution_candidates,
)

__all__ = [
    "PostgresBacktestReader",
    "PostgresCompanyProfileReader",
    "PostgresEtfProfileReader",
    "PostgresThemePurityLeaderboardReader",
    "backtest_run_limitations",
    "build_mcp_server",
    "mcp",
    "normalize_theme_slug",
    "resolve_fund_resolution_candidates",
]

logger = logging.getLogger(__name__)

_SERVICE_PRINCIPAL_ID = "principal:llm-service-mcp"
_OWNER_PRINCIPAL_ID = "principal:owner"
_SERVICE_TENANT_ID = "tenant:truealpha"
_DELEGATION_ID = "delegation:mcp-bearer-token"
_CONTEXT_LIFETIME = timedelta(minutes=5)
MART_READ_FAILED_MESSAGE = "the mart read failed; the detail is in the service log"


class _ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrategyRunToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy_id: str


class ResearchReportToolRequest(_ToolModel):
    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...]
    cutoff_at: datetime
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    title: str | None = None


class ResearchReportMarkdown(_ToolModel):
    report_id: str
    schema_version: str
    markdown: str


class ResearchCardToolRequest(ResearchReportToolRequest):
    card_kind: CardKind
    style_preset: str | None = None
    page: int | None = None


class IssuerModuleToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issuer_id: str


class CompanyProfileToolRequest(_ToolModel):
    issuer_id: str


class ThemePurityToolRequest(_ToolModel):
    theme_id: str
    limit: int = 10


class EtfProfileToolRequest(_ToolModel):
    fund_id: str = "etf:series:S000101292"


class BacktestToolRequest(_ToolModel):
    run_id: str | None = None
    strategy_key: str | None = None
    limit: int = 10


class MetricNodeValueToolRequest(_ToolModel):
    node_key: str
    issuer_id: str | None = None
    limit: int = 100


def _owner_access_context() -> AccessContext:
    issued_at = datetime.now(UTC)
    stamp = issued_at.timestamp()
    return AccessContext(
        context_id=f"ctx:mcp-owner:{stamp}",
        principal_id=_OWNER_PRINCIPAL_ID,
        tenant_id=_SERVICE_TENANT_ID,
        session_id=f"session:mcp-owner:{stamp}",
        authentication_method=AuthenticationMethod.DELEGATED_MCP,
        principal_kind=PrincipalKind.ADMINISTRATOR,
        issued_at=issued_at,
        expires_at=issued_at + _CONTEXT_LIFETIME,
        delegated_by_service_principal_id=_SERVICE_PRINCIPAL_ID,
        delegation_id=_DELEGATION_ID,
    )


class _AuditedFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        started = datetime.now(UTC)
        outcome = "error"
        try:
            result = await super().call_tool(name, arguments)
            outcome = "ok"
            return result
        finally:
            elapsed_ms = round((datetime.now(UTC) - started).total_seconds() * 1000)
            logger.info(
                "mcp tool call: tool=%s principal=%s outcome=%s duration_ms=%d",
                name,
                _OWNER_PRINCIPAL_ID,
                outcome,
                elapsed_ms,
            )


def _default_repository() -> StrategyRunReadRepository:
    return PostgresStrategyRunRepository(database_url=settings.database_url)


def _default_research_report_repository() -> ResearchReadPort:
    return MartResearchReadRepository(database_url=settings.database_url)


def _default_issuer_module_reader() -> IssuerModuleReader:
    return PostgresIssuerModuleReader(database_url=settings.database_url)


def _module_row(
    read: Callable[[str], SupplyChainExposure | AnalystRatings | None], issuer_id: str, tool: str
) -> SupplyChainExposure | AnalystRatings | None:
    try:
        return read(issuer_id)
    except psycopg.Error:
        logger.exception("%s: the mart read failed", tool)
        raise ToolError(MART_READ_FAILED_MESSAGE) from None


def _handle_issuer_module(
    read_fn: Callable[[str], SupplyChainExposure | AnalystRatings | None],
    expected_type: type[SupplyChainExposure | AnalystRatings],
    module_name: Literal["supply_chain_exposure", "analyst_ratings"],
    issuer_id: str,
    tool_name: str,
) -> Any:
    row = _module_row(read_fn, issuer_id, tool_name)
    return row if isinstance(row, expected_type) else IssuerModuleUnavailable(issuer_id=issuer_id, module=module_name)


def _report_request(request: ResearchReportToolRequest) -> ResearchReportRequest:
    return ResearchReportRequest(
        report_kind=request.report_kind,
        target_entity_ids=request.target_entity_ids,
        cutoff_at=request.cutoff_at,
        section_kinds=request.section_kinds,
        strategy_id=request.strategy_id,
        title=request.title,
    )


def _card_report_request(request: ResearchCardToolRequest) -> ResearchReportRequest:
    report_kind = request.report_kind
    if (
        request.card_kind is CardKind.RANKING
        and report_kind is ResearchReportKind.COMPANY
        and all(t.strip().lower().startswith("theme:") for t in request.target_entity_ids if t.strip())
    ):
        report_kind = ResearchReportKind.THEME_RANKING
    section_kinds = () if request.section_kinds == (ReportSectionKind.RANKING,) else request.section_kinds
    return ResearchReportRequest(
        report_kind=report_kind,
        target_entity_ids=request.target_entity_ids,
        cutoff_at=request.cutoff_at,
        section_kinds=section_kinds,
        strategy_id=request.strategy_id,
        title=request.title,
    )


class CompanyProfileReader(Protocol):
    def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]: ...


class ThemePurityLeaderboardReader(Protocol):
    def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]: ...


class EtfProfileReader(Protocol):
    def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]: ...


class BacktestReader(Protocol):
    def get_backtest_report(
        self, *, run_id: str | None = None, strategy_key: str | None = None, limit: int = 10
    ) -> dict[str, Any]: ...


def _default_company_profile_reader() -> CompanyProfileReader:
    return PostgresCompanyProfileReader(database_url=settings.database_url)


def _default_theme_purity_reader() -> ThemePurityLeaderboardReader:
    return PostgresThemePurityLeaderboardReader(database_url=settings.database_url)


def _default_etf_profile_reader() -> EtfProfileReader:
    return PostgresEtfProfileReader(database_url=settings.database_url)


def _default_backtest_reader() -> BacktestReader:
    return PostgresBacktestReader(database_url=settings.database_url)


def build_mcp_server(
    *,
    repository: StrategyRunReadRepository | None = None,
    topt_repository: Any | None = None,
    metric_node_repository: PostgresMetricNodeValueRepository | None = None,
    research_repository: ResearchReadPort | None = None,
    company_profile_reader: CompanyProfileReader | None = None,
    theme_purity_reader: ThemePurityLeaderboardReader | None = None,
    etf_profile_reader: EtfProfileReader | None = None,
    backtest_reader: BacktestReader | None = None,
    issuer_module_reader: IssuerModuleReader | None = None,
) -> FastMCP:
    server = _AuditedFastMCP(
        "truealpha-mcp",
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )
    url = settings.database_url
    active_repository = repository or _default_repository()
    active_metric_node = metric_node_repository or PostgresMetricNodeValueRepository(database_url=url)
    active_research = research_repository or _default_research_report_repository()
    active_company_profile = company_profile_reader or _default_company_profile_reader()
    active_theme_purity = theme_purity_reader or _default_theme_purity_reader()
    active_etf_profile = etf_profile_reader or _default_etf_profile_reader()
    active_backtest = backtest_reader or _default_backtest_reader()
    active_modules = issuer_module_reader or _default_issuer_module_reader()

    @server.tool(
        name="strategy_run",
        description="Read the latest large_model_value_v0 Core Strategy run and its definition review_state from mart.",
    )
    def strategy_run(request: StrategyRunToolRequest) -> StrategyRunReport | StrategyRunUnavailable:
        return active_repository.get_latest(strategy_id=request.strategy_id, context=_owner_access_context())

    @server.tool(
        name="topt_gppe",
        description="Read the current materialized GPPE metric node outputs at the governed head.",
    )
    def topt_gppe() -> MetricNodeValueReport | MetricNodeValueUnavailable:
        if topt_repository is not None:
            return topt_repository.latest()  # type: ignore[no-any-return]
        return active_metric_node.latest(node_key="gppe_uniform_charge_v0")

    @server.tool(
        name="metric_node_values",
        description="Read materialized metric-forest node outputs by node_key at the governed head.",
    )
    def metric_node_values(request: MetricNodeValueToolRequest) -> MetricNodeValueReport | MetricNodeValueUnavailable:
        return active_metric_node.latest(node_key=request.node_key, issuer_id=request.issuer_id, limit=request.limit)

    @server.tool(
        name="research_report",
        description="Assemble a deterministic research report (company, ETF, or theme ranking) over mart outputs.",
    )
    def research_report(request: ResearchReportToolRequest) -> ResearchReport:
        return build_research_report(_report_request(request), active_research, context=_owner_access_context())

    @server.tool(
        name="research_report_markdown",
        description="Render the deterministic research report of a request as Markdown with value, period, confidence and trace.",
    )
    def research_report_markdown(request: ResearchReportToolRequest) -> ResearchReportMarkdown:
        report = build_research_report(_report_request(request), active_research, context=_owner_access_context())
        return ResearchReportMarkdown(
            report_id=report.report_id,
            schema_version=report.schema_version,
            markdown=render_report_markdown(report),
        )

    @server.tool(
        name="research_card",
        description="Render a versioned research card via the unified OpenAPI/MCP card service.",
    )
    def research_card(request: ResearchCardToolRequest) -> ResearchCard:
        report_req = _card_report_request(request)
        exported = export_or_render_card(
            _owner_access_context(),
            CardExportRequest(
                card_kind=request.card_kind,
                issuer_id=report_req.target_entity_ids[0] if report_req.target_entity_ids else None,
                cutoff=report_req.cutoff_at.isoformat(),
                preset=request.style_preset,
                page=request.page,
                title=request.title,
                format="html",
                persist=getattr(active_research, "_database_url", None) is not None,
            ),
            research_repo=active_research,
            report_request=report_req,
        )
        return ResearchCard.model_validate_json(
            json.dumps({**exported.card, "rendered_html": exported.rendered_html, "document_id": exported.document_id})
        )

    @server.tool(
        name="company_360_profile",
        description="Read an issuer's 360 profile across GPPE, valuation tier, valuation gap, PEG, and theme purity.",
    )
    def company_360_profile(request: CompanyProfileToolRequest) -> dict[str, Any]:
        return active_company_profile.get_company_profile(issuer_id=request.issuer_id)

    @server.tool(
        name="theme_purity_leaderboard",
        description="Read the theme purity leaderboard ranking the purest issuers under a thematic definition.",
    )
    def theme_purity_leaderboard(request: ThemePurityToolRequest) -> dict[str, Any]:
        return active_theme_purity.get_leaderboard(theme_id=request.theme_id, limit=request.limit)

    @server.tool(
        name="etf_virtual_company_profile",
        description="Read an ETF virtual-company consolidation profile and holdings valuation from mart.",
    )
    def etf_virtual_company_profile(request: EtfProfileToolRequest) -> dict[str, Any]:
        return active_etf_profile.get_etf_profile(fund_id=request.fund_id)

    @server.tool(
        name="issuer_supply_chain_exposure",
        description="Read module 3 supply-chain exposure of an issuer from the governed head in mart.served_head.",
    )
    def issuer_supply_chain_exposure(request: IssuerModuleToolRequest) -> SupplyChainExposure | IssuerModuleUnavailable:
        return _handle_issuer_module(
            active_modules.supply_chain_exposure,
            SupplyChainExposure,
            "supply_chain_exposure",
            request.issuer_id,
            "issuer_supply_chain_exposure",
        )

    @server.tool(
        name="issuer_analyst_ratings",
        description="Read module 4 analyst consensus of an issuer from the governed head in mart.served_head.",
    )
    def issuer_analyst_ratings(request: IssuerModuleToolRequest) -> AnalystRatings | IssuerModuleUnavailable:
        return _handle_issuer_module(
            active_modules.analyst_ratings,
            AnalystRatings,
            "analyst_ratings",
            request.issuer_id,
            "issuer_analyst_ratings",
        )

    @server.tool(
        name="governed_backtest",
        description="Read governed backtest execution runs, performance metrics, valuations and simulated trades from mart.",
    )
    def governed_backtest(request: BacktestToolRequest) -> dict[str, Any]:
        return active_backtest.get_backtest_report(
            run_id=request.run_id, strategy_key=request.strategy_key, limit=request.limit
        )

    return server


class _LazyMcp:
    """Defer build_mcp_server() and Settings() validation until runtime (#1375)."""

    def __init__(self) -> None:
        self._instance: FastMCP | None = None
        self._http_app: Any = None

    def _get(self) -> FastMCP:
        if self._instance is None:
            inst = build_mcp_server()
            self._http_app = inst.streamable_http_app()
            self._instance = inst
        return self._instance

    def streamable_http_app(self) -> Any:
        lazy = self

        async def _asgi(scope: Any, receive: Any, send: Any) -> None:
            lazy._get()
            await lazy._http_app(scope, receive, send)

        return _asgi

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


mcp: Any = _LazyMcp()
