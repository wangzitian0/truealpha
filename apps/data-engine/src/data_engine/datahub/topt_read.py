"""Governed mart read for downstream consumers (#405 / #41 / #362).

The first Postgres-backed read that returns the materialized TOPT results and the run's quality
report from `mart` WITHOUT the caller hand-carrying a 7-part identity tuple: it resolves the
current head run and reads the typed mart rows. This is the read App/MCP use instead of the
`FixtureStrategyRunRepository` fixture. Reads only `mart`; never `raw`/`staging`.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


class PostgresToptReadRepository:
    """Bounded, read-only access to the materialized TOPT mart for downstream consumers."""

    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def current_run_id(self) -> str | None:
        """The current (latest by cutoff) complete production TOPT run — the governed head."""
        row = self._connection.execute(
            """
            select run_id from mart.topt_capture_status
            where environment = 'production' and complete
            order by cutoff desc, run_id desc limit 1
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
