"""Data-engine-internal governed mart read (#405 / #41 / #362).

The first Postgres-backed read that returns the materialized TOPT results and the run's quality
report from `mart` WITHOUT the caller hand-carrying a 7-part identity tuple: it resolves the
current head run and reads the typed mart rows. Reads only `mart`; never `raw`/`staging`.

NOT what App/MCP use for this: that's `truealpha_contracts.topt_read.PostgresToptGppeRepository`
(a separate, independently-maintained implementation of the same governed-head resolution --
see truealpha#462 for the drift/consistency risk that split creates). This module exists for
data-engine's own internal reads; an earlier version of this docstring claimed App/MCP used it,
which was never true of the deployed path and misled reviewers into thinking this module's real
test coverage (apps/data-engine/tests/datahub/test_topt_read.py) covered the class MCP actually
calls, when it didn't.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


class PostgresToptReadRepository:
    """Bounded, read-only access to the materialized TOPT mart for downstream consumers."""

    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def current_run_id(self) -> str | None:
        """The current governed head: the most recently ACCEPTED complete production run.

        run_id is a content hash, so `order by run_id` picks the lexically-largest hash, not
        the newest run — that silently served a stale run. Gate on the quality report (the
        per-run acceptance artifact) and take the most recently accepted run. The A1
        `mart.current_pointer` is the eventual governed head; routing live capture into that
        evidence-graph plane so the pointer can advance is the #405 consolidation follow-up.
        """
        row = self._connection.execute(
            """
            select target_run_id from mart.current_pointer_head
            where environment = 'production' and factor_id = 'gross_profit_per_employee'
            order by advanced_at desc limit 1
            """
        ).fetchone()
        if row is None:  # fallback: no pointer advanced yet (#378)
            row = self._connection.execute(
                """
                select s.run_id
                from mart.topt_capture_status s
                join mart.datahub_quality_report q on q.run_id = s.run_id
                where s.environment = 'production' and s.complete
                order by q.created_at desc, q.report_id desc limit 1
                """
            ).fetchone()
        return None if row is None else row[0]

    def gppe_results(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """The GPPE results for a run: listing, availability, value, confidence — from mart."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self._connection.execute(
            """
            select payload->>'listing_id', payload->>'availability',
                   payload->>'gppe', payload->>'confidence'
            from mart.topt_gppe_results
            where payload->>'run_id' = %s
            order by payload->>'listing_id' limit %s
            """,
            (run_id, limit),
        ).fetchall()
        return [{"listing_id": r[0], "availability": r[1], "gppe": r[2], "confidence": r[3]} for r in rows]

    def quality_report(self, run_id: str) -> dict[str, Any] | None:
        """The persisted row-complete DataHub quality report for a run (or None)."""
        row = self._connection.execute(
            "select payload from mart.datahub_quality_report where run_id = %s order by created_at desc limit 1",
            (run_id,),
        ).fetchone()
        return None if row is None else row[0]
