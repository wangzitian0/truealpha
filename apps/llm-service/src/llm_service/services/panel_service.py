"""Panel domain services: universes, wide-panel input coverage, macro curve, and governed heads."""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from pydantic import BaseModel, Field
from truealpha_contracts.environment_contract import EnvironmentContract, UnknownUniverse, load_environment_contract
from truealpha_contracts.strategy import ExclusionReason
from truealpha_contracts.universes import id_prefix_like, key_of_lane, keys, lane_name, universes_with_role
from truealpha_runtime.access import AccessContext
from truealpha_runtime.issuer_modules_mart import (
    ENTITY_DISPLAY_SQL,
    _display_keys_for_row,
    resolve_static_entity_display,
)
from truealpha_runtime.research_report_mart import (
    POINTER_HEAD_SQL,
    MetricNodeValueReport,
    MetricNodeValueUnavailable,
    PostgresMetricNodeValueRepository,
)
from truealpha_runtime.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository, read_strategy_provenance

from llm_service.config import settings
from llm_service.services._db import with_mart_readonly

SERVED_HEAD_SQL = "select run_id, freshness, availability from mart.served_head"
DASHBOARD_STRATEGY_ID = "large_model_value_v0"
_EXCLUSION_LABELS: dict[str, str] = {
    reason.value: reason.value.replace("_", " ").capitalize() for reason in ExclusionReason
}


def served_run_predicate(column: str, universe_prefix: str | None = None) -> str:
    """Require a non-withheld non-canary `mart.served_head` match, failing closed when empty."""
    prefix_filter = f"starts_with(h.universe_id, '{universe_prefix}') and " if universe_prefix else ""
    return (
        f"{column} in (select h.run_id from mart.served_head h "
        f"where {prefix_filter}h.run_id is not null and not starts_with(h.universe_id, 'universe:canary-'))"
    )


def resolve_governed_head_run_id(cur: Any, universe_like: str) -> tuple[str | None, str | None]:
    """Resolve the governed served run id for `universe_like`, failing closed when unavailable."""
    cur.execute(POINTER_HEAD_SQL, (universe_like,))
    row = cur.fetchone()
    if row is None:
        return None, "no served head for strategy"
    if row.get("availability") == "unavailable" or row.get("run_id") is None:
        reason = str(row.get("staleness_reason") or "").strip() or "older_than_30d"
        return None, f"head_unavailable: {reason}"
    return str(row["run_id"]), None


def _id_prefix_like(role: str) -> str:
    return id_prefix_like(universes_with_role(role)[0])  # type: ignore[arg-type]


def _resolve_head_freshness(
    contract: EnvironmentContract, raw_freshness: str | None, universe_id: str, environment: str | None
) -> str:
    try:
        if not contract.is_head_scheduled(universe_id, environment):
            return "manual"
    except LookupError:
        pass
    if raw_freshness in {"fresh", "stale", "manual"}:
        return raw_freshness
    return "unknown"


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


class EntityDisplayItem(BaseModel):
    issuer_id: str
    ticker: str
    display_name: str | None = None
    label: str


class WidePanelCellDto(BaseModel):
    node_key: str
    period: str | None = None
    value: str | None = None
    confidence: str | None = None
    availability: str = "unavailable"


class MacroSeriesDto(BaseModel):
    series_id: str
    label: str
    unit: str
    source_id: str
    max_age_days: int
    latest: dict[str, Any] | None = None
    is_fresh: bool = False


class MacroPanelResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    series: list[MacroSeriesDto] = Field(default_factory=list)
    months: list[dict[str, Any]] = Field(default_factory=list)


class UniverseOverviewResponse(BaseModel):
    heads: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    members: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class CoverageDecisionDto(BaseModel):
    issuer_id: str
    label: str
    cutoff_at: str
    outcome: str
    eligible: bool
    exclusion_reason: str | None = None
    exclusion_label: str | None = None
    trace_id: str


class PanelCoverageResponse(BaseModel):
    available: bool
    reason: str | None = None
    strategy_id: str = "large_model_value_v0"
    strategy_run_id: str | None = None
    governed: bool = False
    head_age_hours: float | None = None
    head_limit_hours: float | None = None
    head_freshness: str | None = None
    cutoff_at: str | None = None
    universe: str | None = None
    total_decisions: int = 0
    decisions: list[CoverageDecisionDto] = Field(default_factory=list)
    wide_panel: dict[str, dict[str, WidePanelCellDto]] = Field(default_factory=dict)
    display_map: dict[str, EntityDisplayItem] = Field(default_factory=dict)
    node_values_report: dict[str, Any] | None = None


