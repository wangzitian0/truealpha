"""Postgres-backed `StrategyRunReadRepository` — see #361.

Reads `mart.strategy_runs`/`mart.strategy_decisions` (#355) instead of the
checked-in fixture `FixtureStrategyRunRepository` uses. This is the
Postgres-backed sibling `strategy_run_fixture.py`'s own docstring already
flagged as follow-up work, gated on #355 landing the real mart tables (done)
and a real writer populating them (still #26's own open gap — this
repository reports an honest `no_runs_recorded` until one exists).

`mart.strategy_decisions` (#355's migration) has no `confidence` column;
`confidence` is read from `mart.topt_core_results` on (issuer_id, cutoff),
the same join the TypeScript twin makes, so MCP and the web report one number.

Which run is "latest" is decided by the governed capture head (#575), see
`LATEST_RUN_SQL`; the newest recorded run is only a fallback.
"""

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

# The run the governed capture head resolves to comes first (#575); only when no head
# resolves a run for this strategy — a fresh database, a preview run, a fixture — does
# the newest recorded run stand in. `mart.governed_strategy_run` holds the join (the
# head's snapshot cutoff is the strategy run's executed_at, by construction of the
# tick); the twins only rank by it. The TypeScript twin carries the same statement
# modulo placeholder syntax, and test_strategy_run_selection_parity pins the two texts.
LATEST_RUN_SQL = """
    select r.strategy_run_id, r.corpus_sha256, r.executed_at,
           exists (select 1 from mart.governed_strategy_run g
                   where g.strategy_run_id = r.strategy_run_id) as is_governed
    from mart.strategy_runs r
    where r.strategy_key = %s
    order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc
    limit 1
"""
_LATEST_RUN_SQL = LATEST_RUN_SQL

# `confidence` is joined from mart.topt_core_results on (issuer_id, cutoff), exactly as
# the TypeScript twin does: mart.strategy_decisions has no confidence column (#355), and
# the Python twin hard-coded None while the web rendered 0.85 — the two surfaces
# disagreed on the same decision. A join is a read, not a computation.
_DECISIONS_SQL = """
    select d.issuer_id, d.cutoff_at, d.capital_adjusted_labor_efficiency, d.tier,
           d.current_price_to_sales, d.target_price_to_sales, d.valuation_gap,
           d.eligible, d.outcome, d.exclusion_reason, d.rank, d.target_weight, d.peg, d.peg_rank,
           t.confidence
    from mart.strategy_decisions d
    left join mart.topt_core_results t
      on t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
    where d.strategy_run_id = %s
    order by d.cutoff_at, d.issuer_id
"""


def _decision_from_row(row: dict[str, Any]) -> StrategyRunDecision:
    return StrategyRunDecision(
        issuer_id=row["issuer_id"],
        # psycopg returns timestamptz in the SESSION's timezone; without this
        # normalization the serialized report (and every trace ID derived from
        # it) varies with the server's TZ setting — invisible on UTC CI, wrong
        # everywhere else. The TS twin normalizes in SQL (at time zone 'UTC');
        # the parity conformance fixture pins both to the same bytes (#469).
        cutoff_at=row["cutoff_at"].astimezone(UTC),
        outcome=StrategyRunOutcome(row["outcome"]),
        eligible=row["eligible"],
        tier=ValuationTier(row["tier"]) if row["tier"] is not None else None,
        capital_adjusted_labor_efficiency=row["capital_adjusted_labor_efficiency"],
        current_price_to_sales=row["current_price_to_sales"],
        target_price_to_sales=row["target_price_to_sales"],
        valuation_gap=row["valuation_gap"],
        # From mart.topt_core_results (same join as the TypeScript twin); None when the
        # decision has no core result at its cutoff, e.g. a fixture or preview run.
        confidence=row["confidence"],
        exclusion_reason=row["exclusion_reason"],
        rank=row["rank"],
        target_weight=row["target_weight"],
        # Module 1 (#284), recorded but not selecting. Both twins must carry it or the
        # parity gate diverges — which is exactly how this was caught.
        peg=row["peg"],
        peg_rank=row["peg_rank"],
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
