"""Consumer-facing TOPT mart read (#405 / #41 / #362).

Reads the materialized TOPT GPPE results and the run's quality report from `mart` for downstream
consumers (MCP, App) — resolving the current complete production run so callers never hand-carry
a 7-part identity tuple. Reads only `mart`; the `mart_readonly` role enforces the boundary.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, ValidationError

# Any of these mean the query returned rows that no longer match the shape this
# repository expects (a renamed/unaliased column, a dropped field) -- a caller-facing
# crash here would take down the whole MCP tool call for a schema drift that should
# instead degrade to a typed Unavailable, the same reasoning
# truealpha_contracts.strategy_run_postgres's _ROW_VALIDATION_ERRORS documents (see
# truealpha#462: this repository shipped without that protection, so #461's
# target_run_id/run_id alias mismatch crashed every call instead of degrading).
_ROW_VALIDATION_ERRORS = (KeyError, ValueError, TypeError, ValidationError)


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
        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                # Resolve the governed head. run_id is a content hash, so `order by run_id`
                # picks the lexically-largest hash, not the newest run — it silently served
                # a stale run. Interim: gate on the quality report, the per-run acceptance
                # artifact, and take the most recently accepted run. The A1
                # `mart.current_pointer` is the eventual governed head, but it can only
                # reference runs registered in the evidence-graph plane; routing live
                # capture through that plane is tracked as the #405 consolidation
                # follow-up.
                # #378: the governed head is mart.current_pointer_head (ADR A1). The
                # acceptance-gated ORDER BY remains only as a fallback for databases
                # where no pointer has been advanced yet.
                head = conn.execute(
                    """
                    select target_run_id as run_id from mart.current_pointer_head
                    where environment = 'production' and factor_id = 'gross_profit_per_employee'
                    order by advanced_at desc limit 1
                    """
                ).fetchone()
                if head is None:
                    head = conn.execute(
                        """
                        select s.run_id
                        from mart.topt_capture_status s
                        join mart.datahub_quality_report q on q.run_id = s.run_id
                        where s.environment = 'production' and s.complete
                        order by q.created_at desc, q.report_id desc limit 1
                        """
                    ).fetchone()
                if head is None:
                    return ToptGppeUnavailable(reason="no accepted (quality-reported) production TOPT run")
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
                # The requested-cell denominator comes from the capture plane —
                # mart.topt_capture_status.obligation_count IS the run's requested
                # cells — never a constant: the hardcoded 84 made any run over a
                # different universe self-contradict its own quality payload
                # (#462 AC3; the TS twin applies the identical query).
                status_row = conn.execute(
                    "select obligation_count from mart.topt_capture_status where run_id = %s",
                    (run_id,),
                ).fetchone()
                if status_row is None:
                    return ToptGppeUnavailable(reason="no capture status for the governed run")
                cells = tuple(ToptGppeCell(**r) for r in rows)
                return ToptGppeReport(
                    run_id=run_id,
                    requested_count=status_row["obligation_count"],
                    available_count=sum(c.availability == "available" for c in cells),
                    cells=cells,
                    quality=report_row["payload"] if report_row else None,
                )
        except psycopg.Error:
            return ToptGppeUnavailable(reason="database_unavailable")
        except _ROW_VALIDATION_ERRORS:
            return ToptGppeUnavailable(reason="schema_mismatch")
