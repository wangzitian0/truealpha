"""Postgres-backed `StrategyRunReadRepository` (#361)."""

from __future__ import annotations

from datetime import UTC
from decimal import InvalidOperation
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from truealpha_contracts.access import AccessContext
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy_run import (
    StrategyRunDecision,
    StrategyRunDefinition,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)
from truealpha_contracts.universes import id_prefix_like, universe_with_role

_ROW_VALIDATION_ERRORS = (KeyError, ValueError, TypeError, InvalidOperation, ValidationError)

LATEST_RUN_SQL = """
    select r.strategy_run_id, r.corpus_sha256, r.executed_at,
           exists (
               select 1 from mart.served_head h
               join mart.strategy_run_capture b on b.capture_run_id = h.run_id
               where b.strategy_run_id = r.strategy_run_id
                 and h.factor_id = 'gross_profit_per_employee'
                 and h.universe_id like %s
           ) as is_governed,
           r.strategy_version, r.definition_content_sha256
    from mart.strategy_runs r
    where r.strategy_key = %s
    order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc
    limit 1
"""
_LATEST_RUN_SQL = LATEST_RUN_SQL


def latest_run_params(strategy_key: str) -> tuple[str, str]:
    return (id_prefix_like(universe_with_role("strategy")), strategy_key)


DECISIONS_FROM_SQL = """
    from mart.strategy_decisions d
    left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
    left join mart.topt_core_results t
      on t.run_id = scope.capture_run_id and t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
    left join lateral (
        select ei.legacy_id
        from mart.entity_identity ei
        where ei.entity_id::text = d.issuer_id
        limit 1
    ) ei on true
    where d.strategy_run_id = %s
    order by d.cutoff_at, coalesce(ei.legacy_id, d.issuer_id)
"""
_DECISIONS_SQL = (
    """
    select coalesce(ei.legacy_id, d.issuer_id) as issuer_id,
           d.cutoff_at, d.capital_adjusted_labor_efficiency, d.tier,
           d.current_price_to_sales, d.target_price_to_sales,
           d.current_price_to_gross_profit, d.target_price_to_gross_profit, d.valuation_gap,
           d.eligible, d.outcome, d.exclusion_reason, d.rank, d.target_weight, d.peg, d.peg_rank,
           d.labor_efficiency_metric, t.confidence
"""
    + DECISIONS_FROM_SQL
)


def _decision_from_row(row: dict[str, Any]) -> StrategyRunDecision:
    return StrategyRunDecision(
        issuer_id=row["issuer_id"],
        cutoff_at=row["cutoff_at"].astimezone(UTC),
        outcome=StrategyRunOutcome(row["outcome"]),
        eligible=row["eligible"],
        tier=ValuationTier(row["tier"]) if row["tier"] is not None else None,
        capital_adjusted_labor_efficiency=row["capital_adjusted_labor_efficiency"],
        current_price_to_sales=row["current_price_to_sales"],
        target_price_to_sales=row["target_price_to_sales"],
        current_price_to_gross_profit=row["current_price_to_gross_profit"],
        target_price_to_gross_profit=row["target_price_to_gross_profit"],
        valuation_gap=row["valuation_gap"],
        confidence=row["confidence"],
        exclusion_reason=row["exclusion_reason"],
        rank=row["rank"],
        target_weight=row["target_weight"],
        peg=row["peg"],
        peg_rank=row["peg_rank"],
        labor_efficiency_metric=row["labor_efficiency_metric"],
    )


class PostgresStrategyRunRepository:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunReport | StrategyRunUnavailable:
        del context
        try:
            with psycopg.connect(self._database_url, connect_timeout=5, autocommit=True) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(_LATEST_RUN_SQL, latest_run_params(strategy_id))
                    run_row = cursor.fetchone()
                    if run_row is None:
                        return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")
                    cursor.execute(_DECISIONS_SQL, (run_row["strategy_run_id"],))
                    decision_rows = cursor.fetchall()
                    if strategy_id != "large_model_value_v0":
                        return StrategyRunUnavailable(strategy_id=strategy_id, reason="schema_mismatch")
                    if not decision_rows:
                        return StrategyRunUnavailable(strategy_id=strategy_id, reason="empty_decisions")
        except psycopg.Error:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="database_unavailable")

        try:
            return StrategyRunReport(
                strategy_id=strategy_id,  # type: ignore[arg-type]
                source="mart",
                corpus_sha256=run_row["corpus_sha256"],
                decisions=tuple(_decision_from_row(row) for row in decision_rows),
                strategy_run_id=run_row["strategy_run_id"],
                governed=bool(run_row["is_governed"]),
                definition=StrategyRunDefinition.resolve(
                    version=run_row["strategy_version"],
                    content_sha256=run_row["definition_content_sha256"],
                ),
            )
        except _ROW_VALIDATION_ERRORS:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="schema_mismatch")
