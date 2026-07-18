"""Consumer-facing TOPT mart read (#405 / #41 / #362).

Reads the materialized TOPT GPPE results and the run's quality report from `mart` for downstream
consumers (MCP, App) — resolving the current complete production run so callers never hand-carry
a 7-part identity tuple. Reads only `mart`; the `mart_readonly` role enforces the boundary.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict


class ToptGppeCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    listing_id: str
    availability: str
    gppe: str | None
    confidence: str | None


class ToptGppeReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    requested_count: int
    available_count: int
    cells: tuple[ToptGppeCell, ...]
    quality: dict[str, Any] | None = None


class ToptGppeUnavailable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


class PostgresToptGppeRepository:
    """Reads mart.topt_gppe_results + the quality report for the current production run."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def latest(self, *, limit: int = 100) -> ToptGppeReport | ToptGppeUnavailable:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
            head = conn.execute(
                """
                select run_id from mart.topt_capture_status
                where environment = 'production' and complete
                order by cutoff desc, run_id desc limit 1
                """
            ).fetchone()
            if head is None:
                return ToptGppeUnavailable(reason="no complete production TOPT run")
            run_id = head["run_id"]
            rows = conn.execute(
                """
                select payload->>'listing_id' as listing_id,
                       payload->>'availability' as availability,
                       payload->>'gppe' as gppe,
                       payload->>'confidence' as confidence
                from mart.topt_gppe_results
                where payload->>'run_id' = %s
                order by payload->>'listing_id' limit %s
                """,
                (run_id, limit),
            ).fetchall()
            report_row = conn.execute(
                "select payload from mart.datahub_quality_report where run_id = %s order by created_at desc limit 1",
                (run_id,),
            ).fetchone()
        cells = tuple(ToptGppeCell(**r) for r in rows)
        return ToptGppeReport(
            run_id=run_id,
            requested_count=84,
            available_count=sum(c.availability == "available" for c in cells),
            cells=cells,
            quality=report_row["payload"] if report_row else None,
        )
