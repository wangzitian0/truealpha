"""Consumer-facing TOPT mart read (#405 / #41 / #362).

Reads the materialized TOPT GPPE results and the run's quality report from `mart` for downstream
consumers (MCP, App) — resolving the current complete production run so callers never hand-carry
a 7-part identity tuple. Reads only `mart`; the `mart_readonly` role enforces the boundary.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from truealpha_contracts.confidence_bands import confidence_band
from truealpha_contracts.issuer_modules_mart import resolve_static_entity_display
from truealpha_contracts.universes import id_prefix_like, universe_with_role

# Any of these mean the query returned rows that no longer match the shape this
# repository expects (a renamed/unaliased column, a dropped field) -- a caller-facing
# crash here would take down the whole MCP tool call for a schema drift that should
# instead degrade to a typed Unavailable, the same reasoning
# truealpha_contracts.strategy_run_postgres's _ROW_VALIDATION_ERRORS documents (see
# truealpha#462: this repository shipped without that protection, so #461's
# target_run_id/run_id alias mismatch crashed every call instead of degrading).
_ROW_VALIDATION_ERRORS = (KeyError, ValueError, TypeError, ValidationError)


def _round_gppe_display(gppe: str | None) -> str | None:
    if gppe is None:
        return None
    try:
        return str(Decimal(gppe).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return gppe


class ToptGppeCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    listing_id: str
    availability: str
    gppe: str | None
    confidence: str | None
    issuer_id: str | None = None
    ticker: str | None = None
    display_name: str | None = None
    gppe_display: str | None = None

    @model_validator(mode="after")
    def _populate_display_fields(self) -> ToptGppeCell:
        resolved_issuer, resolved_ticker, resolved_name = resolve_static_entity_display(self.listing_id)
        if self.issuer_id is None and resolved_issuer is not None:
            object.__setattr__(self, "issuer_id", resolved_issuer)
        if self.ticker is None and resolved_ticker is not None:
            object.__setattr__(self, "ticker", resolved_ticker)
        if self.display_name is None:
            object.__setattr__(self, "display_name", resolved_name)
        if self.gppe_display is None and self.gppe is not None:
            object.__setattr__(self, "gppe_display", _round_gppe_display(self.gppe))
        return self


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


def resolve_strategy_governed_run(conn: Any) -> tuple[str | None, str | None]:
    """Return `(run_id, unavailable_reason)` for the governed strategy universe head."""
    like_prefix = (id_prefix_like(universe_with_role("strategy")),)
    head = conn.execute(
        """
        select run_id, freshness, availability, staleness_reason
        from mart.served_head
        where factor_id = 'gross_profit_per_employee'
          and universe_id like %s
        order by advanced_at desc limit 1
        """,
        like_prefix,
    ).fetchone()
    if head is not None and (head["availability"] == "unavailable" or head["run_id"] is None):
        return None, f"head_unavailable: {head['staleness_reason'] or 'older_than_30d'}"
    if head is None:
        head = conn.execute(
            """
            select s.run_id
            from mart.topt_capture_status s
            join mart.datahub_quality_report q on q.run_id = s.run_id
            where s.environment = (select environment from mart.environment_identity)
              and s.complete
              and s.universe_id like %s
            order by q.created_at desc, q.report_id desc limit 1
            """,
            like_prefix,
        ).fetchone()
    if head is None or head["run_id"] is None:
        return None, "no accepted (quality-reported) production TOPT run"
    return str(head["run_id"]), None


class PostgresToptGppeRepository:
    """Reads mart.topt_gppe_results + the quality report for the current production run."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def latest(self, *, limit: int = 100) -> ToptGppeReport | ToptGppeUnavailable:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                run_id, unavailable_reason = resolve_strategy_governed_run(conn)
                if unavailable_reason is not None or run_id is None:
                    return ToptGppeUnavailable(reason=unavailable_reason or "no_run")
                rows = conn.execute(
                    """
                    select coalesce(ei.listing_id, nullif(r.payload->>'listing_id', '')) as listing_id,
                           r.payload->>'availability' as availability,
                           r.payload->>'gppe' as gppe,
                           r.payload->>'confidence' as confidence
                    from mart.topt_gppe_results r
                    left join lateral (
                        select ei.listing_id
                        from mart.entity_identity ei
                        where ei.entity_id::text = (r.payload->>'listing_id')
                           or (ei.entity_id::text = r.issuer_id and ei.kind = 'issuer')
                        order by (ei.entity_id::text = (r.payload->>'listing_id')) desc
                        limit 1
                    ) ei on true
                    where r.run_id = %s
                    order by coalesce(ei.listing_id, nullif(r.payload->>'listing_id', '')) limit %s
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


class MetricNodeValueCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer_id: str
    node_id: str
    node_key: str
    period: str
    period_tag: str | None = None
    value: str | None
    unit: str | None = None
    availability_status: str
    reason_codes: tuple[str, ...] = ()
    source_evidence_status: str
    factor_validation_status: str
    confidence: str | None
    confidence_band: str | None = None
    tree_version: str | None = None
    tree_sha256: str | None = None


class MetricNodeValueReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    node_key: str
    available_count: int
    cells: tuple[MetricNodeValueCell, ...]


class MetricNodeValueUnavailable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_key: str
    reason: str


class PostgresMetricNodeValueRepository:
    """Reads `mart.metric_node_values` by `node_key` for the governed production run."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def latest(
        self,
        *,
        node_key: str,
        issuer_id: str | None = None,
        limit: int = 100,
    ) -> MetricNodeValueReport | MetricNodeValueUnavailable:
        if not node_key or not node_key.strip():
            raise ValueError("node_key must be non-empty")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
                run_id, unavailable_reason = resolve_strategy_governed_run(conn)
                if unavailable_reason is not None or run_id is None:
                    return MetricNodeValueUnavailable(node_key=node_key, reason=unavailable_reason or "no_run")
                query = """
                    select v.issuer_id, v.node_id::text as node_id, v.node_key, v.period, v.period_tag,
                           v.value::text as value, v.unit, v.availability_status, v.reason_codes,
                           v.source_evidence_status, v.factor_validation_status,
                           v.confidence::text as confidence, v.tree_version, v.tree_sha256
                    from mart.metric_node_values v
                    where v.run_id = %s and v.node_key = %s
                """
                params: list[object] = [run_id, node_key]
                if issuer_id is not None:
                    query += " and v.issuer_id = %s"
                    params.append(issuer_id)
                rows = conn.execute(
                    query + " order by v.issuer_id, v.period desc limit %s",
                    (*params, limit),
                ).fetchall()
                cells = tuple(
                    MetricNodeValueCell(
                        **(
                            r
                            | {
                                "reason_codes": tuple(r["reason_codes"] or ()),
                                "confidence_band": confidence_band(
                                    None if r["confidence"] is None else Decimal(r["confidence"])
                                ),
                            }
                        )
                    )
                    for r in rows
                )
                return MetricNodeValueReport(
                    run_id=run_id,
                    node_key=node_key,
                    available_count=sum(c.availability_status == "available" for c in cells),
                    cells=cells,
                )
        except psycopg.Error:
            return MetricNodeValueUnavailable(node_key=node_key, reason="database_unavailable")
        except _ROW_VALIDATION_ERRORS:
            return MetricNodeValueUnavailable(node_key=node_key, reason="schema_mismatch")
