"""The MCP endpoint registering read-only research mart and admin fact tools behind BearerTokenGate (#1748)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from truealpha_runtime.access import PrincipalKind
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
    ResearchReportKind,
    ResearchReportRequest,
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
from llm_service.services.admin_service import (
    DatahubStatsResponse,
    OpsOverviewResponse,
    build_datahub_fact_diff,
    get_datahub_stats,
    get_ops_overview,
)
from llm_service.services.auth_service import MCP_OWNER_PRINCIPAL_ID, resolve_mcp_access_context
from llm_service.services.entities_service import (
    TraceResponse,
    get_company_profile_payload,
    get_etf_profile_payload,
    get_theme_purity_leaderboard,
    get_trace,
    normalize_theme_slug,
    resolve_fund_resolution_candidates,
)
from llm_service.services.library_service import (
    CardExportRequest,
    CreateDocumentResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    LibraryReportResponse,
    build_library_report,
    create_document,
    export_or_render_card,
    get_document_detail,
    list_documents,
)
from llm_service.services.panel_service import (
    MacroPanelResponse,
    PanelCoverageResponse,
    UniverseOverviewResponse,
    load_macro_curve,
    load_universes_panel,
    load_wide_panel_coverage,
    metric_node_values,
)
from llm_service.services.strategy_service import (
    DASHBOARD_STRATEGY_ID,
    PostgresBacktestReader,
    TriggerBacktestRequest,
    TriggerBacktestResponse,
    backtest_run_limitations,
    call_strategy_repository,
    trigger_backtest_run,
)

__all__ = [
    "PostgresBacktestReader",
    "backtest_run_limitations",
    "build_mcp_server",
    "get_company_profile_payload",
    "get_etf_profile_payload",
    "get_theme_purity_leaderboard",
    "normalize_theme_slug",
    "resolve_fund_resolution_candidates",
]

logger = logging.getLogger(__name__)

MART_READ_FAILED_MESSAGE = "the mart read failed; the detail is in the service log"


class _ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PanelCoverageToolRequest(_ToolModel):
    strategy_id: str = DASHBOARD_STRATEGY_ID
    universe: str | None = None
    cutoff: str | None = None
    node_key: str | None = None
    issuer_id: str | None = None
    limit: int = 100


class StrategyRankingsToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy_id: str = DASHBOARD_STRATEGY_ID
    universe: str | None = None
    convention: str = "historical_cagr"


class StrategyBacktestsToolRequest(_ToolModel):
    strategy_key: str | None = None
    limit: int = 20


class StrategyBacktestDetailToolRequest(_ToolModel):
    run_id: str


class StrategyBacktestTriggerToolRequest(_ToolModel):
    definition_resource: str | None = None
    start_cutoff: str | None = None
    end_cutoff: str | None = None
    executed_at: str | None = None


class EntitiesHoldingsToolRequest(_ToolModel):
    fund_id: str = "etf:series:S000101292"


class EntitiesThemesToolRequest(_ToolModel):
    theme_id: str
    limit: int = 10


class EntitiesTraceToolRequest(_ToolModel):
    issuer_id: str
    cutoff: str | None = None


class EntitiesIssuerModuleToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issuer_id: str


class EntitiesDetailToolRequest(_ToolModel):
    issuer_id: str


class LibraryReportToolRequest(_ToolModel):
    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...]
    cutoff_at: datetime
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    title: str | None = None
    format: Literal["json", "markdown"] = "json"


class LibraryCardExportToolRequest(_ToolModel):
    report_kind: ResearchReportKind
    target_entity_ids: tuple[str, ...]
    cutoff_at: datetime
    card_kind: CardKind
    section_kinds: tuple[ReportSectionKind, ...] = ()
    strategy_id: str | None = None
    style_preset: str | None = None
    page: int | None = None
    title: str | None = None


class LibraryDocumentsListToolRequest(_ToolModel):
    limit: int | None = None
    before_created_at: str | None = None
    before_document_id: str | None = None


class LibraryDocumentCreateToolRequest(_ToolModel):
    source_artifact_id: str
    content: str
    content_type: str = "text/plain; charset=utf-8"


class LibraryDocumentDetailToolRequest(_ToolModel):
    document_id: str


class AdminDatahubFactsResponse(BaseModel):
    facts: DatahubStatsResponse
    diff: dict[str, Any]


def _require_mcp_admin() -> None:
    ctx = resolve_mcp_access_context()
    if ctx.principal_kind is not PrincipalKind.ADMINISTRATOR:
        raise ToolError("administrator principal required")


class _AuditedFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from datetime import UTC

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
                MCP_OWNER_PRINCIPAL_ID,
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


def _card_report_request(request: LibraryCardExportToolRequest) -> ResearchReportRequest:
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


def _default_backtest_reader() -> BacktestReader:
    return PostgresBacktestReader(database_url=settings.database_url)


def build_mcp_server(
    *,
    repository: StrategyRunReadRepository | None = None,
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
    active_backtest = backtest_reader or _default_backtest_reader()
    active_modules = issuer_module_reader or _default_issuer_module_reader()

    # --- Domain 1: panel (3) ---

    @server.tool(
        name="panel_universes",
        description="Read governed universe ruleset heads, history, and constituent members.",
    )
    def panel_universes() -> UniverseOverviewResponse:
        return load_universes_panel()

    @server.tool(
        name="panel_coverage",
        description="Read wide-panel input coverage, exclusions, and metric-forest node values at the governed head.",
    )
    def panel_coverage(
        request: PanelCoverageToolRequest | None = None,
    ) -> PanelCoverageResponse | MetricNodeValueReport | MetricNodeValueUnavailable:
        req = request or PanelCoverageToolRequest()
        if req.node_key is not None and metric_node_repository is not None:
            return metric_node_values(
                node_key=req.node_key,
                issuer_id=req.issuer_id,
                limit=req.limit,
                metric_node_repository=active_metric_node,
            )
        if req.node_key is not None and req.universe is None and req.cutoff is None:
            return metric_node_values(
                node_key=req.node_key,
                issuer_id=req.issuer_id,
                limit=req.limit,
                metric_node_repository=active_metric_node,
            )
        return load_wide_panel_coverage(
            resolve_mcp_access_context(),
            strategy_id=req.strategy_id,
            universe=req.universe,
            cutoff=req.cutoff,
            node_key=req.node_key,
            issuer_id=req.issuer_id,
            limit=req.limit,
            metric_node_repository=metric_node_repository,
        )

    @server.tool(
        name="panel_macro",
        description="Read U.S. Treasury par yield curve freshness and 12-month macro series history.",
    )
    def panel_macro() -> MacroPanelResponse:
        return load_macro_curve()

    # --- Domain 2: strategy (3) ---

    @server.tool(
        name="strategy_rankings",
        description="Read the latest governed strategy rankings and definition review_state from mart.",
    )
    def strategy_rankings(request: StrategyRankingsToolRequest) -> StrategyRunReport | StrategyRunUnavailable:
        return call_strategy_repository(
            active_repository,
            strategy_id=request.strategy_id,
            context=resolve_mcp_access_context(),
            universe=request.universe,
            convention=request.convention,
        )

    @server.tool(
        name="strategy_backtests",
        description="List governed backtest execution runs and performance summaries from mart.",
    )
    def strategy_backtests(request: StrategyBacktestsToolRequest) -> dict[str, Any]:
        return active_backtest.get_backtest_report(strategy_key=request.strategy_key, limit=request.limit)

    @server.tool(
        name="strategy_backtest_detail",
        description="Read a governed backtest run's valuations, trades, and stored limitations from mart.",
    )
    def strategy_backtest_detail(request: StrategyBacktestDetailToolRequest) -> dict[str, Any]:
        return active_backtest.get_backtest_report(run_id=request.run_id)

    @server.tool(
        name="strategy_backtest_trigger",
        description="Queue a governed nightly backtest pipeline execution via staging.pipeline_trigger_requests.",
    )
    def strategy_backtest_trigger(request: StrategyBacktestTriggerToolRequest) -> TriggerBacktestResponse:
        _require_mcp_admin()
        return trigger_backtest_run(
            MCP_OWNER_PRINCIPAL_ID,
            TriggerBacktestRequest(
                definition_resource=request.definition_resource,
                start_cutoff=request.start_cutoff,
                end_cutoff=request.end_cutoff,
                executed_at=request.executed_at,
            ),
        )

    # --- Domain 3: entities (6) ---

    @server.tool(
        name="entities_holdings",
        description="Read an ETF virtual-company consolidation profile and holdings valuation from mart.",
    )
    def entities_holdings(request: EntitiesHoldingsToolRequest) -> dict[str, Any]:
        if etf_profile_reader is not None:
            return etf_profile_reader.get_etf_profile(fund_id=request.fund_id)
        return get_etf_profile_payload(fund_id=request.fund_id)

    @server.tool(
        name="entities_themes",
        description="Read the theme purity leaderboard ranking the purest issuers under a thematic definition.",
    )
    def entities_themes(request: EntitiesThemesToolRequest) -> dict[str, Any]:
        if theme_purity_reader is not None:
            return theme_purity_reader.get_leaderboard(theme_id=request.theme_id, limit=request.limit)
        return get_theme_purity_leaderboard(theme_id=request.theme_id, limit=request.limit)

    @server.tool(
        name="entities_trace",
        description="Resolve a strategy decision output to its mart.metric_node_values and source lineage.",
    )
    def entities_trace(request: EntitiesTraceToolRequest) -> TraceResponse:
        return get_trace(resolve_mcp_access_context(), issuer_id=request.issuer_id, cutoff=request.cutoff)

    @server.tool(
        name="entities_supply_chain",
        description="Read module 3 supply-chain exposure of an issuer from the governed head in mart.served_head.",
    )
    def entities_supply_chain(
        request: EntitiesIssuerModuleToolRequest,
    ) -> SupplyChainExposure | IssuerModuleUnavailable:
        return _handle_issuer_module(
            active_modules.supply_chain_exposure,
            SupplyChainExposure,
            "supply_chain_exposure",
            request.issuer_id,
            "entities_supply_chain",
        )

    @server.tool(
        name="entities_analyst_ratings",
        description="Read module 4 analyst consensus of an issuer from the governed head in mart.served_head.",
    )
    def entities_analyst_ratings(request: EntitiesIssuerModuleToolRequest) -> AnalystRatings | IssuerModuleUnavailable:
        return _handle_issuer_module(
            active_modules.analyst_ratings,
            AnalystRatings,
            "analyst_ratings",
            request.issuer_id,
            "entities_analyst_ratings",
        )

    @server.tool(
        name="entities_detail",
        description="Read an issuer's 360 profile across GPPE, valuation tier, valuation gap, PEG, and theme purity.",
    )
    def entities_detail(request: EntitiesDetailToolRequest) -> dict[str, Any]:
        if company_profile_reader is not None:
            return company_profile_reader.get_company_profile(issuer_id=request.issuer_id)
        return get_company_profile_payload(issuer_id=request.issuer_id)

    # --- Domain 4: library (5) ---

    @server.tool(
        name="library_report",
        description="Assemble a deterministic research report (JSON or Markdown) over mart outputs.",
    )
    def library_report(request: LibraryReportToolRequest) -> LibraryReportResponse:
        return build_library_report(
            resolve_mcp_access_context(),
            report_kind=request.report_kind,
            target_entity_ids=request.target_entity_ids,
            cutoff_at=request.cutoff_at,
            section_kinds=request.section_kinds,
            strategy_id=request.strategy_id,
            title=request.title,
            output_format=request.format,
            research_repo=active_research,
        )

    @server.tool(
        name="library_card_export",
        description="Render a versioned 1080x1440 research card via the unified OpenAPI/MCP card service.",
    )
    def library_card_export(request: LibraryCardExportToolRequest) -> ResearchCard:
        report_req = _card_report_request(request)
        exported = export_or_render_card(
            resolve_mcp_access_context(),
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
        name="library_documents_list",
        description="List owner-scoped research documents and archived cards from app.research_documents.",
    )
    def library_documents_list(request: LibraryDocumentsListToolRequest | None = None) -> DocumentListResponse:
        req = request or LibraryDocumentsListToolRequest()
        return list_documents(
            resolve_mcp_access_context(),
            limit=req.limit,
            before_created_at=req.before_created_at,
            before_document_id=req.before_document_id,
        )

    @server.tool(
        name="library_document_create",
        description="Persist an owner-scoped research document revision in app.research_documents and S3.",
    )
    def library_document_create(request: LibraryDocumentCreateToolRequest) -> CreateDocumentResponse:
        return create_document(
            resolve_mcp_access_context(),
            source_artifact_id=request.source_artifact_id,
            content_bytes=request.content.encode("utf-8"),
            content_type=request.content_type,
        )

    @server.tool(
        name="library_document_detail",
        description="Read an owner-scoped research document's revisions and latest content.",
    )
    def library_document_detail(request: LibraryDocumentDetailToolRequest) -> DocumentDetailResponse | None:
        return get_document_detail(resolve_mcp_access_context(), document_id=request.document_id)

    @server.tool(
        name="admin_ops_snapshot",
        description="Read operational Dagster run status, dependency health, served heads, and nightly verdicts.",
    )
    def admin_ops_snapshot() -> OpsOverviewResponse:
        _require_mcp_admin()
        return get_ops_overview()

    @server.tool(
        name="admin_datahub_facts",
        description="Read full DataHub telemetry, oracle outcomes, quotas, invariants, and per-run deltas.",
    )
    def admin_datahub_facts() -> AdminDatahubFactsResponse:
        _require_mcp_admin()
        stats = get_datahub_stats()
        return AdminDatahubFactsResponse(facts=stats, diff=build_datahub_fact_diff(stats))

    return server
