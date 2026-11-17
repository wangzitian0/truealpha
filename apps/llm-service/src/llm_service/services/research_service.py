"""Unified research, mart, XHS card, and owner-scoped document service."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from infra2_sdk.runtime.s3 import create_s3_client, put_object_bytes, read_object_bytes
from pydantic import BaseModel, Field
from truealpha_contracts.common import require_stable_and_immutable
from truealpha_contracts.environment_contract import UnknownUniverse, load_environment_contract
from truealpha_contracts.metrics import AliasKind, is_registered_input_key
from truealpha_contracts.strategy import ExclusionReason
from truealpha_contracts.universes import id_prefix_like, universe_with_role
from truealpha_runtime.access import AccessContext
from truealpha_runtime.issuer_modules_mart import (
    AnalystRatings,
    PostgresIssuerModuleReader,
    SupplyChainExposure,
    issuer_identity_sql,
    resolve_static_entity_display,
)
from truealpha_runtime.research_cards import (
    XHS_CARD_HEIGHT_PX,
    XHS_CARD_WIDTH_PX,
    CardKind,
    build_card,
    build_strategy_context_from_report,
    enrich_card_metadata,
    paginate_card_series,
    render_card_html,
    render_card_json,
    verify_card_html,
)
from truealpha_runtime.research_report import (
    ReportSectionKind,
    ResearchReadPort,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
    render_report_markdown,
)
from truealpha_runtime.research_report_mart import (
    ACCEPTANCE_FALLBACK_HEAD_SQL as _ACCEPTANCE_FALLBACK_HEAD_SQL,
)
from truealpha_runtime.research_report_mart import (
    POINTER_HEAD_SQL,
    MartResearchReadRepository,
    MetricNodeValueReport,
    MetricNodeValueUnavailable,
    PostgresMetricNodeValueRepository,
)
from truealpha_runtime.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_runtime.strategy_run_postgres import (
    PostgresStrategyRunRepository,
    read_strategy_provenance,
    read_trace_lineage,
)

from llm_service.config import settings
from llm_service.services._db import with_mart_readonly, with_owner_scoped_runtime

DASHBOARD_STRATEGY_ID = "large_model_value_v0"
CARD_EXPORT_CACHE_CONTROL = "public, max-age=300"

SERVED_HEAD_SQL = "select run_id, freshness, availability from mart.served_head"


logger = logging.getLogger(__name__)


def served_run_predicate(column: str) -> str:
    return f"{column} in (select h.run_id from mart.served_head h where h.run_id is not null)"


def served_or_empty_head_predicate(column: str, universe_prefix: str | None = None) -> str:
    """Require a governed `mart.served_head` match whenever heads exist for `universe_prefix`
    (or any universe when None), failing closed (0 rows, no stale fallback) when `unavailable`."""
    if universe_prefix:
        return (
            f"({column} in (select h.run_id from mart.served_head h where starts_with(h.universe_id, '{universe_prefix}') and h.run_id is not null)"
            f" or not exists (select 1 from mart.served_head h where starts_with(h.universe_id, '{universe_prefix}')))"
        )
    return (
        f"({column} in (select h.run_id from mart.served_head h where h.run_id is not null)"
        f" or not exists (select 1 from mart.served_head))"
    )


def resolve_governed_head_run_id(cur: Any, universe_like: str) -> tuple[str | None, str | None]:
    """Resolve the governed served run id for `universe_like`, failing closed when unavailable."""
    cur.execute(POINTER_HEAD_SQL, (universe_like,))
    row = cur.fetchone()
    if row is not None:
        if row.get("availability") == "unavailable" or row.get("run_id") is None:
            reason = str(row.get("staleness_reason") or "").strip() or "older_than_30d"
            return None, f"head_unavailable: {reason}"
        return str(row["run_id"]), None
    cur.execute(_ACCEPTANCE_FALLBACK_HEAD_SQL, (universe_like,))
    fallback = cur.fetchone()
    if fallback is None or fallback.get("run_id") is None:
        return None, "no accepted (quality-reported) strategy run"
    return str(fallback["run_id"]), None


def _id_prefix_like(role: str) -> str:
    return id_prefix_like(universe_with_role(role))  # type: ignore[arg-type]


def normalize_peg_convention(value: str | None) -> str:
    if value in {"consensus", "analyst_consensus"}:
        return "consensus"
    if value in {"guidance", "company_guidance"}:
        return "guidance"
    return "historical_cagr"


def project_peg_by_convention(
    *, peg: str | None = None, peg_rank: int | None = None, convention: str = "historical_cagr"
) -> tuple[str | None, int | None, list[str]]:
    norm = normalize_peg_convention(convention)
    if norm == "historical_cagr":
        return peg, peg_rank, []
    return None, None, [f"growth_convention_unsourced:{norm}"]


def normalize_ranking_universe(value: str | None) -> str | None:
    clean = (value or "").strip().lower()
    if not clean:
        return universe_with_role("strategy")
    if clean == "all":
        return "all"
    contract = load_environment_contract()
    return clean if clean in contract.universes else None


def matches_ranking_universe(universe_id: str | None, universe: str) -> bool:
    if universe == "all":
        return True
    if not universe_id:
        return False
    try:
        return load_environment_contract().universe_of_id(universe_id) == universe
    except UnknownUniverse:
        return False


# --- Identifier / pagination validation (shared with documents) ---------------------------

_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def assert_non_empty_text(value: str, label: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{label} must be a non-empty string")
    return trimmed


def assert_stable_id(value: str, label: str) -> str:
    trimmed = assert_non_empty_text(value, label)
    return require_stable_and_immutable(trimmed, label)


def clamp_list_limit(limit: int | None) -> int:
    if limit is None:
        return 50
    return max(1, min(limit, 200))


def parse_before_cursor(created_at: str | None, document_id: str | None) -> tuple[str, str] | None:
    if created_at is None and document_id is None:
        return None
    if not created_at or not document_id or not _ISO_TIMESTAMP_RE.match(created_at):
        raise ValueError("before cursor must supply valid UTC created_at and document_id")
    datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return created_at, assert_stable_id(document_id, "before.document_id")


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


# --- Entity display & wide panel constants -------------------------------------------------

ENTITY_DISPLAY_SQL = """
  select r.issuer_id,
         coalesce(nullif(btrim(r.ticker), ''), nullif(btrim(ei.current_ticker), '')) as ticker,
         coalesce(nullif(btrim(r.display_name), ''), nullif(btrim(ei.name), '')) as display_name,
         ei.legacy_id
  from mart.entity_display_resolution r
  left join (
    select distinct on (e.entity_id::text)
           e.entity_id::text as issuer_id, e.current_ticker, e.name, e.legacy_id
    from mart.entity_identity e
    order by e.entity_id::text, e.legacy_id
  ) ei on ei.issuer_id = r.issuer_id
"""

WIDE_PANEL_INPUT_KEYS: tuple[str, ...] = (
    "gross_profit",
    "pre_provision_profit",
    "revenue_total",
    "total_assets",
    "stockholders_equity",
    "tangible_common_equity",
    "employees_total",
    "last_close",
    "shares_outstanding",
)
GPPE_VARIANT_KEYS: tuple[str, ...] = (
    "gppe_uniform_charge_v0",
    "gppe_banking_tce_v1",
    "gppe_operating_financial_v1",
)
RANKING_PANEL_INPUT_KEYS: tuple[str, ...] = (
    "operating_gross_profit",
    "capital_adjusted_gross_profit",
    "tangible_common_equity",
    "total_gross_return",
    "employees_total",
)

MODULE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "module": 1,
        "name": "PEG",
        "note": "recency-weighted historical growth",
        "gate": "Gate 2",
        "field": "peg",
        "output_table": None,
    },
    {
        "module": 2,
        "name": "Labor efficiency",
        "note": "capital-adjusted labor efficiency, named per issuer class (#1176)",
        "gate": "Gate 1",
        "field": "capital_adjusted_labor_efficiency",
        "output_table": None,
    },
    {
        "module": 3,
        "name": "Supply-chain graph",
        "note": "confidence-gated scenario exposure",
        "gate": "Gate 2",
        "field": None,
        "output_table": "mart.issuer_supply_chain_exposure",
    },
    {
        "module": 4,
        "name": "Analyst backtesting",
        "note": "PIT event eligibility and outcomes",
        "gate": "Gate 2",
        "field": None,
        "output_table": "mart.issuer_analyst_ratings",
    },
    {
        "module": 5,
        "name": "ETF virtual company",
        "note": "delayed N-PORT holdings",
        "gate": "Gate 2",
        "field": None,
        "output_table": "mart.fund_virtual_company",
    },
    {
        "module": 6,
        "name": "Pure-blood screening",
        "note": "traceable segment classification",
        "gate": "Gate 2",
        "field": None,
        "output_table": "mart.issuer_theme_purity",
    },
    {
        "module": 7,
        "name": "Three-tier valuation",
        "note": "materialized composite factor",
        "gate": "Gate 1",
        "field": "tier",
        "output_table": None,
    },
)

THEME_LATEST_RUN_SQL = """
  select p.run_id, max(p.cutoff) as cutoff
  from mart.issuer_theme_purity p
  where p.partition_id is not null
    and not exists (
      select 1 from mart.served_head h
      where h.head_run_id = p.run_id and h.availability = 'unavailable'
    )
  group by run_id
  order by max(cutoff) desc
  limit 1
"""

THEME_ROWS_SQL = """
  select theme_id, theme, definition_version, definition_sha256,
         to_char(cutoff, 'YYYY-MM-DD"T"HH24:MI:SSOF') as cutoff,
         issuer_id, theme_share::text as theme_share,
         consolidated_revenue::text as consolidated_revenue,
         in_theme_revenue::text as in_theme_revenue,
         out_of_theme_revenue::text as out_of_theme_revenue,
         unclassified_revenue::text as unclassified_revenue,
         partition_residual::text as partition_residual,
         segments, confidence::text as confidence, reason_codes, extractor,
         availability_status, source_evidence_status, factor_validation_status,
         to_char(period_end, 'YYYY-MM-DD') as period_end
  from mart.issuer_theme_purity
  where run_id = %s and partition_id is not null
  order by theme_id, theme_share desc nulls last, issuer_id
