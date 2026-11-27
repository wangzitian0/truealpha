"""Postgres-backed `StrategyRunReadRepository` (#361)."""

from __future__ import annotations

from datetime import UTC
from decimal import InvalidOperation
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.universes import id_prefix_like, universes_with_role

from truealpha_runtime.access import AccessContext
from truealpha_runtime.strategy_run import (
    StrategyRunDecision,
    StrategyRunDefinition,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)

_ROW_VALIDATION_ERRORS = (KeyError, ValueError, TypeError, InvalidOperation, ValidationError)

LATEST_RUN_SQL = """
    with head_scope as (
        select h.run_id, h.advanced_at, h.universe_id, h.universe_version,
               h.age_hours, h.limit_hours, h.freshness, h.availability
        from mart.served_head h
        where h.factor_id = 'gross_profit_per_employee'
          and h.universe_id like any(%s::text[])
    ),
    chosen_run as (
        select r.strategy_run_id, r.corpus_sha256, r.executed_at,
               true as is_governed,
               r.strategy_version, r.definition_content_sha256
        from mart.strategy_runs r
        where r.strategy_key = %s
          and exists (
              select 1 from head_scope hs
              join mart.strategy_run_capture b on b.capture_run_id = hs.run_id
              where hs.run_id is not null
                and b.strategy_run_id = r.strategy_run_id
          )
        order by r.executed_at desc, r.created_at desc, r.strategy_run_id desc
        limit 1
    ),
    top_head as (
        select hs.run_id, hs.age_hours, hs.limit_hours, hs.freshness, hs.availability
        from head_scope hs
        order by (hs.run_id is not null) desc, hs.advanced_at desc, hs.universe_id desc, hs.universe_version desc
        limit 1
    )
    select cr.strategy_run_id,
           cr.corpus_sha256,
           cr.executed_at,
           coalesce(cr.is_governed, false) as is_governed,
           cr.strategy_version,
           cr.definition_content_sha256,
           th.run_id as head_run_id,
           th.availability as head_availability,
           matched.age_hours as head_age_hours,
           matched.limit_hours as head_limit_hours,
           matched.freshness as head_freshness
    from (select 1 as anchor) a
    left join top_head th on true
    left join chosen_run cr on true
    left join lateral (
        select hs.age_hours, hs.limit_hours, hs.freshness
        from head_scope hs
        join mart.strategy_run_capture b on b.capture_run_id = hs.run_id
        where hs.run_id is not null
          and b.strategy_run_id = cr.strategy_run_id
        order by hs.advanced_at desc, hs.universe_id desc, hs.universe_version desc
        limit 1
    ) matched on true
    where th.availability is not null or cr.strategy_run_id is not null
"""


_PEG_CONSENSUS_LATERAL = """
    left join lateral (
        select mc.value, mc.reason_codes
        from mart.metric_node_values mc
        where mc.run_id = scope.capture_run_id
          and mc.issuer_id = d.issuer_id
          and mc.node_key = 'peg_consensus'
        order by mc.period desc, mc.node_id
        limit 1
    ) peg_c on true
    left join lateral (
        select mcr.value
        from mart.metric_node_values mcr
        where mcr.run_id = scope.capture_run_id
          and mcr.issuer_id = d.issuer_id
          and mcr.node_key = 'peg_consensus_rank'
        order by mcr.period desc, mcr.node_id
        limit 1
    ) peg_cr on true"""

_PEG_GEOMETRIC_LATERAL = """
    left join lateral (
        select mc.value, mc.reason_codes
        from mart.metric_node_values mc
        where mc.run_id = scope.capture_run_id
          and mc.issuer_id = d.issuer_id
          and mc.node_key = 'peg_geometric_cagr'
        order by mc.period desc, mc.node_id
        limit 1
    ) peg_g on true
    left join lateral (
        select mcr.value
        from mart.metric_node_values mcr
        where mcr.run_id = scope.capture_run_id
          and mcr.issuer_id = d.issuer_id
          and mcr.node_key = 'peg_geometric_cagr_rank'
        order by mcr.period desc, mcr.node_id
        limit 1
    ) peg_gr on true"""


def _decisions_from_sql(convention: str) -> str:
    """Build the FROM/JOIN clause, omitting unused PEG laterals for `historical_cagr` and `guidance`."""
    peg_joins: list[str] = []
    if convention in {"all", "consensus"}:
        peg_joins.append(_PEG_CONSENSUS_LATERAL)
    if convention in {"all", "geometric_cagr"}:
        peg_joins.append(_PEG_GEOMETRIC_LATERAL)
    return (
        """
    from mart.strategy_decisions d
    left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
    left join lateral (
        select m.confidence
        from mart.metric_node_values m
        where m.run_id = scope.capture_run_id
          and m.issuer_id = d.issuer_id
          and m.node_key = coalesce(d.labor_efficiency_metric, 'gppe_uniform_charge_v0')
        order by m.period desc, m.node_id
        limit 1
    ) m on true"""
        + "".join(peg_joins)
        + """
    left join lateral (
        select ei.legacy_id from mart.entity_identity ei where ei.entity_id::text = d.issuer_id limit 1
    ) ei on true
    where d.strategy_run_id = %s
    order by d.cutoff_at, coalesce(ei.legacy_id, d.issuer_id)
"""
    )