def _display_label(issuer_id: str, display_map: dict[str, EntityDisplayItem]) -> str:
    if item := display_map.get(issuer_id):
        return item.label
    return resolve_static_entity_display(issuer_id)[2]


def load_entity_display_items_from_cursor(cur: Any) -> dict[str, EntityDisplayItem]:
    """Load `mart.entity_identity` issuer display items indexed by canonical and legacy keys."""
    cur.execute(ENTITY_DISPLAY_SQL)
    out: dict[str, EntityDisplayItem] = {}
    for row in cur.fetchall():
        issuer_id = str(row.get("issuer_id") or "").strip()
        if not issuer_id:
            continue
        ticker = str(row.get("ticker") or "").strip()
        display_name = str(row.get("display_name") or "").strip() or None
        if ticker and display_name:
            label = display_name if display_name.startswith(f"{ticker} ·") else f"{ticker} · {display_name}"
        else:
            label = ticker or display_name or issuer_id
        item = EntityDisplayItem(issuer_id=issuer_id, ticker=ticker, display_name=display_name, label=label)
        for idx, key in enumerate(_display_keys_for_row(row)):
            if idx == 0:
                out[key] = item
            else:
                out.setdefault(key, item)
    return out


def load_entity_display_items(*, database_url: str | None = None) -> dict[str, EntityDisplayItem]:
    return with_mart_readonly(load_entity_display_items_from_cursor, database_url=database_url)


_PANEL_NODE_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue_total": ("revenue_total", "revenue"),
    "last_close": ("last_close", "price"),
}


def _load_wide_panel_from_cursor(cur: Any, node_keys: tuple[str, ...]) -> dict[str, dict[str, WidePanelCellDto]]:
    requested = set(node_keys)
    query_keys: list[str] = []
    seen_q: set[str] = set()
    for key in node_keys:
        for candidate in _PANEL_NODE_KEY_ALIASES.get(key, (key,)):
            if candidate not in seen_q:
                seen_q.add(candidate)
                query_keys.append(candidate)

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

    alias_targets = {
        "revenue": "revenue_total",
        "price": "last_close",
    }

    cur.execute(sql, (query_keys,))
    by_issuer: dict[str, dict[str, WidePanelCellDto]] = {}
    for row in cur.fetchall():
        iid = row.get("issuer_id")
        nkey = row.get("node_key")
        if not isinstance(iid, str) or not isinstance(nkey, str):
            continue
        bucket = by_issuer.setdefault(iid, {})
        targets = [nkey] if nkey in requested else []
        if (aliased := alias_targets.get(nkey)) and aliased in requested and aliased not in targets:
            targets.append(aliased)
        for target_key in targets:
            if target_key in bucket and target_key != nkey:
                continue
            if target_key in bucket and target_key == nkey:
                continue
            bucket[target_key] = WidePanelCellDto(
                node_key=target_key,
                period=row.get("period"),
                value=row.get("value"),
                confidence=row.get("confidence"),
                availability=str(row.get("availability_status") or "unavailable"),
            )
    return by_issuer


def _load_wide_panel(
    node_keys: tuple[str, ...], *, database_url: str | None = None
) -> dict[str, dict[str, WidePanelCellDto]]:
    return with_mart_readonly(lambda cur: _load_wide_panel_from_cursor(cur, node_keys), database_url=database_url)


def metric_node_values(
    *,
    node_key: str,
    issuer_id: str | None = None,
    limit: int = 100,
    metric_node_repository: Any | None = None,
    database_url: str | None = None,
) -> MetricNodeValueReport | MetricNodeValueUnavailable:
    repo = metric_node_repository or PostgresMetricNodeValueRepository(
        database_url=database_url or settings.database_url
    )
    return repo.latest(node_key=node_key, issuer_id=issuer_id, limit=limit)


