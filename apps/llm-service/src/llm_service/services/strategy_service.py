"""Strategy domain services: rankings, backtests, PEG conventions, and strategy-run projections."""

from __future__ import annotations

import hashlib
import inspect
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from truealpha_contracts.environment_contract import UnknownUniverse, load_environment_contract
from truealpha_contracts.strategy import ExclusionReason
from truealpha_contracts.universes import universes_with_role
from truealpha_runtime.access import AccessContext
from truealpha_runtime.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository, read_strategy_provenance

from llm_service.config import settings
from llm_service.services._db import with_mart_readonly
from llm_service.services.panel_service import (
    GPPE_VARIANT_KEYS,
    RANKING_PANEL_INPUT_KEYS,
    WIDE_PANEL_INPUT_KEYS,
    EntityDisplayItem,
    WidePanelCellDto,
    _display_label,
    _load_wide_panel,
    load_entity_display_items,
)

DASHBOARD_STRATEGY_ID = "large_model_value_v0"

_EXCLUSION_LABELS: dict[str, str] = {
    reason.value: reason.value.replace("_", " ").capitalize() for reason in ExclusionReason
}


def normalize_peg_convention(value: str | None) -> str:
    if value in {"consensus", "analyst_consensus"}:
        return "consensus"
    if value in {"guidance", "company_guidance"}:
        return "guidance"
    return "historical_cagr"


def normalize_ranking_universe(value: str | None) -> str | None:
    clean = (value or "").strip().lower()
    if not clean:
        keys = universes_with_role("strategy")
        return keys[0] if keys else None
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
    convention: str = "historical_cagr"
    total_decisions: int = 0
    decisions: list[StrategyDecisionItemDto] = Field(default_factory=list)
    wide_panel: dict[str, dict[str, WidePanelCellDto]] = Field(default_factory=dict)
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)


class RankingsResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    strategy_id: str = DASHBOARD_STRATEGY_ID
    strategy_run_id: str | None = None
    definition_version: str | None = None
    review_state: str | None = None
    cutoff_at: str | None = None
    universe: str = "all"
    convention: str = "historical_cagr"
    total: int = 0
    rows: list[StrategyDecisionItemDto] = Field(default_factory=list)
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)


class BacktestRunDto(BaseModel):
    run_id: str
    strategy_key: str
    strategy_version: str
    universe_id: str
    start_date: str
    end_date: str
    status: str
    cagr_monthly: Decimal | None = None
    sharpe_daily: Decimal | None = None
    max_dd_daily: Decimal | None = None
    vol_daily: Decimal | None = None
    turnover_monthly: Decimal | None = None
    calmar_daily: Decimal | None = None
    metrics_payload: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    executed_at: str
    created_at: str
    definition_content_sha256: str | None = None
    price_basis: str | None = None
    claim_ceiling: str | None = None
    benchmark_rule: str | None = None
    benchmark_cagr: Decimal | None = None
    benchmark_max_dd: Decimal | None = None


class BacktestValuationDto(BaseModel):
    run_id: str
    resolution: str
    valuation_date: str
    cum_nav: Decimal
    drawdown: Decimal
    gross_exposure: Decimal
    cash_weight: Decimal
    benchmark_cum_nav: Decimal | None = None


class BacktestTradeDto(BaseModel):
    trade_id: int
    run_id: str
    trade_date: str
    symbol: str
    side: str
    shares: Decimal
    execution_price: Decimal
    trade_value: Decimal
    weight_before: Decimal
    weight_after: Decimal
    fee_paid: Decimal


class BacktestRunsResponse(BaseModel):
    runs: list[BacktestRunDto] = Field(default_factory=list)


class BacktestDetailResponse(BaseModel):
    run: BacktestRunDto
    valuations: list[BacktestValuationDto] = Field(default_factory=list)
    trades: list[BacktestTradeDto] = Field(default_factory=list)


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
    return with_mart_readonly(lambda cur: read_strategy_provenance(cur, strategy_run_id), database_url=database_url)


def call_strategy_repository(
    repo: Any,
    *,
    strategy_id: str,
    context: AccessContext,
    universe: str | None = None,
    convention: str | None = None,
) -> StrategyRunReport | StrategyRunUnavailable:
    params = inspect.signature(repo.get_latest).parameters
    kwargs: dict[str, Any] = {"strategy_id": strategy_id, "context": context}
    if "universe" in params:
        kwargs["universe"] = universe
    if "convention" in params:
        kwargs["convention"] = convention
    return repo.get_latest(**kwargs)


def _load_strategy_report(
    context: AccessContext,
    *,
    strategy_id: str = DASHBOARD_STRATEGY_ID,
    universe: str | None = None,
    convention: str | None = None,
    database_url: str | None = None,
) -> StrategyRunReport | StrategyRunUnavailable:
    repo = PostgresStrategyRunRepository(database_url=database_url or settings.database_url)
    return call_strategy_repository(
        repo, strategy_id=strategy_id, context=context, universe=universe, convention=convention
    )