DECISIONS_FROM_SQL = _decisions_from_sql("all")


def _decisions_sql(convention: str) -> str:
    """Build the `strategy_decisions` query, joining PEG metric laterals only for conventions that read them."""
    peg_selects: list[str] = []
    if convention in {"all", "consensus"}:
        peg_selects.append(
            " peg_c.value as peg_consensus, peg_cr.value as peg_consensus_rank,"
            " peg_c.reason_codes as peg_consensus_reason_codes,"
        )
    if convention in {"all", "geometric_cagr"}:
        peg_selects.append(
            " peg_g.value as peg_geometric_cagr, peg_gr.value as peg_geometric_cagr_rank,"
            " peg_g.reason_codes as peg_geometric_cagr_reason_codes,"
        )
    return (
        "select coalesce(ei.legacy_id, d.issuer_id) as issuer_id, d.cutoff_at,"
        " d.capital_adjusted_labor_efficiency, d.tier, d.current_price_to_sales, d.target_price_to_sales,"
        " d.current_price_to_gross_profit, d.target_price_to_gross_profit, d.valuation_gap,"
        " d.eligible, d.outcome, d.exclusion_reason, d.availability_status,"
        " d.source_evidence_status, d.peg_reason_codes,"
        " d.rank, d.target_weight, d.peg, d.peg_rank,"
        + "".join(peg_selects)
        + " d.labor_efficiency_metric, m.confidence "
        + _decisions_from_sql(convention)
    )


def read_strategy_provenance(cursor: Any, strategy_run_id: str) -> dict[str, dict[str, Any]]:
    cursor.execute(
        """
        select coalesce(ei.legacy_id, d.issuer_id) as issuer_id,
               to_char(t.operating_period_end, 'YYYY-MM-DD') as operating_period_end,
               to_char(t.revenue_period_end, 'YYYY-MM-DD') as revenue_period_end,
               to_char(t.shares_period_end, 'YYYY-MM-DD') as shares_period_end,
               t.universe_id, t.universe_version, t.universe_sha256,
               t.gppe_definition_sha256,
               sr.definition_content_sha256 as tier_definition_sha256
        from mart.strategy_decisions d
        join mart.strategy_runs sr on sr.strategy_run_id = d.strategy_run_id
        left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
        left join mart.capture_issuer_lineage t
          on t.run_id = scope.capture_run_id and t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
        left join lateral (
          select ei.legacy_id from mart.entity_identity ei
          where ei.entity_id::text = d.issuer_id order by ei.legacy_id limit 1
        ) ei on true
        where d.strategy_run_id = %s
        """,
        (strategy_run_id,),
    )
    return {str(r["issuer_id"]): dict(r) for r in cursor.fetchall() if r.get("issuer_id")}


def read_trace_lineage(
    cursor: Any, strategy_run_id: str, issuer_id: str, cutoff_at: str
) -> tuple[str | None, str | None, Any]:
    cursor.execute(
        """
        select m.result_id, m.listing_id, m.lineage
        from mart.strategy_decisions d
        join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
        join mart.capture_issuer_lineage m
          on m.run_id = scope.capture_run_id and m.issuer_id = d.issuer_id and m.cutoff = d.cutoff_at
        left join lateral (
          select ei.legacy_id from mart.entity_identity ei
          where ei.entity_id::text = d.issuer_id order by ei.legacy_id limit 1
        ) ei on true
        where d.strategy_run_id = %s
          and (d.issuer_id = %s or coalesce(ei.legacy_id, d.issuer_id) = %s)
          and d.cutoff_at = %s::timestamptz
        order by m.result_id
        limit 1
        """,
        (strategy_run_id, issuer_id, issuer_id, cutoff_at),
    )
    row = cursor.fetchone()
    if row is None:
        return None, None, None
    return (
        str(row["result_id"]) if row.get("result_id") else None,
        str(row["listing_id"]) if row.get("listing_id") else None,
        row.get("lineage"),
    )


def latest_run_params(strategy_key: str, *, universe: str | None = None) -> tuple[list[str], str]:
    clean = (universe or "").strip().lower()
    if clean and clean != "all":
        return ([id_prefix_like(clean)], strategy_key)
    return ([id_prefix_like(k) for k in universes_with_role("strategy")], strategy_key)


def _normalize_convention(convention: str | None) -> str:
    if convention in {"consensus", "analyst_consensus"}:
        return "consensus"
    if convention in {"guidance", "company_guidance"}:
        return "guidance"
    if convention in {"geometric_cagr", "cagr_3y"}:
        return "geometric_cagr"
    return "historical_cagr"