"""


# --- OpenAPI Pydantic DTOs ----------------------------------------------------------------


class EntityDisplayItem(BaseModel):
    issuer_id: str
    ticker: str
    display_name: str | None = None
    label: str


class EntityDisplayMapResponse(BaseModel):
    items: dict[str, EntityDisplayItem] = Field(default_factory=dict)


class WidePanelCellDto(BaseModel):
    node_key: str
    period: str | None = None
    value: str | None = None
    confidence: str | None = None
    availability: str = "unavailable"


class ModuleOutputCountDto(BaseModel):
    table: str
    run_id: str | None = None
    rows: int = 0
    available_rows: int = 0
    latest_cutoff: str | None = None


class ModuleOverviewRowDto(BaseModel):
    module: int
    name: str
    note: str
    gate: str
    availability: str
    with_value: int | None = None
    total: int | None = None
    source: str
    output: ModuleOutputCountDto | None = None


class OverviewResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    strategy_run_id: str | None = None
    executed_at: str | None = None
    source: str = "mart"
    corpus_prefix: str = ""
    latest_cutoff: str | None = None
    modules: list[ModuleOverviewRowDto] = Field(default_factory=list)


class DecisionProvenanceDto(BaseModel):
    operating_period_end: str | None = None
    revenue_period_end: str | None = None
    shares_period_end: str | None = None
    universe_id: str | None = None
    universe_version: str | None = None
    universe_sha256: str | None = None
    gppe_definition_sha256: str | None = None
    tier_definition_sha256: str | None = None


class StrategyDecisionItemDto(BaseModel):
    issuer_id: str
    label: str
    cutoff_at: str
    outcome: str
    eligible: bool
    tier: str | None = None
    capital_adjusted_labor_efficiency: str | None = None
    labor_efficiency_metric: str | None = None
    current_price_to_sales: str | None = None
    target_price_to_sales: str | None = None
    current_price_to_gross_profit: str | None = None
    target_price_to_gross_profit: str | None = None
    valuation_basis: str | None = None
    current_multiple: str | None = None
    target_multiple: str | None = None
    valuation_gap: str | None = None
    confidence: str | None = None
    exclusion_reason: str | None = None
    exclusion_label: str | None = None
    rank: int | None = None
    target_weight: str | None = None
    peg: str | None = None
    peg_rank: int | None = None
    availability: str = "unavailable"
    trace_id: str = ""
    gppe_variants: dict[str, WidePanelCellDto | None] = Field(default_factory=dict)
    panel_inputs: dict[str, WidePanelCellDto | None] = Field(default_factory=dict)
    provenance: DecisionProvenanceDto = Field(default_factory=DecisionProvenanceDto)


class StrategyRunPageResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    strategy_id: str
    strategy_run_id: str | None = None
    executed_at: str | None = None
    source: str = "mart"
    corpus_sha256: str = ""
    definition_version: str | None = None
    review_state: str | None = None
    cutoff_at: str | None = None
    universe: str = "all"
    total_decisions: int = 0
    decisions: list[StrategyDecisionItemDto] = Field(default_factory=list)
    wide_panel: dict[str, dict[str, WidePanelCellDto]] = Field(default_factory=dict)
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)


class RankingsResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    cutoff_at: str | None = None
    universe: str = "all"
    total: int = 0
    rows: list[StrategyDecisionItemDto] = Field(default_factory=list)
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)


class TraceNodeValueDto(BaseModel):
    node_key: str
    period: str | None = None
    value: str | None = None
    unit: str | None = None
    confidence: str | None = None
    availability_status: str = "unavailable"
    reason_codes: list[str] = Field(default_factory=list)


class TraceResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    trace_id: str = ""
    strategy_id: str = DASHBOARD_STRATEGY_ID
    issuer_id: str = ""
    label: str = ""
    cutoff_at: str = ""
    corpus_sha256: str = ""
    lineage_result_id: str | None = None
    lineage_inputs: list[dict[str, Any]] = Field(default_factory=list)
    decision: StrategyDecisionItemDto | None = None
    node_values: list[TraceNodeValueDto] = Field(default_factory=list)


class ThemePurityRowDto(BaseModel):
    issuer_id: str
    label: str
    theme_share: str | None = None
    consolidated_revenue: str
    in_theme_revenue: str
    out_of_theme_revenue: str
    unclassified_revenue: str
    partition_residual: str
    segments: int | None = None
    confidence: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    extractor: str
    availability_status: str
    source_evidence_status: str
    factor_validation_status: str
    period_end: str


class ThemePurityGroupDto(BaseModel):
    theme_id: str
    theme: str
    definition_version: str
    definition_sha256: str
    cutoff: str
    run_id: str
    rows: list[ThemePurityRowDto] = Field(default_factory=list)


class ThemesResponse(BaseModel):
    groups: list[ThemePurityGroupDto] = Field(default_factory=list)


class ValuedHoldingRowDto(BaseModel):
    holding_name: str
    ticker: str | None = None
    weight_pct: str | None = None
    current_ps: str | None = None
    target_ps_midpoint: str | None = None
    current_pgp: str | None = None
    target_pgp_midpoint: str | None = None
    valuation_gap: str | None = None
    tier: str | None = None
    availability: str | None = None


class FundValuationDto(BaseModel):
    fund_id: str
    fund_name: str
    report_period: str
    run_id: str | None = None
    valued_weight_pct: str = "0.00"
    resolved_weight_pct: str = "0.00"
    total_weight_pct: str = "0.00"
    weighted_gap: str | None = None
    weighted_gap_basis: str | None = None
    availability_status: str | None = None
    source_evidence_status: str | None = None
    factor_validation_status: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    lines: list[ValuedHoldingRowDto] = Field(default_factory=list)


class FundValuationsResponse(BaseModel):
    funds: list[FundValuationDto] = Field(default_factory=list)


class BacktestRunDto(BaseModel):
    run_id: str
    strategy_key: str
    strategy_version: str
    universe_id: str
    start_date: str
    end_date: str
    status: str
    cagr_monthly: float | None = None
    sharpe_daily: float | None = None
    max_dd_daily: float | None = None
    vol_daily: float | None = None
    turnover_monthly: float | None = None
    calmar_daily: float | None = None
    metrics_payload: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    executed_at: str
    created_at: str
    definition_content_sha256: str | None = None
    price_basis: str | None = None
    claim_ceiling: str | None = None
    benchmark_rule: str | None = None
    benchmark_cagr: float | None = None
    benchmark_max_dd: float | None = None


class BacktestValuationDto(BaseModel):
    run_id: str
    resolution: str
    valuation_date: str
    cum_nav: float
    drawdown: float
    gross_exposure: float
    cash_weight: float
    benchmark_cum_nav: float | None = None


class BacktestTradeDto(BaseModel):
    trade_id: int
    run_id: str
    trade_date: str
    symbol: str
    side: str
    shares: float
    execution_price: float
    trade_value: float
    weight_before: float
    weight_after: float
    fee_paid: float


class BacktestRunsResponse(BaseModel):
    runs: list[BacktestRunDto] = Field(default_factory=list)


class BacktestDetailResponse(BaseModel):
    run: BacktestRunDto
    valuations: list[BacktestValuationDto] = Field(default_factory=list)
    trades: list[BacktestTradeDto] = Field(default_factory=list)


class EntityDetailResponse(BaseModel):
    issuer_id: str
    label: str
    decisions: list[StrategyDecisionItemDto] = Field(default_factory=list)
    node_values: list[TraceNodeValueDto] = Field(default_factory=list)
    themes: list[dict[str, Any]] = Field(default_factory=list)
    analyst_ratings: dict[str, Any] | None = None
    supply_chain_exposure: dict[str, Any] | None = None
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)


class DocumentDto(BaseModel):
    document_id: str
    tenant_id: str
    owner_principal_id: str
    created_at: str


class DocumentRevisionDto(BaseModel):
    revision_id: str
    document_id: str
    tenant_id: str
    owner_principal_id: str
    source_artifact_id: str
    artifact_sha256: str
    artifact_byte_length: int
    artifact_content_type: str
    created_at: str


class DocumentCursorDto(BaseModel):
    created_at: str
    document_id: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentDto] = Field(default_factory=list)
    next_before: DocumentCursorDto | None = None


class CreateDocumentRequest(BaseModel):
    source_artifact_id: str
    content_type: str = "text/plain; charset=utf-8"
    content: str


class DocumentDetailResponse(BaseModel):
    document: DocumentDto
    revisions: list[DocumentRevisionDto] = Field(default_factory=list)
    latest_content: str | None = None


class CreateDocumentResponse(BaseModel):
    document: DocumentDto
    revision: DocumentRevisionDto


class DeleteDocumentResponse(BaseModel):
    tombstone_id: str
    document_id: str
    created_at: str


class CardExportRequest(BaseModel):
    card_kind: CardKind = CardKind.RANKING
    universe: str | None = "all"
    issuer_id: str | None = None
    cutoff: str | None = None
    convention: str = "normalized"
    preset: str | None = "study-notes"
    page: int | None = None
    title: str | None = None
    format: Literal["html", "json"] = "html"
    persist: bool = True


class CardExportResponse(BaseModel):
    card_id: str
    card_kind: str
    render_mode: str
    document_id: str | None = None
    rendered_html: str | None = None
    card: dict[str, Any]


# --- Formatting & exclusion labels --------------------------------------------------------

_EXCLUSION_LABELS: dict[str, str] = {
    reason.value: reason.value.replace("_", " ").capitalize() for reason in ExclusionReason
}


def _display_label(issuer_id: str, display_map: dict[str, EntityDisplayItem]) -> str:
    if item := display_map.get(issuer_id):
        return item.label
    return resolve_static_entity_display(issuer_id)[2]


def load_entity_display_items(*, database_url: str | None = None) -> dict[str, EntityDisplayItem]:
    def _q(cur: Any) -> dict[str, EntityDisplayItem]:
        cur.execute(ENTITY_DISPLAY_SQL)
        out: dict[str, EntityDisplayItem] = {}
        for row in cur.fetchall():
            issuer_id = str(row["issuer_id"])
            ticker = str(row.get("ticker") or "").strip()
            display_name = str(row.get("display_name") or "").strip() or None
            if ticker and display_name:
                label = f"{ticker} · {display_name}"
            else:
                label = ticker or display_name or issuer_id
            item = EntityDisplayItem(issuer_id=issuer_id, ticker=ticker, display_name=display_name, label=label)
            out[issuer_id] = item
            legacy_id = row.get("legacy_id")
            if isinstance(legacy_id, str) and legacy_id and legacy_id not in out:
                out[legacy_id] = item
        return out

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return {}


def _load_wide_panel(
    node_keys: tuple[str, ...], *, database_url: str | None = None
) -> dict[str, dict[str, WidePanelCellDto]]:
    sql = f"""
      select coalesce(ei.legacy_id, v.issuer_id) as issuer_id,
             v.node_key, v.period, v.value::text as value,
             v.confidence::text as confidence,
             coalesce(v.availability_status, 'unavailable') as availability_status
      from mart.metric_node_values v
      left join lateral (
        select ei.legacy_id from mart.entity_identity ei
        where ei.entity_id::text = v.issuer_id order by ei.legacy_id limit 1
      ) ei on true
      where {served_run_predicate("v.run_id")}
        and v.node_key = any(%s::text[])
      order by coalesce(ei.legacy_id, v.issuer_id), v.node_key, v.period desc
    """

    def _q(cur: Any) -> dict[str, dict[str, WidePanelCellDto]]:
        cur.execute(sql, (list(node_keys),))
        by_issuer: dict[str, dict[str, WidePanelCellDto]] = {}
        for row in cur.fetchall():
            iid = row.get("issuer_id")
            nkey = row.get("node_key")
            if not isinstance(iid, str) or not isinstance(nkey, str):
                continue
            bucket = by_issuer.setdefault(iid, {})
            if nkey in bucket:
                continue
            bucket[nkey] = WidePanelCellDto(
                node_key=nkey,
                period=row.get("period"),
                value=row.get("value"),
                confidence=row.get("confidence"),
                availability=str(row.get("availability_status") or "unavailable"),
            )
        return by_issuer

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return {}


def _decision_availability(decision: Any) -> str:
    if str(getattr(decision, "availability_status", None) or "") == "not_applicable":
        return "not_applicable"
    if decision.outcome.value == "excluded":
        return "excluded"
    if (
        decision.tier is not None
        or decision.current_price_to_sales is not None
        or decision.current_price_to_gross_profit is not None
        or decision.capital_adjusted_labor_efficiency is not None
    ):
        return "available"
    return "unavailable"


def _decision_to_dto(
    decision: Any,
    *,
    source: str,
    corpus_sha256: str,
    display_map: dict[str, EntityDisplayItem],
    provenance_map: dict[str, dict[str, Any]],
    wide_panel: dict[str, dict[str, WidePanelCellDto]],
) -> StrategyDecisionItemDto:
    iid = decision.issuer_id
    prov_raw = provenance_map.get(iid, {})
    prov = DecisionProvenanceDto(
        operating_period_end=prov_raw.get("operating_period_end"),
        revenue_period_end=prov_raw.get("revenue_period_end"),
        shares_period_end=prov_raw.get("shares_period_end"),
        universe_id=prov_raw.get("universe_id"),
        universe_version=prov_raw.get("universe_version"),
        universe_sha256=prov_raw.get("universe_sha256"),
        gppe_definition_sha256=prov_raw.get("gppe_definition_sha256"),
        tier_definition_sha256=prov_raw.get("tier_definition_sha256"),
    )
    panel = wide_panel.get(iid, {})
    avail = _decision_availability(decision)
    excl = decision.exclusion_reason.value if hasattr(decision.exclusion_reason, "value") else decision.exclusion_reason
    cur_pgp = getattr(decision, "current_price_to_gross_profit", None)
    tgt_pgp = getattr(decision, "target_price_to_gross_profit", None)
    basis = (
        "price_to_gross_profit"
        if cur_pgp is not None or tgt_pgp is not None
        else (
            "price_to_sales"
            if decision.current_price_to_sales is not None or decision.target_price_to_sales is not None
            else None
        )
    )
    cur_mult = cur_pgp if basis == "price_to_gross_profit" else decision.current_price_to_sales
    tgt_mult = tgt_pgp if basis == "price_to_gross_profit" else decision.target_price_to_sales
    trace_payload = f"{source}:{corpus_sha256[:12]}:{iid}:{decision.cutoff_at.isoformat()}"
    trace_id = f"trace:{hashlib.sha256(trace_payload.encode()).hexdigest()[:16]}"
    active_metric = decision.labor_efficiency_metric or "gppe_uniform_charge_v0"
    gppe_variants: dict[str, WidePanelCellDto | None] = {k: panel.get(k) for k in GPPE_VARIANT_KEYS}
    if (
        active_metric in GPPE_VARIANT_KEYS
        and gppe_variants.get(active_metric) is None
        and decision.capital_adjusted_labor_efficiency is not None
    ):
        gppe_variants[active_metric] = WidePanelCellDto(
            node_key=active_metric,
            period=prov.operating_period_end,
            value=str(decision.capital_adjusted_labor_efficiency),
            confidence=str(decision.confidence) if decision.confidence is not None else None,
            availability=avail,
        )
    panel_inputs: dict[str, WidePanelCellDto | None] = {k: panel.get(k) for k in RANKING_PANEL_INPUT_KEYS}
    return StrategyDecisionItemDto(
        issuer_id=iid,
        label=_display_label(iid, display_map),
        cutoff_at=decision.cutoff_at.isoformat(),
        outcome=decision.outcome.value,
        eligible=decision.eligible,
        tier=decision.tier,
        capital_adjusted_labor_efficiency=(
            str(decision.capital_adjusted_labor_efficiency)
            if decision.capital_adjusted_labor_efficiency is not None
            else None
        ),
        labor_efficiency_metric=decision.labor_efficiency_metric,
        current_price_to_sales=str(decision.current_price_to_sales)
        if decision.current_price_to_sales is not None
        else None,
        target_price_to_sales=str(decision.target_price_to_sales)
        if decision.target_price_to_sales is not None
        else None,
        current_price_to_gross_profit=str(cur_pgp) if cur_pgp is not None else None,
        target_price_to_gross_profit=str(tgt_pgp) if tgt_pgp is not None else None,
        valuation_basis=basis,
        current_multiple=str(cur_mult) if cur_mult is not None else None,
        target_multiple=str(tgt_mult) if tgt_mult is not None else None,
        valuation_gap=str(decision.valuation_gap) if decision.valuation_gap is not None else None,
        confidence=str(decision.confidence) if decision.confidence is not None else None,
        exclusion_reason=excl,
        exclusion_label=_EXCLUSION_LABELS.get(excl) if excl else None,
        rank=decision.rank,
        target_weight=str(decision.target_weight) if decision.target_weight is not None else None,
        peg=str(decision.peg) if getattr(decision, "peg", None) is not None else None,
        peg_rank=getattr(decision, "peg_rank", None),
        availability=avail,
        trace_id=trace_id,
        gppe_variants=gppe_variants,
        panel_inputs=panel_inputs,
        provenance=prov,
    )


def _load_strategy_provenance(
    strategy_run_id: str | None, *, database_url: str | None = None
) -> dict[str, dict[str, Any]]:
    if not strategy_run_id:
        return {}
    try:
        return with_mart_readonly(lambda cur: read_strategy_provenance(cur, strategy_run_id), database_url=database_url)
    except psycopg.Error:
        return {}


def _load_strategy_report(
    context: AccessContext, *, strategy_id: str = DASHBOARD_STRATEGY_ID, database_url: str | None = None
) -> StrategyRunReport | StrategyRunUnavailable:
    repo = PostgresStrategyRunRepository(database_url=database_url or settings.database_url)
    return repo.get_latest(strategy_id=strategy_id, context=context)


def _module_output_counts(*, database_url: str | None = None) -> dict[str, ModuleOutputCountDto]:
    tables = [m["output_table"] for m in MODULE_CATALOG if m["output_table"] is not None]

    def _q(cur: Any) -> dict[str, ModuleOutputCountDto]:
        out: dict[str, ModuleOutputCountDto] = {}
        for table in tables:
            where = f"where {served_or_empty_head_predicate('run_id')}"
            sql = f"""
              with newest as (
                select run_id from {table} {where} group by run_id order by max(cutoff) desc limit 1
              )
              select (select run_id from newest) as run_id,
                     count(*)::int as row_count,
                     count(*) filter (where availability_status = 'available')::int as available_count,
                     to_char(max(cutoff) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as latest_cutoff
              from {table}
              where run_id = (select run_id from newest)
            """
            try:
                cur.execute(sql)
                row = cur.fetchone()
                if row:
                    out[table] = ModuleOutputCountDto(
                        table=table,
                        run_id=row.get("run_id"),
                        rows=int(row.get("row_count") or 0),
                        available_rows=int(row.get("available_count") or 0),
                        latest_cutoff=row.get("latest_cutoff"),
                    )
            except psycopg.Error:
                cur.connection.rollback()
        return out

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return {}


def get_overview(context: AccessContext, *, database_url: str | None = None) -> OverviewResponse:
    report = _load_strategy_report(context, database_url=database_url)
    if isinstance(report, StrategyRunUnavailable):
        return OverviewResponse(available=False, reason=report.reason)
    outputs = _module_output_counts(database_url=database_url)
    cutoffs = sorted((d.cutoff_at.isoformat() for d in report.decisions), reverse=True)
    modules: list[ModuleOverviewRowDto] = []
    for entry in MODULE_CATALOG:
        table = entry["output_table"]
        if table is not None:
            out_row = outputs.get(table)
            avail = "available" if out_row and out_row.available_rows > 0 else "unavailable"
            cov = (out_row.available_rows, out_row.rows) if out_row else None
            modules.append(
                ModuleOverviewRowDto(
                    module=entry["module"],
                    name=entry["name"],
                    note=entry["note"],
                    gate=entry["gate"],
                    availability=avail,
                    with_value=cov[0] if cov else None,
                    total=cov[1] if cov else None,
                    source=table,
                    output=out_row,
                )
            )
        else:
            field_name = entry["field"]
            with_val = sum(1 for d in report.decisions if getattr(d, field_name, None) is not None)
            avail = "available" if with_val > 0 else "unavailable"
            modules.append(
                ModuleOverviewRowDto(
                    module=entry["module"],
                    name=entry["name"],
                    note=entry["note"],
                    gate=entry["gate"],
                    availability=avail,
                    with_value=with_val,
                    total=len(report.decisions),
                    source="mart.strategy_decisions",
                )
            )
    return OverviewResponse(
        available=True,
        strategy_run_id=report.strategy_run_id,
        executed_at=None,
        source=report.source,
        corpus_prefix=report.corpus_sha256[:12],
        latest_cutoff=cutoffs[0] if cutoffs else None,
        modules=modules,
    )


def get_strategy_run_page(
    context: AccessContext,
    *,
    strategy_id: str = DASHBOARD_STRATEGY_ID,
    universe: str | None = None,
    cutoff: str | None = None,
    database_url: str | None = None,
) -> StrategyRunPageResponse:
    norm_universe = "all" if universe is None else (normalize_ranking_universe(universe) or "all")
    report = _load_strategy_report(context, strategy_id=strategy_id, database_url=database_url)
    if isinstance(report, StrategyRunUnavailable):
        return StrategyRunPageResponse(
            available=False, reason=report.reason, strategy_id=strategy_id, universe=norm_universe
        )
    display_map = load_entity_display_items(database_url=database_url)
    prov_map = _load_strategy_provenance(report.strategy_run_id, database_url=database_url)
    wide_panel = _load_wide_panel(
        WIDE_PANEL_INPUT_KEYS + GPPE_VARIANT_KEYS + RANKING_PANEL_INPUT_KEYS, database_url=database_url
    )
    target_cutoff = cutoff or max((d.cutoff_at.isoformat() for d in report.decisions), default=None)
    filtered = [
        d
        for d in report.decisions
        if (target_cutoff is None or d.cutoff_at.isoformat() == target_cutoff)
        and matches_ranking_universe(prov_map.get(d.issuer_id, {}).get("universe_id"), norm_universe)
    ]
    ordered = sorted(filtered, key=lambda d: (0, d.rank, d.issuer_id) if d.rank is not None else (1, 0, d.issuer_id))
    items = [
        _decision_to_dto(
            d,
            source=report.source,
            corpus_sha256=report.corpus_sha256,
            display_map=display_map,
            provenance_map=prov_map,
            wide_panel=wide_panel,
        )
        for d in ordered
    ]
    return StrategyRunPageResponse(
        available=True,
        strategy_id=report.strategy_id,
        strategy_run_id=report.strategy_run_id,
        executed_at=None,
        source=report.source,
        corpus_sha256=report.corpus_sha256,
        definition_version=report.definition.version if report.definition else None,
        review_state=report.definition.review_state if report.definition else None,
        cutoff_at=target_cutoff,
        universe=norm_universe,
        total_decisions=len(items),
        decisions=items,
        wide_panel=wide_panel,
        display_map=display_map,
    )


def get_rankings(
    context: AccessContext,
    *,
    universe: str | None = "all",
    cutoff: str | None = None,
    limit: int = 200,
    database_url: str | None = None,
) -> RankingsResponse:
    page = get_strategy_run_page(context, universe=universe, cutoff=cutoff, database_url=database_url)
    return RankingsResponse(
        available=page.available,
        reason=page.reason,
        cutoff_at=page.cutoff_at,
        universe=page.universe,
        total=page.total_decisions,
        rows=page.decisions[:limit],
        display_map=page.display_map,
    )


_TRACE_INPUT_KEYS = (
    ("gross_profit", "financial-fact", True),
    ("total_assets", "financial-fact", True),
    ("headcount", "financial-fact", False),
    ("revenue", "financial-fact", True),
    ("shares_outstanding", "financial-fact", False),
    ("close", "market-price", True),
)


def _extract_lineage_inputs(lineage: Any, listing_id: str | None) -> list[dict[str, Any]]:
    if not isinstance(lineage, list):
        return []
    carriers = [
        item
        for item in lineage
        if isinstance(item, dict)
        and item.get("semantic_type") in {"financial-fact", "market-price"}
        and (listing_id is None or (item.get("inputs") or {}).get("listing_id") == listing_id)
    ]
    out: list[dict[str, Any]] = []
    for key, stype, monetary in _TRACE_INPUT_KEYS:
        item = next((c for c in carriers if c.get("semantic_type") == stype), None)
        inputs = (item or {}).get("inputs") or {}
        filing = (((item or {}).get("vintage")) or {}).get(key) or {}
        raw_val = inputs.get(key)
        curr = inputs.get("currency") if monetary and raw_val is not None else None
        know = (
            filing.get("filed")
            or (filing.get("knowable_at") if key == "headcount" else None)
            or (item or {}).get("knowable_at")
        )
        src = (
            (filing.get("raw") or filing.get("evidence_ref"))
            if key == "headcount"
            else (item or {}).get("source_vintage_id")
        )
        out.append(
            {
                "key": key,
                "value": f"{raw_val} {curr}"
                if raw_val is not None and curr
                else (str(raw_val) if raw_val is not None else None),
                "period": filing.get("period_end"),
                "knowable_at": know,
                "accession": filing.get("accession"),
                "source": src,
            }
        )
    return out


def get_trace(
    context: AccessContext,
    *,
    issuer_id: str,
    cutoff: str | None = None,
    database_url: str | None = None,
) -> TraceResponse:
    page = get_strategy_run_page(context, cutoff=cutoff, database_url=database_url)
    if not page.available:
        return TraceResponse(available=False, reason=page.reason, issuer_id=issuer_id)
    decision = next(
        (d for d in page.decisions if d.issuer_id == issuer_id or issuer_id.lower() in d.label.lower()), None
    )
    target_iid = decision.issuer_id if decision else issuer_id
    node_sql = f"""
      select v.node_key, v.period, v.value::text as value, v.unit, v.confidence::text as confidence,
             coalesce(v.availability_status, 'unavailable') as availability_status, v.reason_codes
      from mart.metric_node_values v
      where {served_run_predicate("v.run_id")}
        and {issuer_identity_sql("v.issuer_id", "%s")}
      order by v.node_key, v.period desc
      limit 100
    """

    def _q(cur: Any) -> tuple[list[TraceNodeValueDto], str | None, list[dict[str, Any]]]:
        cur.execute(node_sql, (target_iid, target_iid, target_iid))
        nodes = [
            TraceNodeValueDto(
                node_key=str(r["node_key"]),
                period=r.get("period"),
                value=r.get("value"),
                unit=r.get("unit"),
                confidence=r.get("confidence"),
                availability_status=str(r.get("availability_status") or "unavailable"),
                reason_codes=list(r.get("reason_codes") or ()),
            )
            for r in cur.fetchall()
        ]
        res_id: str | None = None
        lin_inputs: list[dict[str, Any]] = []
        target_cutoff = decision.cutoff_at if decision else cutoff
        if page.strategy_run_id and target_cutoff:
            try:
                res_id, list_id, lineage = read_trace_lineage(cur, page.strategy_run_id, target_iid, target_cutoff)
                lin_inputs = _extract_lineage_inputs(lineage, list_id)
            except psycopg.Error:
                cur.connection.rollback()
        return nodes, res_id, lin_inputs

    nodes: list[TraceNodeValueDto] = []
    lineage_result_id: str | None = None
    lineage_inputs: list[dict[str, Any]] = []
    try:
        nodes, lineage_result_id, lineage_inputs = with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        nodes = []
    return TraceResponse(
        available=decision is not None or bool(nodes),
        reason=None if (decision or nodes) else "trace_not_found",
        trace_id=decision.trace_id if decision else "",
        issuer_id=target_iid,
        label=decision.label if decision else target_iid,
        cutoff_at=decision.cutoff_at if decision else (cutoff or ""),
        corpus_sha256=page.corpus_sha256,
        lineage_result_id=lineage_result_id,
        lineage_inputs=lineage_inputs,
        decision=decision,
        node_values=nodes,
    )


def get_entity_detail(
    context: AccessContext,
    *,
    issuer_id: str,
    database_url: str | None = None,
) -> EntityDetailResponse:
    page = get_strategy_run_page(context, database_url=database_url)
    decisions = [d for d in page.decisions if d.issuer_id == issuer_id]
    trace = get_trace(context, issuer_id=issuer_id, database_url=database_url)
    db_url = database_url or settings.database_url
    profile = PostgresCompanyProfileReader(database_url=db_url).get_company_profile(issuer_id=issuer_id)
    modules = PostgresIssuerModuleReader(database_url=db_url)
    supply = modules.supply_chain_exposure(issuer_id=issuer_id)
    ratings = modules.analyst_ratings(issuer_id=issuer_id)
    return EntityDetailResponse(
        issuer_id=issuer_id,
        label=_display_label(issuer_id, page.display_map),
        decisions=decisions,
        node_values=trace.node_values,
        themes=list(profile.get("theme_purity") or ()),
        analyst_ratings=ratings.model_dump(mode="json") if isinstance(ratings, AnalystRatings) else None,
        supply_chain_exposure=supply.model_dump(mode="json") if isinstance(supply, SupplyChainExposure) else None,
        display_map=page.display_map,
    )


def get_themes_overview(*, database_url: str | None = None) -> ThemesResponse:
    display_map = load_entity_display_items(database_url=database_url)
    rows_sql = THEME_ROWS_SQL

    def _q(cur: Any) -> ThemesResponse:
        cur.execute(THEME_LATEST_RUN_SQL)
        head = cur.fetchone()
        if not head or not head.get("run_id"):
            return ThemesResponse()
        run_id = str(head["run_id"])
        cur.execute(rows_sql, (run_id,))
        groups: dict[str, ThemePurityGroupDto] = {}
        for r in cur.fetchall():
            tid = str(r["theme_id"])
            group = groups.get(tid)
            if group is None:
                group = ThemePurityGroupDto(
                    theme_id=tid,
                    theme=str(r["theme"]),
                    definition_version=str(r["definition_version"]),
                    definition_sha256=str(r["definition_sha256"]),
                    cutoff=str(r["cutoff"]),
                    run_id=run_id,
                )
                groups[tid] = group
            iid = str(r["issuer_id"])
            group.rows.append(
                ThemePurityRowDto(
                    issuer_id=iid,
                    label=_display_label(iid, display_map),
                    theme_share=r.get("theme_share"),
                    consolidated_revenue=str(r.get("consolidated_revenue") or "0"),
                    in_theme_revenue=str(r.get("in_theme_revenue") or "0"),
                    out_of_theme_revenue=str(r.get("out_of_theme_revenue") or "0"),
                    unclassified_revenue=str(r.get("unclassified_revenue") or "0"),
                    partition_residual=str(r.get("partition_residual") or "0"),
                    segments=r.get("segments"),
                    confidence=r.get("confidence"),
                    reason_codes=list(r.get("reason_codes") or ()),
                    extractor=str(r.get("extractor") or ""),
                    availability_status=str(r.get("availability_status") or "unavailable"),
                    source_evidence_status=str(r.get("source_evidence_status") or ""),
                    factor_validation_status=str(r.get("factor_validation_status") or ""),
                    period_end=str(r.get("period_end") or ""),
                )
            )
        return ThemesResponse(groups=list(groups.values()))

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return ThemesResponse()


def get_fund_valuations(*, database_url: str | None = None) -> FundValuationsResponse:
    lines_sql = """
      with vintage as materialized (
        select v.fund_id, coalesce(v.fund_name, v.fund_id) as fund_name,
               to_char(v.report_period, 'YYYY-MM-DD') as report_period,
               v.holding_name, v.ticker, v.percent_of_net_assets, v.listing_id,
               lpad(substring(v.issuer_entity from '^(?:issuer|company):cik:0*([0-9]+)$'), 10, '0') as issuer_cik
        from mart.fund_holdings_valuation v
      ), decisions as materialized (
        select coalesce(ei.listing_id, d.issuer_id) as listing_id,
               coalesce(
                 lpad(nullif(ltrim(ei_issuer.cik, '0'), ''), 10, '0'),
                 lpad(substring(d.issuer_id from '^(?:issuer|company):cik:0*([0-9]+)$'), 10, '0')
               ) as issuer_cik,
               d.current_price_to_sales as current_ps,
               d.target_price_to_sales as target_ps_midpoint,
               d.current_price_to_gross_profit as current_pgp,
               d.target_price_to_gross_profit as target_pgp_midpoint,
               d.valuation_gap, d.tier, d.outcome as availability
        from mart.strategy_decisions d
        left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
        left join mart.entity_identity ei on ei.entity_id::text = d.issuer_id
        left join mart.entity_identity ei_issuer on ei_issuer.entity_id::text = d.issuer_id
        where scope.capture_run_id = %s
      )
      select v.fund_id, v.fund_name, v.report_period, v.holding_name, v.ticker,
             v.percent_of_net_assets::text as weight_pct,
             r.current_ps::text, r.target_ps_midpoint::text,
             r.current_pgp::text, r.target_pgp_midpoint::text,
             r.valuation_gap::text, r.tier, r.availability
      from vintage v
      left join decisions r on (r.listing_id = v.listing_id or (r.issuer_cik is not null and r.issuer_cik = v.issuer_cik))
      order by v.fund_id, v.percent_of_net_assets desc nulls last, v.holding_name
    """
    consolidation_sql = """
      select fund_id, weighted_valuation_gap::text as weighted_gap, valuation_basis,
             valued_weight_pct::text as valued_weight_pct, availability_status,
             source_evidence_status, factor_validation_status, reason_codes
      from mart.fund_virtual_company where run_id = %s
    """
    coverage_sql = """
      select distinct on (fund_id) fund_id,
             total_weight_pct::text as total_weight_pct,
             resolved_weight_pct::text as resolved_weight_pct
      from mart.fund_holdings_coverage order by fund_id, transaction_time desc, report_period desc
    """

    def _q(cur: Any) -> FundValuationsResponse:
        run_id, _ = resolve_governed_head_run_id(cur, _id_prefix_like("fund_consolidation"))
        join_run_id = run_id or "capture-run:none"
        cur.execute(lines_sql, (join_run_id,))
        line_rows = cur.fetchall()
        cur.execute(consolidation_sql, (join_run_id,))
        consol = {str(r["fund_id"]): dict(r) for r in cur.fetchall()}
        cur.execute(coverage_sql)
        cov = {str(r["fund_id"]): dict(r) for r in cur.fetchall()}
        by_fund: dict[str, FundValuationDto] = {}
        for r in line_rows:
            fid = str(r["fund_id"])
            fund = by_fund.get(fid)
            if fund is None:
                c = consol.get(fid, {})
                cv = cov.get(fid, {})
                fund = FundValuationDto(
                    fund_id=fid,
                    fund_name=str(r["fund_name"]),
                    report_period=str(r["report_period"]),
                    run_id=run_id,
                    valued_weight_pct=str(c.get("valued_weight_pct") or "0.00"),
                    resolved_weight_pct=str(cv.get("resolved_weight_pct") or "0.00"),
                    total_weight_pct=str(cv.get("total_weight_pct") or "0.00"),
                    weighted_gap=c.get("weighted_gap"),
                    weighted_gap_basis=c.get("valuation_basis"),
                    availability_status=c.get("availability_status"),
                    source_evidence_status=c.get("source_evidence_status"),
                    factor_validation_status=c.get("factor_validation_status"),
                    reason_codes=list(c.get("reason_codes") or ()),
                )
                by_fund[fid] = fund
            fund.lines.append(
                ValuedHoldingRowDto(
                    holding_name=str(r["holding_name"]),
                    ticker=r.get("ticker"),
                    weight_pct=r.get("weight_pct"),
                    current_ps=r.get("current_ps"),
                    target_ps_midpoint=r.get("target_ps_midpoint"),
                    current_pgp=r.get("current_pgp"),
                    target_pgp_midpoint=r.get("target_pgp_midpoint"),
                    valuation_gap=r.get("valuation_gap"),
                    tier=r.get("tier"),
                    availability=r.get("availability"),
                )
            )
        return FundValuationsResponse(funds=sorted(by_fund.values(), key=lambda f: f.fund_name))

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return FundValuationsResponse()


_BACKTEST_RUN_SELECT = """
  select run_id, strategy_key, strategy_version, universe_id,
         start_date::text, end_date::text, status,
         cagr_monthly::float, sharpe_daily::float, max_dd_daily::float,
         vol_daily::float, turnover_monthly::float, calmar_daily::float,
         coalesce(metrics_payload, '{}'::jsonb) as metrics_payload, error_message,
         executed_at::text, created_at::text,
         definition_content_sha256, price_basis, claim_ceiling,
         benchmark_rule, benchmark_cagr::float, benchmark_max_dd::float
  from mart.backtest_runs
"""


def list_backtest_runs(*, limit: int = 20, database_url: str | None = None) -> BacktestRunsResponse:
    def _q(cur: Any) -> BacktestRunsResponse:
        cur.execute(f"{_BACKTEST_RUN_SELECT} order by created_at desc limit %s", (limit,))
        return BacktestRunsResponse(runs=[BacktestRunDto(**dict(r)) for r in cur.fetchall()])

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return BacktestRunsResponse()


def get_backtest_detail(run_id: str, *, database_url: str | None = None) -> BacktestDetailResponse | None:
    def _q(cur: Any) -> BacktestDetailResponse | None:
        cur.execute(f"{_BACKTEST_RUN_SELECT} where run_id = %s", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            """
            select run_id, resolution, valuation_date::text,
                   cum_nav::float, drawdown::float, gross_exposure::float,
                   cash_weight::float, benchmark_cum_nav::float
            from mart.backtest_valuations
            where run_id = %s
            order by valuation_date asc
            """,
            (run_id,),
        )
        vals = [BacktestValuationDto(**dict(r)) for r in cur.fetchall()]
        cur.execute(
            """
            select trade_id, run_id, trade_date::text, symbol, side,
                   shares::float, execution_price::float, trade_value::float,
                   weight_before::float, weight_after::float, fee_paid::float
            from mart.backtest_trades
            where run_id = %s
            order by trade_date asc, trade_id asc
            limit 200
            """,
            (run_id,),
        )
        trades = [BacktestTradeDto(**dict(r)) for r in cur.fetchall()]
        return BacktestDetailResponse(run=BacktestRunDto(**dict(row)), valuations=vals, trades=trades)

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return None


# --- MCP / Company / Theme / ETF / Backtest Readers (using metric_node_values & served_head) ---


def _decision_reason_codes(decision_row: dict[str, Any] | None) -> list[str]:
    if not decision_row:
        return []
    if codes := decision_row.get("reason_codes"):
        return [str(code) for code in codes]
    if exclusion := decision_row.get("exclusion_reason"):
        return [str(exclusion)]
    return ["missing_audited_gross_profit_or_coordinate_join"] if decision_row.get("outcome") == "excluded" else []


def _opt_str(row: dict[str, Any] | None, key: str) -> str | None:
    if not row:
        return None
    val = row.get(key)
    return str(val) if val is not None else None


def _opt_int(row: dict[str, Any] | None, key: str) -> int | None:
    if not row:
        return None
    val = row.get(key)
    return int(val) if isinstance(val, int) and not isinstance(val, bool) else None


def _format_company_profile(
    *,
    issuer_id: str,
    decision_row: dict[str, Any] | None,
    gppe_row: dict[str, Any] | None,
    themes: list[dict[str, Any]],
) -> dict[str, Any]:
    target_pgp = _opt_str(decision_row, "target_price_to_gross_profit")
    target_ps = _opt_str(decision_row, "target_price_to_sales")
    valuation_basis = (
        "price_to_gross_profit" if target_pgp is not None else ("price_to_sales" if target_ps is not None else None)
    )
    resolved_issuer_id = _opt_str(decision_row, "issuer_id") or issuer_id
    gppe_confidence = _opt_str(gppe_row, "confidence")
    gppe_reason_codes = [str(c) for c in (gppe_row or {}).get("reason_codes") or ()]
    decision_reason_codes = _decision_reason_codes(decision_row)

    _, resolved_ticker, resolved_display_name = resolve_static_entity_display(resolved_issuer_id)
    if resolved_ticker is None and issuer_id != resolved_issuer_id:
        _, resolved_ticker, resolved_display_name = resolve_static_entity_display(issuer_id)

    shared = {
        "tier": _opt_str(decision_row, "tier"),
        "valuation_gap": _opt_str(decision_row, "valuation_gap"),
        "valuation_basis": valuation_basis,
        "current_price_to_sales": _opt_str(decision_row, "current_price_to_sales"),
        "target_price_to_sales": target_ps,
        "current_price_to_gross_profit": _opt_str(decision_row, "current_price_to_gross_profit"),
        "target_price_to_gross_profit": target_pgp,
        "confidence": _opt_str(decision_row, "confidence") or gppe_confidence,
        "peg": _opt_str(decision_row, "peg"),
        "peg_rank": _opt_int(decision_row, "peg_rank"),
        "peg_reason_codes": [str(c) for c in (decision_row or {}).get("peg_reason_codes") or ()],
    }
    gppe_val = _opt_str(gppe_row, "gppe")
    return {
        "issuer_id": resolved_issuer_id,
        "ticker": resolved_ticker,
        "display_name": resolved_display_name,
        **shared,
        "reason_codes": decision_reason_codes if decision_row else gppe_reason_codes,
        "gppe": gppe_val,
        "gppe_detail": {
            "gppe": gppe_val,
            "operating_branch": _opt_str(gppe_row, "operating_branch"),
            "capital_adjusted_gross_profit": _opt_str(gppe_row, "capital_adjusted_gross_profit"),
            "confidence": gppe_confidence,
            "availability_status": _opt_str(gppe_row, "availability_status") or "unavailable",
            "reason_codes": gppe_reason_codes,
        }
        if gppe_row
        else None,
        "strategy_decision": {
            "cutoff_at": _opt_str(decision_row, "cutoff_at"),
            "outcome": _opt_str(decision_row, "outcome"),
            **shared,
            "reason_codes": decision_reason_codes,
        }
        if decision_row
        else None,
        "theme_purity": themes,
        "availability_status": "available" if (decision_row or gppe_row or bool(themes)) else "unavailable",
    }


class PostgresCompanyProfileReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]:
        clean = issuer_id.strip()
        params = (clean, clean, clean)

        def _q(cur: Any) -> tuple[Any, dict[str, Any] | None, list[dict[str, Any]]]:
            cur.execute(
                f"""
                select coalesce(ei.legacy_id, d.issuer_id) as issuer_id,
                       to_char(d.cutoff_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff_at,
                       d.tier, d.valuation_gap::text as valuation_gap,
                       d.current_price_to_sales::text as current_price_to_sales,
                       d.target_price_to_sales::text as target_price_to_sales,
                       d.current_price_to_gross_profit::text as current_price_to_gross_profit,
                       d.target_price_to_gross_profit::text as target_price_to_gross_profit,
                       d.outcome, d.exclusion_reason, d.rank,
                       d.peg::text as peg, d.peg_rank, d.peg_reason_codes,
                       m.confidence, m.reason_codes
                from mart.strategy_decisions d
                left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
                left join lateral (
                  select m.confidence::text as confidence, m.reason_codes
                  from mart.metric_node_values m
                  where m.run_id = scope.capture_run_id
                    and m.issuer_id = d.issuer_id
                    and m.node_key = coalesce(d.labor_efficiency_metric, 'gppe_uniform_charge_v0')
                  order by m.recorded_at desc
                  limit 1
                ) m on true
                left join lateral (
                  select ei.legacy_id from mart.entity_identity ei
                  where ei.entity_id::text = d.issuer_id limit 1
                ) ei on true
                where {issuer_identity_sql("d.issuer_id", "%s")}
                  and {served_or_empty_head_predicate("scope.capture_run_id")}
                order by d.cutoff_at desc limit 1
                """,
                params,
            )
            decision = cur.fetchone()
            cur.execute(
                f"""
                select v.value::text as gppe, v.node_key as operating_branch,
                       v.confidence::text as confidence,
                       coalesce(v.availability_status, 'unavailable') as availability_status,
                       v.reason_codes
                from mart.metric_node_values v
                where {issuer_identity_sql("v.issuer_id", "%s")}
                  and v.node_key = any(%s::text[])
                  and {served_run_predicate("v.run_id")}
                order by (v.availability_status = 'available') desc, v.period desc limit 1
                """,
                (*params, list(GPPE_VARIANT_KEYS)),
            )
            gppe = cur.fetchone()
            cur.execute(
                f"""
                select theme_id, theme, theme_share, in_theme_revenue, consolidated_revenue,
                       segments, confidence, availability_status
                from (
                  select distinct on (p.theme_id)
                         p.theme_id, p.theme, p.theme_share::text as theme_share,
                         p.theme_share as raw_share, p.in_theme_revenue::text as in_theme_revenue,
                         p.consolidated_revenue::text as consolidated_revenue, p.segments,
                         p.confidence::text as confidence,
                         coalesce(p.availability_status, 'unavailable') as availability_status
                  from mart.issuer_theme_purity p
                  where {issuer_identity_sql("p.issuer_id", "%s")}
                    and p.partition_id is not null
                    and {served_or_empty_head_predicate("p.run_id")}
                  order by p.theme_id, p.cutoff desc, p.created_at desc
                ) latest_per_theme
                order by raw_share desc nulls last, theme asc
                """,
                params,
            )
            themes = [dict(r) for r in cur.fetchall()]
            return decision, gppe, themes

        try:
            decision_row, gppe_row, themes = with_mart_readonly(_q, database_url=self._database_url)
        except psycopg.Error:
            decision_row, gppe_row, themes = None, None, []
        return _format_company_profile(
            issuer_id=issuer_id,
            decision_row=dict(decision_row) if decision_row else None,
            gppe_row=dict(gppe_row) if gppe_row else None,
            themes=themes,
        )


def _legacy_id_join(column: str) -> str:
    return (
        "left join lateral (select ei.legacy_id from mart.entity_identity ei "
        f"where ei.entity_id::text = {column} limit 1) ei on true"
    )


def _query_mart(database_url: str, label: str, query_fn: Any, default: Any) -> Any:
    try:
        return with_mart_readonly(query_fn, database_url=database_url)
    except psycopg.Error as exc:
        logger.warning("%s failed to query database: %s", label, exc)
        return default


def _format_theme_row(row: dict[str, Any], *, include_issuer: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "theme_id": str(row.get("theme_id", "")),
        "theme": str(row.get("theme", "")),
        "theme_share": _opt_str(row, "theme_share"),
        "in_theme_revenue": _opt_str(row, "in_theme_revenue"),
        "consolidated_revenue": _opt_str(row, "consolidated_revenue"),
        "segments": _opt_int(row, "segments") if not include_issuer else (_opt_int(row, "segments") or 0),
        "confidence": _opt_str(row, "confidence"),
        "availability_status": str(row.get("availability_status", "unavailable")),
    }
    return {"issuer_id": str(row["issuer_id"]), "cik": _opt_int(row, "cik"), **out} if include_issuer else out


def normalize_theme_slug(theme_id: str) -> str:
    slug = theme_id.strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def resolve_fund_resolution_candidates(fund_id: str) -> dict[str, Any]:
    clean_id = fund_id.strip()
    token = clean_id.split(":")[-1].split("/")[-1].strip()
    upper_token, lower_token = token.upper(), token.lower()
    return {
        "clean_id": clean_id,
        "series_candidate": clean_id if clean_id.lower().startswith("etf:series:") else f"etf:series:{upper_token}",
        "upper_token": upper_token,
        "clean_upper": clean_id.upper(),
        "name_pattern": f"%{lower_token}%" if len(lower_token) >= 2 else "",
    }


class PostgresThemePurityLeaderboardReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]:
        clamped_limit = max(1, min(limit, 100))
        normalized_slug = normalize_theme_slug(theme_id)

        def _load(cur: Any) -> list[dict[str, Any]]:
            cur.execute(
                f"""
                with latest_per_issuer as (
                    select distinct on (p.issuer_id)
                           p.issuer_id, p.cik, p.theme_id, p.theme, p.theme_share,
                           p.in_theme_revenue, p.consolidated_revenue, p.segments, p.confidence,
                           coalesce(p.availability_status, 'unavailable') as availability_status,
                           p.cutoff, p.created_at
                    from mart.issuer_theme_purity p
                    where (p.theme_id = %s or p.theme_id = %s or lower(p.theme) = lower(%s))
                      and p.partition_id is not null
                      and {served_or_empty_head_predicate("p.run_id")}
                    order by p.issuer_id, p.cutoff desc, p.created_at desc
                )
                select coalesce(ei.legacy_id, l.issuer_id) as issuer_id,
                       l.cik, l.theme_id, l.theme, l.theme_share::text as theme_share,
                       l.in_theme_revenue::text as in_theme_revenue,
                       l.consolidated_revenue::text as consolidated_revenue,
                       l.segments, l.confidence::text as confidence, l.availability_status
                from latest_per_issuer l
                {_legacy_id_join("l.issuer_id")}
                order by (l.theme_share) desc nulls last, (l.confidence) desc nulls last limit %s
                """,
                (theme_id, normalized_slug, theme_id.strip(), clamped_limit),
            )
            return [_format_theme_row(r, include_issuer=True) for r in cur.fetchall()]

        issuers = _query_mart(self._database_url, "get_leaderboard", _load, [])
        return {"theme_id": theme_id, "limit": clamped_limit, "count": len(issuers), "issuers": issuers}


def _etf_match_clauses(prefix: str, id_param: str) -> tuple[str, str]:
    col_id, col_name = f"{prefix}fund_id", f"{prefix}fund_name"
    identity_sub = (
        "select entity_id::text from mart.entity_identity "
        "where upper(current_ticker) in (%(upper_token)s, %(clean_upper)s) "
        "or upper(legacy_id) in (%(upper_token)s, %(clean_upper)s)"
    )
    where_sql = (
        f"{col_id} = %({id_param})s or {col_id} = %(series_candidate)s or {col_id} in ({identity_sub}) "
        f"or (%(name_pattern)s <> '' and (lower({col_name}) like %(name_pattern)s or lower({col_id}) like %(name_pattern)s))"
    )
    rank_sql = (
        f"case when {col_id} = %({id_param})s or {col_id} = %(series_candidate)s then 1 "
        f"when {col_id} in ({identity_sub}) then 2 else 3 end asc"
    )
    return where_sql, rank_sql


class PostgresEtfProfileReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]:
        query_params = resolve_fund_resolution_candidates(fund_id)
        fund_where, fund_rank = _etf_match_clauses("", "clean_id")
        hold_where, hold_rank = _etf_match_clauses("v.", "target_fund_id")

        def _load(cur: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
            cur.execute(
                f"""
                select fund_id, coalesce(fund_name, fund_id) as fund_name,
                       to_char(cutoff, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
                       to_char(report_period, 'YYYY-MM-DD') as report_period,
                       weighted_valuation_gap::text as weighted_valuation_gap, valuation_basis,
                       total_weight_pct::text as total_weight_pct, resolved_weight_pct::text as resolved_weight_pct,
                       valued_weight_pct::text as valued_weight_pct, lines, valued_lines, confidence::text as confidence,
                       coalesce(availability_status, 'unavailable') as availability_status, reason_codes
                from mart.fund_virtual_company
                where ({fund_where})
                  and {served_or_empty_head_predicate("run_id", _id_prefix_like("fund_consolidation").removesuffix("%"))}
                order by {fund_rank}, cutoff desc, created_at desc limit 1
                """,
                query_params,
            )
            f_row = cur.fetchone()
            h_params = {**query_params, "target_fund_id": str(f_row["fund_id"]) if f_row else query_params["clean_id"]}
            cur.execute(
                f"""
                select coalesce(fund_name, fund_id) as fund_name, to_char(report_period, 'YYYY-MM-DD') as report_period,
                       holding_name, ticker, isin, percent_of_net_assets::text as weight_pct, value_usd::text as value_usd,
                       coalesce(ei.legacy_id, v.issuer_entity) as issuer_entity, v.listing_id
                from mart.fund_holdings_valuation v
                {_legacy_id_join("v.issuer_entity")}
                where {hold_where}
                order by {hold_rank}, v.percent_of_net_assets desc nulls last, v.holding_name asc limit 100
                """,
                h_params,
            )
            return f_row, cur.fetchall()

        fund_row, raw_holdings = _query_mart(self._database_url, "get_etf_profile", _load, (None, []))
        first_holding = raw_holdings[0] if raw_holdings else None
        holdings = [
            {
                "holding_name": str(r.get("holding_name", "")),
                **{
                    k: _opt_str(r, k)
                    for k in ("ticker", "isin", "weight_pct", "value_usd", "issuer_entity", "listing_id")
                },
            }
            for r in raw_holdings
        ]
        lines = _opt_int(fund_row, "lines")
        codes = fund_row.get("reason_codes") if fund_row else None
        return {
            "fund_id": fund_id,
            "fund_name": _opt_str(fund_row, "fund_name") or _opt_str(first_holding, "fund_name") or fund_id,
            "cutoff": _opt_str(fund_row, "cutoff"),
            "report_period": _opt_str(fund_row, "report_period") or _opt_str(first_holding, "report_period"),
            "weighted_valuation_gap": _opt_str(fund_row, "weighted_valuation_gap"),
            "weighted_valuation_gap_basis": _opt_str(fund_row, "valuation_basis"),
            "total_weight_pct": _opt_str(fund_row, "total_weight_pct") or "0.00",
            "resolved_weight_pct": _opt_str(fund_row, "resolved_weight_pct") or "0.00",
            "valued_weight_pct": _opt_str(fund_row, "valued_weight_pct") or "0.00",
            "lines": lines if lines is not None else len(holdings),
            "valued_lines": _opt_int(fund_row, "valued_lines") or 0,
            "confidence": _opt_str(fund_row, "confidence"),
            "availability_status": _opt_str(fund_row, "availability_status")
            or ("available" if holdings else "unavailable"),
            "reason_codes": list(codes)
            if codes is not None
            else ([] if holdings else ["no_virtual_company_consolidation_found"]),
            "holdings": holdings,
        }


def backtest_run_limitations(run: dict[str, Any]) -> list[str]:
    payload = run.get("metrics_payload")
    stored = payload.get("limitations") if isinstance(payload, dict) else None
    return [str(item) for item in stored] if isinstance(stored, list) else []


_BACKTEST_RUN_SELECT = """
    select run_id, strategy_key, strategy_version, universe_id,
           start_date::text, end_date::text, status,
           cagr_monthly::text as cagr_monthly, sharpe_daily::text as sharpe_daily,
           max_dd_daily::text as max_dd_daily, vol_daily::text as vol_daily,
           turnover_monthly::text as turnover_monthly, calmar_daily::text as calmar_daily,
           metrics_payload, error_message, executed_at::text, created_at::text,
           definition_content_sha256, price_basis, claim_ceiling, benchmark_rule,
           benchmark_cagr::text as benchmark_cagr, benchmark_max_dd::text as benchmark_max_dd
    from mart.backtest_runs
"""


class PostgresBacktestReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_backtest_report(
        self, *, run_id: str | None = None, strategy_key: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        valuations: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []

        def _load(cur: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
            out_runs: list[dict[str, Any]] = []
            out_vals: list[dict[str, Any]] = []
            out_trades: list[dict[str, Any]] = []
            if run_id:
                cur.execute(f"{_BACKTEST_RUN_SELECT} where run_id = %s limit 1", (run_id,))
                if run_row := cur.fetchone():
                    out_runs.append(dict(run_row))
                    cur.execute(
                        """
                        select run_id, resolution, valuation_date::text,
                               cum_nav::text as cum_nav, drawdown::text as drawdown,
                               gross_exposure::text as gross_exposure, cash_weight::text as cash_weight,
                               benchmark_cum_nav::text as benchmark_cum_nav
                        from mart.backtest_valuations where run_id = %s order by valuation_date desc limit 100
                        """,
                        (run_id,),
                    )
                    out_vals = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        """
                        select trade_id, run_id, trade_date::text, symbol, side,
                               shares::text as shares, execution_price::text as execution_price,
                               trade_value::text as trade_value, weight_before::text as weight_before,
                               weight_after::text as weight_after, fee_paid::text as fee_paid
                        from mart.backtest_trades where run_id = %s order by trade_date desc limit 100
                        """,
                        (run_id,),
                    )
                    out_trades = [dict(r) for r in cur.fetchall()]
            else:
                where = " where strategy_key = %s" if strategy_key else ""
                params = (strategy_key, limit) if strategy_key else (limit,)
                cur.execute(f"{_BACKTEST_RUN_SELECT}{where} order by created_at desc limit %s", params)
                out_runs = [dict(r) for r in cur.fetchall()]
            return out_runs, out_vals, out_trades

        try:
            runs, valuations, trades = with_mart_readonly(_load, database_url=self._database_url)
        except Exception as e:
            logger.warning("Failed to read backtest from mart: %s", e)
            return {
                "runs": [],
                "valuations": [],
                "trades": [],
                "count": 0,
                "availability_status": "unavailable",
                "error": str(e),
            }

        for run in runs:
            run["limitations"] = backtest_run_limitations(run)
        return {
            "runs": runs,
            "valuations": valuations,
            "trades": trades,
            "count": len(runs),
            "availability_status": "available" if runs else "empty",
        }


# --- XHS Card export & Research Documents (S3 + app RLS) --------------------------------


def _put_s3_bytes(principal_id: str, data: bytes, content_type: str) -> tuple[str, str, int]:
    digest = hashlib.sha256(data).hexdigest()
    prefix = settings.s3_documents_prefix.strip("/") or "documents"
    key = f"{prefix}/{principal_id}/{digest[:2]}/{digest}"
    client = create_s3_client(settings.to_s3_settings())
    put_object_bytes(client, bucket=settings.s3_bucket, key=key, data=data, content_type=content_type)
    return key, digest, len(data)


def _get_s3_bytes(key: str) -> bytes | None:
    try:
        client = create_s3_client(settings.to_s3_settings())
        return read_object_bytes(client, bucket=settings.s3_bucket, key=key)
    except Exception:  # noqa: BLE001
        return None


def list_documents(
    context: AccessContext,
    *,
    limit: int | None = None,
    before_created_at: str | None = None,
    before_document_id: str | None = None,
    database_url: str | None = None,
) -> DocumentListResponse:
    clamped = clamp_list_limit(limit)
    cursor = parse_before_cursor(before_created_at, before_document_id)

    def _q(cur: Any) -> DocumentListResponse:
        cur.execute(
            """
            select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at,
                   to_char(d.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') as created_at_cursor
            from app.research_documents d
            where not exists (
              select 1 from app.research_document_tombstones t where t.document_id = d.document_id
            )
              and (%s::timestamptz is null or (d.created_at, d.document_id) < (%s::timestamptz, %s))
            order by d.created_at desc, d.document_id desc
            limit %s
            """,
            (cursor[0] if cursor else None, cursor[0] if cursor else None, cursor[1] if cursor else None, clamped),
        )
        rows = cur.fetchall()
        docs = [
            DocumentDto(
                document_id=str(r["document_id"]),
                tenant_id=str(r["tenant_id"]),
                owner_principal_id=str(r["owner_principal_id"]),
                created_at=r["created_at"].isoformat(),
            )
            for r in rows
        ]
        next_cursor = (
            DocumentCursorDto(created_at=str(rows[-1]["created_at_cursor"]), document_id=str(rows[-1]["document_id"]))
            if len(docs) == clamped and rows
            else None
        )
        return DocumentListResponse(documents=docs, next_before=next_cursor)

    return with_owner_scoped_runtime(context, _q, database_url=database_url)


def create_document(
    context: AccessContext,
    *,
    source_artifact_id: str,
    content_bytes: bytes,
    content_type: str = "text/plain; charset=utf-8",
    database_url: str | None = None,
) -> CreateDocumentResponse:
    clean_source = assert_stable_id(source_artifact_id, "source_artifact_id")
    clean_ct = assert_non_empty_text(content_type, "content_type")
    if not content_bytes:
        raise ValueError("document content must not be empty")
    object_key, sha256, byte_length = _put_s3_bytes(context.principal_id, content_bytes, clean_ct)

    def _q(cur: Any) -> CreateDocumentResponse:
        doc_id = _new_id("document")
        rev_id = _new_id("revision")
        cur.execute(
            """
            insert into app.research_documents (document_id, tenant_id, owner_principal_id)
            values (%s, %s, %s)
            returning document_id, tenant_id, owner_principal_id, created_at
            """,
            (doc_id, context.tenant_id, context.principal_id),
        )
        d = cur.fetchone()
        cur.execute(
            """
            insert into app.research_document_revisions
              (revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id,
               artifact_sha256, artifact_byte_length, artifact_content_type, object_key)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning revision_id, document_id, tenant_id, owner_principal_id, source_artifact_id,
                      artifact_sha256, artifact_byte_length, artifact_content_type, created_at
            """,
            (
                rev_id,
                doc_id,
                context.tenant_id,
                context.principal_id,
                clean_source,
                sha256,
                byte_length,
                clean_ct,
                object_key,
            ),
        )
        rv = cur.fetchone()
        return CreateDocumentResponse(
            document=DocumentDto(
                document_id=str(d["document_id"]),
                tenant_id=str(d["tenant_id"]),
                owner_principal_id=str(d["owner_principal_id"]),
                created_at=d["created_at"].isoformat(),
            ),
            revision=DocumentRevisionDto(
                revision_id=str(rv["revision_id"]),
                document_id=str(rv["document_id"]),
                tenant_id=str(rv["tenant_id"]),
                owner_principal_id=str(rv["owner_principal_id"]),
                source_artifact_id=str(rv["source_artifact_id"]),
                artifact_sha256=str(rv["artifact_sha256"]),
                artifact_byte_length=int(rv["artifact_byte_length"]),
                artifact_content_type=str(rv["artifact_content_type"]),
                created_at=rv["created_at"].isoformat(),
            ),
        )

    return with_owner_scoped_runtime(context, _q, database_url=database_url)


def get_document_detail(
    context: AccessContext, *, document_id: str, database_url: str | None = None
) -> DocumentDetailResponse | None:
    def _q(cur: Any) -> tuple[DocumentDto, list[DocumentRevisionDto], str | None] | None:
        cur.execute(
            """
            select d.document_id, d.tenant_id, d.owner_principal_id, d.created_at
            from app.research_documents d
            where d.document_id = %s
              and not exists (select 1 from app.research_document_tombstones t where t.document_id = d.document_id)
            """,
            (document_id,),
        )
        d = cur.fetchone()
        if d is None:
            return None
        cur.execute(
            """
            select r.revision_id, r.document_id, r.tenant_id, r.owner_principal_id, r.source_artifact_id,
                   r.artifact_sha256, r.artifact_byte_length, r.artifact_content_type, r.object_key, r.created_at
            from app.research_document_revisions r
            where r.document_id = %s
              and not exists (select 1 from app.research_document_tombstones t where t.document_id = r.document_id)
            order by r.created_at asc, r.revision_id asc
            """,
            (document_id,),
        )
        rev_rows = cur.fetchall()
        revs = [
            DocumentRevisionDto(
                revision_id=str(rv["revision_id"]),
                document_id=str(rv["document_id"]),
                tenant_id=str(rv["tenant_id"]),
                owner_principal_id=str(rv["owner_principal_id"]),
                source_artifact_id=str(rv["source_artifact_id"]),
                artifact_sha256=str(rv["artifact_sha256"]),
                artifact_byte_length=int(rv["artifact_byte_length"]),
                artifact_content_type=str(rv["artifact_content_type"]),
                created_at=rv["created_at"].isoformat(),
            )
            for rv in rev_rows
        ]
        latest_key = str(rev_rows[-1]["object_key"]) if rev_rows else None
        return (
            DocumentDto(
                document_id=str(d["document_id"]),
                tenant_id=str(d["tenant_id"]),
                owner_principal_id=str(d["owner_principal_id"]),
                created_at=d["created_at"].isoformat(),
            ),
            revs,
            latest_key,
        )

    found = with_owner_scoped_runtime(context, _q, database_url=database_url)
    if found is None:
        return None
    doc, revs, latest_key = found
    raw = _get_s3_bytes(latest_key) if latest_key else None
    return DocumentDetailResponse(
        document=doc,
        revisions=revs,
        latest_content=raw.decode("utf-8", errors="replace") if raw is not None else None,
    )


def get_document_revision_bytes(
    context: AccessContext, *, document_id: str, revision_id: str, database_url: str | None = None
) -> tuple[bytes, str] | None:
    def _q(cur: Any) -> tuple[str, str] | None:
        cur.execute(
            """
            select r.object_key, r.artifact_content_type
            from app.research_document_revisions r
            where r.document_id = %s and r.revision_id = %s
              and not exists (select 1 from app.research_document_tombstones ts where ts.document_id = r.document_id)
            """,
            (document_id, revision_id),
        )
        r = cur.fetchone()
        return (str(r["object_key"]), str(r["artifact_content_type"])) if r else None

    found = with_owner_scoped_runtime(context, _q, database_url=database_url)
    if found is None:
        return None
    raw = _get_s3_bytes(found[0])
    return (raw, found[1]) if raw is not None else None


def delete_document(
    context: AccessContext, *, document_id: str, database_url: str | None = None
) -> DeleteDocumentResponse | None:
    def _q(cur: Any) -> DeleteDocumentResponse | None:
        cur.execute(
            """
            insert into app.research_document_tombstones (tombstone_id, document_id, tenant_id, owner_principal_id)
            select %s, d.document_id, %s, %s
            from app.research_documents d
            where d.document_id = %s
            on conflict (document_id) do nothing
            returning tombstone_id, document_id, created_at
            """,
            (_new_id("tombstone"), context.tenant_id, context.principal_id, document_id),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "select tombstone_id, document_id, created_at from app.research_document_tombstones where document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return DeleteDocumentResponse(
            tombstone_id=str(row["tombstone_id"]),
            document_id=str(row["document_id"]),
            created_at=row["created_at"].isoformat(),
        )

    return with_owner_scoped_runtime(context, _q, database_url=database_url)


def _lookup_governed_card_artifact(
    *, card_kind: str, subject_key: str, convention: str, universe: str, database_url: str | None = None
) -> tuple[str, str] | None:
    sql = f"""
      select a.html, a.render_mode
      from mart.research_card_artifacts a
      where {served_run_predicate("a.run_id")}
        and a.card_kind = %s
        and a.subject_key = %s
        and a.convention = %s
        and a.universe = %s
      order by a.recorded_at desc limit 1
    """

    def _q(cur: Any) -> tuple[str, str] | None:
        cur.execute(sql, (card_kind, subject_key, convention, universe))
        row = cur.fetchone()
        return (str(row["html"]), str(row["render_mode"])) if row else None

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        return None


def export_or_render_card(
    context: AccessContext,
    req: CardExportRequest,
    *,
    research_repo: ResearchReadPort | None = None,
    report_request: ResearchReportRequest | None = None,
    database_url: str | None = None,
) -> CardExportResponse:
    db_url = database_url or settings.database_url
    active_repo = research_repo or MartResearchReadRepository(database_url=db_url)
    norm_convention = normalize_peg_convention(req.convention)
    norm_universe = normalize_ranking_universe(req.universe) or "all"
    if report_request is not None:
        effective_req = report_request
        subject_key = (
            report_request.target_entity_ids[0]
            if req.card_kind == CardKind.COMPANY and report_request.target_entity_ids
            else "theme:large_model_value"
        )
    else:
        target_cutoff_dt = (
            datetime.fromisoformat(req.cutoff.replace("Z", "+00:00")) if req.cutoff else datetime.now(UTC)
        )
        if req.cutoff is None:
            page_info = get_strategy_run_page(context, universe=norm_universe, database_url=db_url)
            if page_info.cutoff_at:
                target_cutoff_dt = datetime.fromisoformat(page_info.cutoff_at.replace("Z", "+00:00"))

        if req.card_kind == CardKind.COMPANY and req.issuer_id:
            report_kind = ResearchReportKind.COMPANY
            targets = (req.issuer_id,)
            sections = (
                ReportSectionKind.OPERATING_EFFICIENCY,
                ReportSectionKind.VALUATION,
                ReportSectionKind.STRATEGY_SUMMARY,
            )
            subject_key = req.issuer_id
        else:
            report_kind = ResearchReportKind.THEME_RANKING
            targets = ("theme:large_model_value",)
            sections = (ReportSectionKind.VALUATION, ReportSectionKind.STRATEGY_SUMMARY, ReportSectionKind.RANKING)
            subject_key = "theme:large_model_value"

        effective_req = ResearchReportRequest(
            report_kind=report_kind,
            target_entity_ids=targets,
            cutoff_at=target_cutoff_dt,
            section_kinds=sections,
            strategy_id=DASHBOARD_STRATEGY_ID,
        )

    report = build_research_report(
        effective_req,
        active_repo,
        context=context,
    )
    card = build_card(report, req.card_kind, title=req.title)
    strategy_ctx = (
        build_strategy_context_from_report(report, strategy_id=DASHBOARD_STRATEGY_ID)
        if req.card_kind in (CardKind.RANKING, CardKind.COMPARISON, CardKind.STRATEGY_SUMMARY)
        else None
    )
    card = enrich_card_metadata(
        card,
        report,
        strategy_context=strategy_ctx,
        style_preset=req.preset,
        include_rendered_html=False,
    )
    if req.page is not None:
        pages = paginate_card_series(card, preset=req.preset)
        card = pages[max(0, min(req.page - 1, len(pages) - 1))]

    if req.format == "json":
        return CardExportResponse(
            card_id=card.card_id,
            card_kind=card.card_kind.value,
            render_mode="cold_verdict_json",
            card=json.loads(render_card_json(card)),
        )

    pre = (
        _lookup_governed_card_artifact(
            card_kind=req.card_kind.value,
            subject_key=subject_key,
            convention=norm_convention,
            universe=norm_universe,
            database_url=db_url,
        )
        if req.page is None
        else None
    )
    html: str
    render_mode: str
    if pre is not None and verify_card_html(card, pre[0])[0]:
        html, render_mode = pre
    else:
        html = render_card_html(card, preset=req.preset)
        ok, violations = verify_card_html(card, html)
        if not ok:
            raise ValueError(f"rendered card HTML failed verification: {violations}")
        render_mode = "cold_verdict_html"

    doc_id: str | None = None
    if req.persist:
        try:
            saved = create_document(
                context,
                source_artifact_id=card.card_id,
                content_bytes=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                database_url=db_url,
            )
            doc_id = saved.document.document_id
        except Exception:  # noqa: BLE001 — persistence best-effort when object store/app is offline in unit probes
            doc_id = None

    return CardExportResponse(
        card_id=card.card_id,
        card_kind=card.card_kind.value,
        render_mode=render_mode,
        document_id=doc_id,
        rendered_html=html,
        card=json.loads(render_card_json(card)),
    )


def metric_node_values(
    *, node_key: str, issuer_id: str | None = None, limit: int = 100, database_url: str | None = None
) -> MetricNodeValueReport | MetricNodeValueUnavailable:
    repo = PostgresMetricNodeValueRepository(database_url=database_url or settings.database_url)
    return repo.latest(node_key=node_key, issuer_id=issuer_id, limit=limit)


__all__ = [
    "AliasKind",
    "BacktestDetailResponse",
    "BacktestRunsResponse",
    "CARD_EXPORT_CACHE_CONTROL",
    "CardExportRequest",
    "CardExportResponse",
    "CreateDocumentRequest",
    "CreateDocumentResponse",
    "DASHBOARD_STRATEGY_ID",
    "DeleteDocumentResponse",
    "DocumentDetailResponse",
    "DocumentListResponse",
    "ENTITY_DISPLAY_SQL",
    "EntityDetailResponse",
    "EntityDisplayMapResponse",
    "FundValuationsResponse",
    "OverviewResponse",
    "POINTER_HEAD_SQL",
    "PostgresCompanyProfileReader",
    "RankingsResponse",
    "SERVED_HEAD_SQL",
    "StrategyRunPageResponse",
    "THEME_LATEST_RUN_SQL",
    "ThemesResponse",
    "TraceResponse",
    "XHS_CARD_HEIGHT_PX",
    "XHS_CARD_WIDTH_PX",
    "assert_non_empty_text",
    "assert_stable_id",
    "clamp_list_limit",
    "create_document",
    "delete_document",
    "export_or_render_card",
    "get_backtest_detail",
    "get_document_detail",
    "get_document_revision_bytes",
    "get_entity_detail",
    "get_fund_valuations",
    "get_overview",
    "get_strategy_run_page",
    "get_themes_overview",
    "get_trace",
    "is_registered_input_key",
    "list_backtest_runs",
    "list_documents",
    "load_entity_display_items",
    "metric_node_values",
    "parse_before_cursor",
    "project_peg_by_convention",
    "render_report_markdown",
    "resolve_governed_head_run_id",
    "served_run_predicate",
]