def get_strategy_run_page(
    context: AccessContext,
    *,
    strategy_id: str = DASHBOARD_STRATEGY_ID,
    universe: str | None = None,
    cutoff: str | None = None,
    convention: str | None = None,
    database_url: str | None = None,
) -> StrategyRunPageResponse:
    norm_universe = "all" if universe is None else (normalize_ranking_universe(universe) or "all")
    norm_convention = normalize_peg_convention(convention)
    report = _load_strategy_report(
        context,
        strategy_id=strategy_id,
        universe=norm_universe,
        convention=norm_convention,
        database_url=database_url,
    )
    if isinstance(report, StrategyRunUnavailable):
        return StrategyRunPageResponse(
            available=False,
            reason=report.reason,
            strategy_id=strategy_id,
            universe=norm_universe,
            convention=norm_convention,
        )
    display_map = load_entity_display_items(database_url=database_url)
    prov_map = _load_strategy_provenance(report.strategy_run_id, database_url=database_url)
    wide_panel = _load_wide_panel(
        WIDE_PANEL_INPUT_KEYS + GPPE_VARIANT_KEYS + RANKING_PANEL_INPUT_KEYS,
        database_url=database_url,
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
        convention=norm_convention,
        total_decisions=len(items),
        decisions=items,
        wide_panel=wide_panel,
        display_map=display_map,
    )


def get_rankings(
    context: AccessContext,
    *,
    strategy_id: str = DASHBOARD_STRATEGY_ID,
    universe: str | None = "all",
    cutoff: str | None = None,
    convention: str | None = None,
    limit: int = 1000,
    database_url: str | None = None,
) -> RankingsResponse:
    page = get_strategy_run_page(
        context,
        strategy_id=strategy_id,
        universe=universe,
        cutoff=cutoff,
        convention=convention,
        database_url=database_url,
    )
    return RankingsResponse(
        available=page.available,
        reason=page.reason,
        strategy_id=page.strategy_id,
        strategy_run_id=page.strategy_run_id,
        definition_version=page.definition_version,
        review_state=page.review_state,
        cutoff_at=page.cutoff_at,
        universe=page.universe,
        convention=page.convention,
        total=page.total_decisions,
        rows=page.decisions[:limit],
        display_map=page.display_map,
    )


_BACKTEST_RUN_SELECT = """
  select run_id, strategy_key, strategy_version, universe_id,
         start_date::text, end_date::text, status,
         cagr_monthly, sharpe_daily, max_dd_daily, vol_daily, turnover_monthly, calmar_daily,
         coalesce(metrics_payload, '{}'::jsonb) as metrics_payload, error_message,
         executed_at::text, created_at::text,
         definition_content_sha256, price_basis, claim_ceiling,
         benchmark_rule, benchmark_cagr, benchmark_max_dd
  from mart.backtest_runs
"""


def list_backtest_runs(
    *, limit: int = 20, strategy_key: str | None = None, database_url: str | None = None
) -> BacktestRunsResponse:
    def _q(cur: Any) -> BacktestRunsResponse:
        if strategy_key:
            cur.execute(
                f"{_BACKTEST_RUN_SELECT} where strategy_key = %s order by created_at desc limit %s",
                (strategy_key, limit),
            )
        else:
            cur.execute(f"{_BACKTEST_RUN_SELECT} order by created_at desc limit %s", (limit,))
        return BacktestRunsResponse(runs=[BacktestRunDto(**dict(r)) for r in cur.fetchall()])

    return with_mart_readonly(_q, database_url=database_url)


def get_backtest_detail(run_id: str, *, database_url: str | None = None) -> BacktestDetailResponse | None:
    def _q(cur: Any) -> BacktestDetailResponse | None:
        cur.execute(f"{_BACKTEST_RUN_SELECT} where run_id = %s", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            """
            select run_id, resolution, valuation_date::text,
                   cum_nav, drawdown, gross_exposure,
                   cash_weight, benchmark_cum_nav
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
                   shares, execution_price, trade_value,
                   weight_before, weight_after, fee_paid
            from mart.backtest_trades
            where run_id = %s
            order by trade_date asc, trade_id asc
            limit 200
            """,
            (run_id,),
        )
        trades = [BacktestTradeDto(**dict(r)) for r in cur.fetchall()]
        return BacktestDetailResponse(run=BacktestRunDto(**dict(row)), valuations=vals, trades=trades)

    return with_mart_readonly(_q, database_url=database_url)


def backtest_run_limitations(run: dict[str, Any]) -> list[str]:
    payload = run.get("metrics_payload")
    stored = payload.get("limitations") if isinstance(payload, dict) else None
    return [str(item) for item in stored] if isinstance(stored, list) else []


class PostgresBacktestReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_backtest_report(
        self, *, run_id: str | None = None, strategy_key: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        valuations: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        if run_id:
            detail = get_backtest_detail(run_id, database_url=self._database_url)
            if detail is not None:
                runs = [detail.run.model_dump(mode="json")]
                valuations = [v.model_dump(mode="json") for v in detail.valuations[:100]]
                trades = [t.model_dump(mode="json") for t in detail.trades[:100]]
        else:
            resp = list_backtest_runs(limit=limit, strategy_key=strategy_key, database_url=self._database_url)
            runs = [r.model_dump(mode="json") for r in resp.runs]

        for run in runs:
            run["limitations"] = backtest_run_limitations(run)
        return {
            "runs": runs,
            "valuations": valuations,
            "trades": trades,
            "count": len(runs),
            "availability_status": "available" if runs else "empty",
        }


__all__ = [
    "BacktestDetailResponse",
    "BacktestRunDto",
    "BacktestRunsResponse",
    "BacktestTradeDto",
    "BacktestValuationDto",
    "DASHBOARD_STRATEGY_ID",
    "DecisionProvenanceDto",
    "PostgresBacktestReader",
    "RankingsResponse",
    "StrategyDecisionItemDto",
    "StrategyRunPageResponse",
    "backtest_run_limitations",
    "call_strategy_repository",
    "get_backtest_detail",
    "get_rankings",
    "get_strategy_run_page",
    "list_backtest_runs",
    "matches_ranking_universe",
    "normalize_peg_convention",
    "normalize_ranking_universe",
]
