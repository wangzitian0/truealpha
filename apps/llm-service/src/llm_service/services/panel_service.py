"""Panel domain services: universes, wide-panel input coverage, macro curve, and governed heads."""

from __future__ import annotations

from typing import Any

import psycopg
from pydantic import BaseModel, Field
from truealpha_contracts.environment_contract import EnvironmentContract
from truealpha_contracts.universes import id_prefix_like, key_of_lane, keys, lane_name, universe_with_role
from truealpha_runtime.access import AccessContext
from truealpha_runtime.issuer_modules_mart import ENTITY_DISPLAY_SQL, resolve_static_entity_display
from truealpha_runtime.research_report_mart import (
    POINTER_HEAD_SQL,
    MetricNodeValueReport,
    MetricNodeValueUnavailable,
    PostgresMetricNodeValueRepository,
)

from llm_service.config import settings
from llm_service.services._db import with_mart_readonly, with_ops_reader

SERVED_HEAD_SQL = "select run_id, freshness, availability from mart.served_head"
_UNDEFINED_TABLE = "42P01"


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
    from truealpha_runtime.research_report_mart import ACCEPTANCE_FALLBACK_HEAD_SQL

    cur.execute(POINTER_HEAD_SQL, (universe_like,))
    row = cur.fetchone()
    if row is not None:
        if row.get("availability") == "unavailable" or row.get("run_id") is None:
            reason = str(row.get("staleness_reason") or "").strip() or "older_than_30d"
            return None, f"head_unavailable: {reason}"
        return str(row["run_id"]), None
    cur.execute(ACCEPTANCE_FALLBACK_HEAD_SQL, (universe_like,))
    fallback = cur.fetchone()
    if fallback is None or fallback.get("run_id") is None:
        return None, "no accepted (quality-reported) strategy run"
    return str(fallback["run_id"]), None


def _id_prefix_like(role: str) -> str:
    return id_prefix_like(universe_with_role(role))  # type: ignore[arg-type]


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


def metric_node_values(
    *, node_key: str, issuer_id: str | None = None, limit: int = 100, database_url: str | None = None
) -> MetricNodeValueReport | MetricNodeValueUnavailable:
    repo = PostgresMetricNodeValueRepository(database_url=database_url or settings.database_url)
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
    except psycopg.Error as exc:
        if exc.sqlstate == _UNDEFINED_TABLE:
            return MacroPanelResponse(available=False, reason="Macro mart views not yet migrated")
        return MacroPanelResponse(available=False, reason=str(exc))


def load_universes_panel(*, database_url: str | None = None) -> UniverseOverviewResponse:
    def _q(cur: Any) -> UniverseOverviewResponse:
        cur.execute(
            """
            select r.kind, r.sequence, coalesce(r.note, '') as note, r.advanced_at::text as advanced_at,
                   o.payload->>'universe_id' as universe_id,
                   (o.payload->>'instrument_count')::int as instrument_count,
                   o.payload->>'instrument_mapping_sha256' as mapping_sha256
            from staging.accepted_rulesets r
            join staging.contract_objects o on o.contract_id = r.contract_id
            where r.kind = any(%s::text[])
            order by r.kind, r.sequence desc
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
        members: dict[str, list[dict[str, Any]]] = {}
        for head in heads:
            ukey = key_of_lane(str(head["kind"]))
            cur.execute(
                """
                select f.ticker, f.company_name, f.cik, f.figi, f.market_cap::text as market_cap,
                       f.as_of::text as as_of, f.source, r.payload_sha256 as fetch_sha256, r.object_uri
                from staging.etf_constituent_facts f
                join raw.fetches r on r.id = f.raw_fetch_id
                where f.etf_symbol = %s
                  and f.as_of = (select max(as_of) from staging.etf_constituent_facts where etf_symbol = %s)
                order by f.ticker
                """,
                (ukey, ukey),
            )
            members[str(head["kind"])] = [dict(r) for r in cur.fetchall()]
        return UniverseOverviewResponse(heads=heads, history=history, members=members)

    return with_ops_reader(_q, database_url=database_url)


def load_wide_panel_coverage(
    context: AccessContext,
    *,
    strategy_id: str = "large_model_value_v0",
    universe: str | None = None,
    cutoff: str | None = None,
    node_key: str | None = None,
    issuer_id: str | None = None,
    limit: int = 100,
    database_url: str | None = None,
) -> PanelCoverageResponse:
    from llm_service.services.strategy_service import get_strategy_run_page

    page = get_strategy_run_page(
        context,
        strategy_id=strategy_id,
        universe=universe,
        cutoff=cutoff,
        database_url=database_url,
    )
    node_report_dict: dict[str, Any] | None = None
    if node_key:
        node_report_dict = metric_node_values(
            node_key=node_key,
            issuer_id=issuer_id,
            limit=limit,
            database_url=database_url,
        ).model_dump(mode="json")

    decisions = [
        CoverageDecisionDto(
            issuer_id=d.issuer_id,
            label=d.label,
            cutoff_at=d.cutoff_at,
            outcome=d.outcome,
            eligible=d.eligible,
            exclusion_reason=d.exclusion_reason,
            exclusion_label=d.exclusion_label,
            trace_id=d.trace_id,
        )
        for d in page.decisions
    ]
    return PanelCoverageResponse(
        available=page.available,
        reason=page.reason,
        strategy_id=page.strategy_id,
        strategy_run_id=page.strategy_run_id,
        cutoff_at=page.cutoff_at,
        universe=page.universe,
        total_decisions=page.total_decisions,
        decisions=decisions,
        wide_panel=page.wide_panel,
        display_map=page.display_map,
        node_values_report=node_report_dict,
    )


__all__ = [
    "CoverageDecisionDto",
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
    "served_or_empty_head_predicate",
    "served_run_predicate",
]
