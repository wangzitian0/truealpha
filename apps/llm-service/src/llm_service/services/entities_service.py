"""Entities domain services: Entity 360, holdings (Module 5), themes (Module 6), supply-chain (Module 3), analyst-ratings (Module 4), and trace."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field
from truealpha_runtime.access import AccessContext
from truealpha_runtime.issuer_modules_mart import (
    _ANALYST_RATINGS_BY_IDS_SQL,
    _SUPPLY_CHAIN_BY_IDS_SQL,
    ENTITY_DISPLAY_SQL,
    AnalystRatings,
    IssuerModuleUnavailable,
    PostgresIssuerModuleReader,
    SupplyChainExposure,
    resolve_issuer_candidates_from_cursor,
    resolve_issuer_identity_from_cursor,
    resolve_static_entity_display,
)
from truealpha_runtime.strategy_run_postgres import read_trace_lineage

from llm_service.config import settings
from llm_service.services._db import with_mart_readonly
from llm_service.services.panel_service import (
    GPPE_VARIANT_KEYS,
    EntityDisplayItem,
    _display_label,
    _id_prefix_like,
    resolve_governed_head_run_id,
    served_or_empty_head_predicate,
    served_run_predicate,
)
from llm_service.services.strategy_service import (
    _EXCLUSION_LABELS,
    DASHBOARD_STRATEGY_ID,
    StrategyDecisionItemDto,
    StrategyRunPageResponse,
    get_strategy_run_page,
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

_COMPANY_DECISION_SQL = f"""
  select d.issuer_id, d.strategy_run_id, sr.corpus_sha256,
         to_char(d.cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff_at,
         d.eligible, d.tier,
         d.capital_adjusted_labor_efficiency::text as capital_adjusted_labor_efficiency,
         d.labor_efficiency_metric,
         d.valuation_gap::text as valuation_gap,
         d.current_price_to_sales::text as current_price_to_sales,
         d.target_price_to_sales::text as target_price_to_sales,
         d.current_price_to_gross_profit::text as current_price_to_gross_profit,
         d.target_price_to_gross_profit::text as target_price_to_gross_profit,
         d.outcome, d.exclusion_reason, d.rank,
         d.target_weight::text as target_weight,
         d.peg::text as peg, d.peg_rank, d.peg_reason_codes,
         d.availability_status,
         m.confidence, m.reason_codes
  from mart.strategy_decisions d
  left join mart.strategy_runs sr on sr.strategy_run_id = d.strategy_run_id
  left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
  left join lateral (
    select m.confidence::text as confidence, m.reason_codes
    from mart.metric_node_values m
    where m.run_id = scope.capture_run_id
      and m.issuer_id = d.issuer_id
      and m.node_key = coalesce(d.labor_efficiency_metric, 'gppe_uniform_charge_v0')
    order by m.period desc, m.node_id
    limit 1
  ) m on true
  where d.issuer_id = any(%s::text[])
    and {served_or_empty_head_predicate("scope.capture_run_id")}
  order by d.cutoff_at desc limit 1
"""
_FUND_VIRTUAL_COLS = (
    "fund_id, coalesce(fund_name, fund_id) as fund_name,"
    ' to_char(cutoff, \'YYYY-MM-DD"T"HH24:MI:SS"Z"\') as cutoff,'
    " to_char(report_period, 'YYYY-MM-DD') as report_period,"
    " weighted_valuation_gap::text as weighted_valuation_gap, valuation_basis,"
    " total_weight_pct::text as total_weight_pct, resolved_weight_pct::text as resolved_weight_pct,"
    " valued_weight_pct::text as valued_weight_pct, lines, valued_lines,"
    " confidence::text as confidence, coalesce(availability_status, 'unavailable') as availability_status,"
    " source_evidence_status, factor_validation_status, reason_codes"
)


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


class EntityDetailResponse(BaseModel):
    issuer_id: str
    label: str
    decisions: list[StrategyDecisionItemDto] = Field(default_factory=list)
    node_values: list[TraceNodeValueDto] = Field(default_factory=list)
    themes: list[dict[str, Any]] = Field(default_factory=list)
    analyst_ratings: dict[str, Any] | None = None
    supply_chain_exposure: dict[str, Any] | None = None
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)


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


def _entity_display_item_from_parts(issuer_id: str, ticker: str, display_name: str | None) -> EntityDisplayItem:
    clean_name = (display_name or "").strip() or None
    if ticker and clean_name:
        label = clean_name if clean_name.startswith(f"{ticker} ·") else f"{ticker} · {clean_name}"
    else:
        label = ticker or clean_name or issuer_id
    return EntityDisplayItem(issuer_id=issuer_id, ticker=ticker, display_name=clean_name, label=label)


def _fetch_all_display_map_from_cursor(cur: Any) -> dict[str, EntityDisplayItem]:
    cur.execute(ENTITY_DISPLAY_SQL)
    out: dict[str, EntityDisplayItem] = {}
    for row in cur.fetchall():
        iid = str(row["issuer_id"])
        item = _entity_display_item_from_parts(iid, str(row.get("ticker") or "").strip(), row.get("display_name"))
        out[iid] = item
        legacy_id = row.get("legacy_id")
        if isinstance(legacy_id, str) and legacy_id and legacy_id not in out:
            out[legacy_id] = item
    return out


def _fetch_issuer_display_map_from_cursor(
    cur: Any,
    issuer_id: str,
    candidates: list[str],
    legacy_by_entity: dict[str, str],
) -> dict[str, EntityDisplayItem]:
    cur.execute(
        """
        select issuer_id, nullif(btrim(ticker), '') as ticker, nullif(btrim(display_name), '') as display_name
        from mart.entity_display_resolution
        where issuer_id = any(%s::text[])
        """,
        (candidates,),
    )
    out: dict[str, EntityDisplayItem] = {}
    for row in cur.fetchall():
        iid = str(row["issuer_id"])
        item = _entity_display_item_from_parts(iid, str(row.get("ticker") or "").strip(), row.get("display_name"))
        out[iid] = item
        if leg := legacy_by_entity.get(iid):
            out.setdefault(leg, item)
    if issuer_id not in out:
        for cand in candidates:
            if matched := out.get(cand):
                out[issuer_id] = matched
                break
    if issuer_id not in out:
        resolved_id, static_ticker, static_label = resolve_static_entity_display(issuer_id)
        if static_ticker:
            name_part = static_label.split(" · ", 1)[1] if " · " in static_label else None
            item = _entity_display_item_from_parts(issuer_id, static_ticker, name_part)
            out[issuer_id] = item
            if resolved_id and resolved_id not in out:
                out[resolved_id] = item
    return out


def _fetch_trace_nodes_from_cursor(cur: Any, candidates: list[str]) -> list[TraceNodeValueDto]:
    node_sql = f"""
      select v.node_key, v.period, v.value::text as value, v.unit, v.confidence::text as confidence,
             coalesce(v.availability_status, 'unavailable') as availability_status, v.reason_codes
      from mart.metric_node_values v
      where {served_run_predicate("v.run_id")}
        and v.issuer_id = any(%s::text[])
      order by v.node_key, v.period desc
      limit 100
    """
    cur.execute(node_sql, (candidates,))
    return [
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


def _fetch_module_row_from_cursor(cur: Any, sql: str, candidates: list[str]) -> dict[str, Any] | None:
    cur.execute(sql, (candidates,))
    row = cur.fetchone()
    if row is None:
        return None
    return {**row, "reason_codes": tuple(str(c) for c in row.get("reason_codes") or ())}


def _decision_row_to_dto(row: dict[str, Any], *, display_map: dict[str, EntityDisplayItem]) -> StrategyDecisionItemDto:
    iid = str(row["issuer_id"])
    cutoff_at = str(row.get("cutoff_at") or "")
    corpus_sha256 = str(row.get("corpus_sha256") or "")
    cur_ps = _opt_str(row, "current_price_to_sales")
    tgt_ps = _opt_str(row, "target_price_to_sales")
    cur_pgp = _opt_str(row, "current_price_to_gross_profit")
    tgt_pgp = _opt_str(row, "target_price_to_gross_profit")
    basis = (
        "price_to_gross_profit"
        if cur_pgp is not None or tgt_pgp is not None
        else ("price_to_sales" if cur_ps is not None or tgt_ps is not None else None)
    )
    cur_mult = cur_pgp if basis == "price_to_gross_profit" else cur_ps
    tgt_mult = tgt_pgp if basis == "price_to_gross_profit" else tgt_ps
    cap_eff = _opt_str(row, "capital_adjusted_labor_efficiency")
    outcome = str(row.get("outcome") or "unavailable")
    excl = _opt_str(row, "exclusion_reason")
    avail_status = _opt_str(row, "availability_status")
    tier = _opt_str(row, "tier")
    if avail_status == "not_applicable":
        avail = "not_applicable"
    elif outcome == "excluded":
        avail = "excluded"
    elif tier is not None or cur_ps is not None or cur_pgp is not None or cap_eff is not None:
        avail = "available"
    else:
        avail = "unavailable"
    trace_payload = f"mart:{corpus_sha256[:12]}:{iid}:{cutoff_at}"
    trace_id = f"trace:{hashlib.sha256(trace_payload.encode()).hexdigest()[:16]}"
    return StrategyDecisionItemDto(
        issuer_id=iid,
        label=_display_label(iid, display_map),
        cutoff_at=cutoff_at,
        outcome=outcome,
        eligible=bool(row.get("eligible")),
        tier=tier,
        capital_adjusted_labor_efficiency=cap_eff,
        labor_efficiency_metric=_opt_str(row, "labor_efficiency_metric"),
        current_price_to_sales=cur_ps,
        target_price_to_sales=tgt_ps,
        current_price_to_gross_profit=cur_pgp,
        target_price_to_gross_profit=tgt_pgp,
        valuation_basis=basis,
        current_multiple=cur_mult,
        target_multiple=tgt_mult,
        valuation_gap=_opt_str(row, "valuation_gap"),
        confidence=_opt_str(row, "confidence"),
        exclusion_reason=excl,
        exclusion_label=_EXCLUSION_LABELS.get(excl) if excl else None,
        rank=_opt_int(row, "rank"),
        target_weight=_opt_str(row, "target_weight"),
        peg=_opt_str(row, "peg"),
        peg_rank=_opt_int(row, "peg_rank"),
        availability=avail,
        trace_id=trace_id,
    )


def get_trace(
    context: AccessContext,
    *,
    issuer_id: str,
    cutoff: str | None = None,
    page: StrategyRunPageResponse | None = None,
    candidates: list[str] | None = None,
    database_url: str | None = None,
) -> TraceResponse:
    page = page if page is not None else get_strategy_run_page(context, cutoff=cutoff, database_url=database_url)
    if not page.available:
        return TraceResponse(available=False, reason=page.reason, issuer_id=issuer_id)
    decision = next(
        (d for d in page.decisions if d.issuer_id == issuer_id or issuer_id.lower() in d.label.lower()), None
    )
    target_iid = decision.issuer_id if decision else issuer_id

    def _q(cur: Any) -> tuple[list[TraceNodeValueDto], str | None, list[dict[str, Any]]]:
        cand = list(candidates) if candidates else resolve_issuer_candidates_from_cursor(cur, target_iid)
        nodes = _fetch_trace_nodes_from_cursor(cur, cand)
        res_id: str | None = None
        lin_inputs: list[dict[str, Any]] = []
        target_cutoff = decision.cutoff_at if decision else cutoff
        if page.strategy_run_id and target_cutoff:
            res_id, list_id, lineage = read_trace_lineage(cur, page.strategy_run_id, target_iid, target_cutoff)
            lin_inputs = _extract_lineage_inputs(lineage, list_id)
        return nodes, res_id, lin_inputs

    nodes, lineage_result_id, lineage_inputs = with_mart_readonly(_q, database_url=database_url)
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
    del context

    def _q(cur: Any) -> EntityDetailResponse:
        cand, legacy_by_entity = resolve_issuer_identity_from_cursor(cur, issuer_id)
        display_map = _fetch_issuer_display_map_from_cursor(cur, issuer_id, cand, legacy_by_entity)
        decision_row = _fetch_company_decision_from_cursor(cur, cand, legacy_by_entity)
        decisions = [_decision_row_to_dto(decision_row, display_map=display_map)] if decision_row else []
        node_values = _fetch_trace_nodes_from_cursor(cur, cand)
        themes = _fetch_company_themes_from_cursor(cur, cand)
        sc_row = _fetch_module_row_from_cursor(cur, _SUPPLY_CHAIN_BY_IDS_SQL, cand)
        ar_row = _fetch_module_row_from_cursor(cur, _ANALYST_RATINGS_BY_IDS_SQL, cand)
        supply = SupplyChainExposure.model_validate(sc_row) if sc_row else None
        ratings = AnalystRatings.model_validate(ar_row) if ar_row else None
        return EntityDetailResponse(
            issuer_id=issuer_id,
            label=_display_label(issuer_id, display_map),
            decisions=decisions,
            node_values=node_values,
            themes=themes,
            analyst_ratings=ratings.model_dump(mode="json") if isinstance(ratings, AnalystRatings) else None,
            supply_chain_exposure=supply.model_dump(mode="json") if isinstance(supply, SupplyChainExposure) else None,
            display_map=display_map,
        )

    return with_mart_readonly(_q, database_url=database_url)


def load_supply_chain_exposure(
    *, issuer_id: str, database_url: str | None = None
) -> SupplyChainExposure | IssuerModuleUnavailable:
    reader = PostgresIssuerModuleReader(database_url=database_url or settings.database_url)
    row = reader.supply_chain_exposure(issuer_id)
    return (
        row
        if isinstance(row, SupplyChainExposure)
        else IssuerModuleUnavailable(issuer_id=issuer_id, module="supply_chain_exposure")
    )


def load_analyst_ratings(
    *, issuer_id: str, database_url: str | None = None
) -> AnalystRatings | IssuerModuleUnavailable:
    reader = PostgresIssuerModuleReader(database_url=database_url or settings.database_url)
    row = reader.analyst_ratings(issuer_id)
    return (
        row
        if isinstance(row, AnalystRatings)
        else IssuerModuleUnavailable(issuer_id=issuer_id, module="analyst_ratings")
    )


def get_themes_overview(*, database_url: str | None = None) -> ThemesResponse:
    def _q(cur: Any) -> ThemesResponse:
        cur.execute(THEME_LATEST_RUN_SQL)
        head = cur.fetchone()
        if not head or not head.get("run_id"):
            return ThemesResponse()
        display_map = _fetch_all_display_map_from_cursor(cur)
        run_id = str(head["run_id"])
        cur.execute(THEME_ROWS_SQL, (run_id,))
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

    return with_mart_readonly(_q, database_url=database_url)


def get_fund_valuations(*, database_url: str | None = None) -> FundValuationsResponse:
    lines_sql = """
      with vintage as materialized (
        select v.fund_id, coalesce(v.fund_name, v.fund_id) as fund_name,
               to_char(v.report_period, 'YYYY-MM-DD') as report_period,
               v.holding_name, v.ticker, v.percent_of_net_assets, v.listing_id,
               coalesce(
                 lpad(nullif(ltrim(ei_v.cik, '0'), ''), 10, '0'),
                 lpad(substring(v.issuer_entity from '^(?:issuer|company):cik:0*([0-9]+)$'), 10, '0')
               ) as issuer_cik
        from mart.fund_holdings_valuation v
        left join mart.entity_identity ei_v on ei_v.entity_id::text = v.issuer_entity
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
    consolidation_sql = f"""
      select {_FUND_VIRTUAL_COLS}
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
                    weighted_gap=c.get("weighted_valuation_gap"),
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

    return with_mart_readonly(_q, database_url=database_url)


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


def _fetch_company_decision_from_cursor(
    cur: Any,
    candidates: list[str],
    legacy_by_entity: dict[str, str],
) -> dict[str, Any] | None:
    cur.execute(_COMPANY_DECISION_SQL, (candidates,))
    decision = cur.fetchone()
    if decision and decision.get("issuer_id") in legacy_by_entity:
        return {**decision, "issuer_id": legacy_by_entity[str(decision["issuer_id"])]}
    return dict(decision) if decision else None


def _fetch_company_themes_from_cursor(cur: Any, candidates: list[str]) -> list[dict[str, Any]]:
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
          where p.issuer_id = any(%s::text[])
            and p.partition_id is not null
            and {served_or_empty_head_predicate("p.run_id")}
          order by p.theme_id, p.cutoff desc, p.created_at desc
        ) latest_per_theme
        order by raw_share desc nulls last, theme asc
        """,
        (candidates,),
    )
    return [dict(r) for r in cur.fetchall()]


class PostgresCompanyProfileReader:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_company_profile(
        self,
        *,
        issuer_id: str,
        candidates: list[str] | tuple[str, ...] | None = None,
        include_decision: bool = True,
    ) -> dict[str, Any]:
        clean = issuer_id.strip()

        def _q(cur: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
            if candidates is not None:
                cand = list(candidates)
                legacy_by_entity: dict[str, str] = {}
            else:
                cand, legacy_by_entity = resolve_issuer_identity_from_cursor(cur, clean)
            decision: dict[str, Any] | None = None
            gppe: dict[str, Any] | None = None
            if include_decision:
                decision = _fetch_company_decision_from_cursor(cur, cand, legacy_by_entity)
                cur.execute(
                    f"""
                    select v.value::text as gppe, v.node_key as operating_branch,
                           v.confidence::text as confidence,
                           coalesce(v.availability_status, 'unavailable') as availability_status,
                           v.reason_codes
                    from mart.metric_node_values v
                    where v.issuer_id = any(%s::text[])
                      and v.node_key = any(%s::text[])
                      and {served_run_predicate("v.run_id")}
                    order by (v.availability_status = 'available') desc, v.period desc limit 1
                    """,
                    (cand, list(GPPE_VARIANT_KEYS)),
                )
                gppe = cur.fetchone()
            themes = _fetch_company_themes_from_cursor(cur, cand)
            return decision, gppe, themes

        decision_row, gppe_row, themes = with_mart_readonly(_q, database_url=self._database_url)
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

        issuers = with_mart_readonly(_load, database_url=self._database_url)
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
                select {_FUND_VIRTUAL_COLS}
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

        fund_row, raw_holdings = with_mart_readonly(_load, database_url=self._database_url)
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


__all__ = [
    "AnalystRatings",
    "EntityDetailResponse",
    "FundValuationDto",
    "FundValuationsResponse",
    "IssuerModuleUnavailable",
    "PostgresCompanyProfileReader",
    "PostgresEtfProfileReader",
    "PostgresThemePurityLeaderboardReader",
    "SupplyChainExposure",
    "THEME_LATEST_RUN_SQL",
    "THEME_ROWS_SQL",
    "ThemePurityGroupDto",
    "ThemePurityRowDto",
    "ThemesResponse",
    "TraceNodeValueDto",
    "TraceResponse",
    "ValuedHoldingRowDto",
    "_format_company_profile",
    "get_entity_detail",
    "get_fund_valuations",
    "get_themes_overview",
    "get_trace",
    "load_analyst_ratings",
    "load_supply_chain_exposure",
    "normalize_theme_slug",
    "resolve_fund_resolution_candidates",
]