def _decision_from_row(row: dict[str, Any], *, convention: str = "historical_cagr") -> StrategyRunDecision:
    data = dict(row)
    peg_consensus = data.pop("peg_consensus", None)
    peg_consensus_rank = data.pop("peg_consensus_rank", None)
    peg_consensus_reasons = data.pop("peg_consensus_reason_codes", None)
    peg_geometric = data.pop("peg_geometric_cagr", None)
    peg_geometric_rank = data.pop("peg_geometric_cagr_rank", None)
    peg_geometric_reasons = data.pop("peg_geometric_cagr_reason_codes", None)
    if convention == "consensus":
        data["peg"] = peg_consensus
        data["peg_rank"] = int(peg_consensus_rank) if peg_consensus_rank is not None else None
        if peg_consensus is not None:
            data["peg_reason_codes"] = ()
        elif peg_consensus_reasons is not None:
            data["peg_reason_codes"] = peg_consensus_reasons
        else:
            data["peg_reason_codes"] = ("growth_convention_unsourced:analyst_consensus",)
    elif convention == "geometric_cagr":
        data["peg"] = peg_geometric
        data["peg_rank"] = int(peg_geometric_rank) if peg_geometric_rank is not None else None
        if peg_geometric is not None:
            data["peg_reason_codes"] = ()
        elif peg_geometric_reasons is not None:
            data["peg_reason_codes"] = peg_geometric_reasons
        else:
            data["peg_reason_codes"] = ("insufficient_earnings_history",)
    elif convention == "guidance":
        data["peg"] = None
        data["peg_rank"] = None
        data["peg_reason_codes"] = ("growth_convention_unsourced:company_guidance",)
    peg_reason_codes = tuple(str(code) for code in (data.pop("peg_reason_codes", None) or ()))
    return StrategyRunDecision(
        **(
            data
            | {
                "cutoff_at": data["cutoff_at"].astimezone(UTC),
                "outcome": StrategyRunOutcome(data["outcome"]),
                "tier": ValuationTier(data["tier"]) if data["tier"] is not None else None,
                "availability_status": (
                    AvailabilityStatus(data["availability_status"])
                    if data.get("availability_status") is not None
                    else None
                ),
                "peg_reason_codes": peg_reason_codes,
            }
        )
    )


class PostgresStrategyRunRepository:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_latest_from_cursor(
        self,
        cursor: Any,
        *,
        strategy_id: str,
        context: AccessContext,
        universe: str | None = None,
        convention: str | None = None,
    ) -> StrategyRunReport | StrategyRunUnavailable:
        """Read the latest governed strategy run on an already-pooled read-only cursor."""
        del context
        norm_convention = _normalize_convention(convention)
        cursor.execute(LATEST_RUN_SQL, latest_run_params(strategy_id, universe=universe))
        run_row = cursor.fetchone()
        if run_row is None:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")
        if run_row.get("head_availability") is not None and run_row.get("head_run_id") is None:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="head_withheld")
        if run_row.get("strategy_run_id") is None:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")
        head_age_hours = float(run_row["head_age_hours"]) if run_row.get("head_age_hours") is not None else None
        head_limit_hours = float(run_row["head_limit_hours"]) if run_row.get("head_limit_hours") is not None else None
        head_freshness = str(run_row["head_freshness"]) if run_row.get("head_freshness") is not None else None
        cursor.execute(_decisions_sql(norm_convention), (run_row["strategy_run_id"],))
        decision_rows = cursor.fetchall()
        if strategy_id != "large_model_value_v0":
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="schema_mismatch")
        if not decision_rows:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="empty_decisions")
        try:
            return StrategyRunReport(
                strategy_id=strategy_id,  # type: ignore[arg-type]
                source="mart",
                corpus_sha256=run_row["corpus_sha256"],
                decisions=tuple(_decision_from_row(row, convention=norm_convention) for row in decision_rows),
                strategy_run_id=run_row["strategy_run_id"],
                governed=bool(run_row["is_governed"]),
                definition=StrategyRunDefinition.resolve(
                    version=run_row["strategy_version"],
                    content_sha256=run_row["definition_content_sha256"],
                ),
                head_age_hours=head_age_hours,
                head_limit_hours=head_limit_hours,
                head_freshness=head_freshness,
            )
        except _ROW_VALIDATION_ERRORS:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="schema_mismatch")

    def get_latest(
        self,
        *,
        strategy_id: str,
        context: AccessContext,
        universe: str | None = None,
        convention: str | None = None,
    ) -> StrategyRunReport | StrategyRunUnavailable:
        try:
            with (
                psycopg.connect(self._database_url, connect_timeout=5) as connection,
                connection.cursor(row_factory=dict_row) as cursor,
            ):
                cursor.execute("set local role mart_readonly")
                cursor.execute("select set_config('statement_timeout', '5s', true)")
                result = self.get_latest_from_cursor(
                    cursor,
                    strategy_id=strategy_id,
                    context=context,
                    universe=universe,
                    convention=convention,
                )
                cursor.execute("reset role")
                return result
        except psycopg.Error:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="database_unavailable")
