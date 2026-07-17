"""Postgres-backed `StrategyRunReadRepository` — see #361.

Reads `mart.strategy_runs`/`mart.strategy_decisions` (#355) instead of the
checked-in fixture `FixtureStrategyRunRepository` uses. This is the
Postgres-backed sibling `strategy_run_fixture.py`'s own docstring already
flagged as follow-up work, gated on #355 landing the real mart tables (done)
and a real writer populating them (still #26's own open gap — this
repository reports an honest `no_runs_recorded` until one exists).

Known schema gap: `mart.strategy_decisions` (#355's migration) has no
`confidence` column, unlike `StrategyRunDecision.confidence`. Rows read
through this repository always carry `confidence=None` until that column
exists — not something this module can silently patch, since the mart
migration belongs to #26/#355's own lane.
"""

from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from truealpha_contracts.access import AccessContext
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)

# Any of these mean the query failed, returned nothing, or returned rows that
# no longer match the DTO shape (schema drift) -- a caller-facing crash here
# would make this read boundary just as brittle as an unhandled fixture
# error would be (see strategy_run_fixture.py's own _FIXTURE_CORRUPTION_ERRORS
# for the same reasoning). get_latest() maps all of them to a structured
# StrategyRunUnavailable instead.
_ROW_VALIDATION_ERRORS = (KeyError, ValueError, TypeError, InvalidOperation, ValidationError)

_LATEST_RUN_SQL = """
    select strategy_run_id, corpus_sha256
    from mart.strategy_runs
    where strategy_key = %s
    order by executed_at desc, created_at desc, strategy_run_id desc
    limit 1
"""

_DECISIONS_SQL = """
    select issuer_id, cutoff_at, capital_adjusted_labor_efficiency, tier,
           current_price_to_sales, target_price_to_sales, valuation_gap,
           eligible, outcome, exclusion_reason, rank, target_weight
    from mart.strategy_decisions
    where strategy_run_id = %s
    order by cutoff_at, issuer_id
"""


def _decision_from_row(row: dict[str, Any]) -> StrategyRunDecision:
    return StrategyRunDecision(
        issuer_id=row["issuer_id"],
        cutoff_at=row["cutoff_at"],
        outcome=StrategyRunOutcome(row["outcome"]),
        eligible=row["eligible"],
        tier=ValuationTier(row["tier"]) if row["tier"] is not None else None,
        capital_adjusted_labor_efficiency=row["capital_adjusted_labor_efficiency"],
        current_price_to_sales=row["current_price_to_sales"],
        target_price_to_sales=row["target_price_to_sales"],
        valuation_gap=row["valuation_gap"],
        # #355's mart.strategy_decisions has no confidence column yet.
        confidence=None,
        exclusion_reason=row["exclusion_reason"],
        rank=row["rank"],
        target_weight=row["target_weight"],
    )


class PostgresStrategyRunRepository:
    """Reads the latest `mart.strategy_runs` row per `strategy_key`, real data only."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunReport | StrategyRunUnavailable:
        del context  # reserved for a future authorization decision; unused today
        try:
            with psycopg.connect(self._database_url, connect_timeout=5, autocommit=True) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(_LATEST_RUN_SQL, (strategy_id,))
                    run_row = cursor.fetchone()
                    if run_row is None:
                        return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")

                    cursor.execute(_DECISIONS_SQL, (run_row["strategy_run_id"],))
                    decision_rows = cursor.fetchall()
        except psycopg.Error:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="database_unavailable")

        try:
            return StrategyRunReport(
                strategy_id=strategy_id,  # type: ignore[arg-type]
                source="mart",
                corpus_sha256=run_row["corpus_sha256"],
                decisions=tuple(_decision_from_row(row) for row in decision_rows),
            )
        except _ROW_VALIDATION_ERRORS:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="schema_mismatch")