def load_macro_curve(*, database_url: str | None = None) -> MacroPanelResponse:
    status_sql = (
        "select series_id, label, unit, source_id, max_age_days, "
        "to_char(observation_date, 'YYYY-MM-DD') as observation_day, value::text as value, "
        "source, fetched_at, age_days, is_fresh from mart.macro_series_status order by series_id"
    )
    months_sql = (
        "select series_id, to_char(observation_date, 'YYYY-MM-DD') as observation_day, value::text as value "
        "from mart.macro_series_current "
        "where observation_date >= ((now() at time zone 'UTC')::date - interval '12 months')::date "
        "order by observation_date desc, series_id"
    )

    def _q(cur: Any) -> MacroPanelResponse:
        cur.execute(status_sql)
        status_rows = cur.fetchall()
        cur.execute(months_sql)
        month_rows = cur.fetchall()
        if not status_rows:
            return MacroPanelResponse(available=False, reason="No macro series configured")
        series = [
            MacroSeriesDto(
                series_id=str(r["series_id"]),
                label=str(r["label"]),
                unit=str(r["unit"]),
                source_id=str(r["source_id"]),
                max_age_days=int(r["max_age_days"]),
                is_fresh=bool(r["is_fresh"]),
                latest=(
                    None
                    if r.get("observation_day") is None
                    else {
                        "observation_date": str(r["observation_day"])[:10],
                        "value": str(r["value"]),
                        "source": str(r["source"]),
                        "fetched_at": r["fetched_at"].isoformat()
                        if hasattr(r["fetched_at"], "isoformat")
                        else str(r["fetched_at"]),
                        "age_days": int(r["age_days"]),
                    }
                ),
            )
            for r in status_rows
        ]
        months = [
            {
                "series_id": str(r["series_id"]),
                "observation_date": str(r["observation_day"])[:10],
                "value": str(r["value"]),
            }
            for r in month_rows
        ]
        return MacroPanelResponse(series=series, months=months)

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.errors.UndefinedTable:
        return MacroPanelResponse(available=False, reason="Macro mart views not yet migrated")


def load_universes_panel(*, database_url: str | None = None) -> UniverseOverviewResponse:
    def _q(cur: Any) -> UniverseOverviewResponse:
        cur.execute(
            """
            select kind, sequence, note, advanced_at::text as advanced_at,
                   universe_id, instrument_count, mapping_sha256
            from mart.universe_heads
            where kind = any(%s::text[])
            order by kind, sequence desc
            """,
            ([lane_name(k) for k in keys()],),
        )
        history = [dict(r) for r in cur.fetchall()]
        heads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in history:
            kind = str(row["kind"])
            if kind not in seen:
                seen.add(kind)
                heads.append(row)
        members: dict[str, list[dict[str, Any]]] = {str(head["kind"]): [] for head in heads}
        if heads:
            ukey_to_kind = {key_of_lane(str(head["kind"])): str(head["kind"]) for head in heads}
            cur.execute(
                """
                select etf_symbol, ticker, company_name, cik, figi, market_cap::text as market_cap,
                       as_of::text as as_of, source, fetch_sha256
                from mart.universe_constituents
                where etf_symbol = any(%s::text[])
                order by etf_symbol, ticker
                """,
                (list(ukey_to_kind),),
            )
            for r in cur.fetchall():
                row_dict = dict(r)
                kind_name = ukey_to_kind.get(str(row_dict.pop("etf_symbol", "")))
                if kind_name is not None:
                    members[kind_name].append(row_dict)
        return UniverseOverviewResponse(heads=heads, history=history, members=members)

    return with_mart_readonly(_q, database_url=database_url)


def _normalize_coverage_universe(value: str | None) -> str:
    clean = (value or "").strip().lower()
    if not clean or clean == "all":
        return "all"
    contract = load_environment_contract()
    return clean if clean in contract.universes else "all"


def _matches_coverage_universe(universe_id: str | None, universe: str) -> bool:
    if universe == "all":
        return True
    if not universe_id:
        return False
    try:
        return load_environment_contract().universe_of_id(universe_id) == universe
    except UnknownUniverse:
        return False


def load_wide_panel_coverage(
    context: AccessContext,
    *,
    strategy_id: str = DASHBOARD_STRATEGY_ID,
    universe: str | None = None,
    cutoff: str | None = None,
    node_key: str | None = None,
    issuer_id: str | None = None,
    limit: int = 100,
    metric_node_repository: Any | None = None,
    database_url: str | None = None,
) -> PanelCoverageResponse:
    db_url = database_url or settings.database_url
    norm_universe = _normalize_coverage_universe(universe)
    node_report_dict: dict[str, Any] | None = None
    if node_key:
        node_report_dict = metric_node_values(
            node_key=node_key,
            issuer_id=issuer_id,
            limit=limit,
            metric_node_repository=metric_node_repository,
            database_url=database_url,
        ).model_dump(mode="json")

    repo = PostgresStrategyRunRepository(database_url=db_url)

    def _q(
        cur: Any,
    ) -> tuple[
        StrategyRunReport | StrategyRunUnavailable,
        dict[str, EntityDisplayItem],
        dict[str, dict[str, Any]],
        dict[str, dict[str, WidePanelCellDto]],
    ]:
        rep = repo.get_latest_from_cursor(
            cur,
            strategy_id=strategy_id,
            context=context,
            universe=norm_universe,
        )
        if isinstance(rep, StrategyRunUnavailable):
            return rep, {}, {}, {}
        d_map = load_entity_display_items_from_cursor(cur)
        # Read provenance only when filtering to a specific universe; universe='all' keeps all rows.
        p_map: dict[str, dict[str, Any]] = (
            read_strategy_provenance(cur, rep.strategy_run_id) if norm_universe != "all" and rep.strategy_run_id else {}
        )
        w_panel = _load_wide_panel_from_cursor(cur, WIDE_PANEL_INPUT_KEYS)
        return rep, d_map, p_map, w_panel

    try:
        report, display_map, prov_map, wide_panel = with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error:
        report = StrategyRunUnavailable(strategy_id=strategy_id, reason="database_unavailable")
        display_map, prov_map, wide_panel = {}, {}, {}

    if isinstance(report, StrategyRunUnavailable):
        return PanelCoverageResponse(
            available=False,
            reason=report.reason,
            strategy_id=strategy_id,
            universe=norm_universe,
            node_values_report=node_report_dict,
        )
    target_cutoff = cutoff or max((d.cutoff_at.isoformat() for d in report.decisions), default=None)
    filtered = [
        d
        for d in report.decisions
        if (target_cutoff is None or d.cutoff_at.isoformat() == target_cutoff)
        and _matches_coverage_universe(prov_map.get(d.issuer_id, {}).get("universe_id"), norm_universe)
    ]
    ordered = sorted(filtered, key=lambda d: (0, d.rank, d.issuer_id) if d.rank is not None else (1, 0, d.issuer_id))

    decisions: list[CoverageDecisionDto] = []
    for d in ordered:
        excl = d.exclusion_reason.value if isinstance(d.exclusion_reason, ExclusionReason) else d.exclusion_reason
        trace_payload = f"{report.source}:{report.corpus_sha256[:12]}:{d.issuer_id}:{d.cutoff_at.isoformat()}"
        decisions.append(
            CoverageDecisionDto(
                issuer_id=d.issuer_id,
                label=_display_label(d.issuer_id, display_map),
                cutoff_at=d.cutoff_at.isoformat(),
                outcome=d.outcome.value,
                eligible=d.eligible,
                exclusion_reason=excl,
                exclusion_label=_EXCLUSION_LABELS.get(excl) if excl else None,
                trace_id=f"trace:{hashlib.sha256(trace_payload.encode()).hexdigest()[:16]}",
            )
        )
    return PanelCoverageResponse(
        available=True,
        strategy_id=report.strategy_id,
        strategy_run_id=report.strategy_run_id,
        governed=bool(getattr(report, "governed", False)),
        head_age_hours=getattr(report, "head_age_hours", None),
        head_limit_hours=getattr(report, "head_limit_hours", None),
        head_freshness=getattr(report, "head_freshness", None),
        cutoff_at=target_cutoff,
        universe=norm_universe,
        total_decisions=len(decisions),
        decisions=decisions,
        wide_panel=wide_panel,
        display_map=display_map,
        node_values_report=node_report_dict,
    )


__all__ = [
    "CoverageDecisionDto",
    "DASHBOARD_STRATEGY_ID",
    "ENTITY_DISPLAY_SQL",
    "EntityDisplayItem",
    "GPPE_VARIANT_KEYS",
    "MacroPanelResponse",
    "MacroSeriesDto",
    "MetricNodeValueReport",
    "MetricNodeValueUnavailable",
    "PanelCoverageResponse",
    "POINTER_HEAD_SQL",
    "RANKING_PANEL_INPUT_KEYS",
    "SERVED_HEAD_SQL",
    "UniverseOverviewResponse",
    "WIDE_PANEL_INPUT_KEYS",
    "WidePanelCellDto",
    "_display_label",
    "_id_prefix_like",
    "_load_wide_panel",
    "_resolve_head_freshness",
    "load_entity_display_items",
    "load_macro_curve",
    "load_universes_panel",
    "load_wide_panel_coverage",
    "metric_node_values",
    "resolve_governed_head_run_id",
    "served_run_predicate",
]
