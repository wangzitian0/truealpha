"""MCP reader of `mart.metric_node_values` (#1413 F4).

Reads any metric-forest node by `node_key` at the governed head (`mart.served_head` where
`universe_id like 'universe:topt-%'`, falling back to the acceptance-gated TOPT run when
no pointer has been advanced yet). The reader derives `confidence_band` from the stored
`confidence` with `confidence_band` (#1517 A2); it never reads the stored column.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, ValidationError

from truealpha_contracts.confidence_bands import confidence_band
from truealpha_contracts.universes import id_prefix_like, universe_with_role

_ROW_VALIDATION_ERRORS = (KeyError, ValueError, TypeError, ValidationError)


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
                head = conn.execute(
                    """
                    select run_id, freshness, availability, staleness_reason
                    from mart.served_head
                    where factor_id = 'gross_profit_per_employee'
                      and universe_id like %s
                    order by advanced_at desc limit 1
                    """,
                    (id_prefix_like(universe_with_role("strategy")),),
                ).fetchone()
                if head is not None and (head["availability"] == "unavailable" or head["run_id"] is None):
                    reason = head["staleness_reason"] or "older_than_30d"
                    return MetricNodeValueUnavailable(node_key=node_key, reason=f"head_unavailable: {reason}")
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
                        (id_prefix_like(universe_with_role("strategy")),),
                    ).fetchone()
                if head is None or head["run_id"] is None:
                    return MetricNodeValueUnavailable(
                        node_key=node_key,
                        reason="no accepted (quality-reported) production TOPT run",
                    )
                run_id = str(head["run_id"])
                query = """
                    select v.issuer_id,
                           v.node_id::text as node_id,
                           v.node_key,
                           v.period,
                           v.period_tag,
                           v.value::text as value,
                           v.unit,
                           v.availability_status,
                           v.reason_codes,
                           v.source_evidence_status,
                           v.factor_validation_status,
                           v.confidence::text as confidence,
                           v.tree_version,
                           v.tree_sha256
                    from mart.metric_node_values v
                    where v.run_id = %s
                      and v.node_key = %s
                """
                params: list[object] = [run_id, node_key]
                if issuer_id is not None:
                    query += " and v.issuer_id = %s"
                    params.append(issuer_id)
                query += " order by v.issuer_id, v.period desc limit %s"
                params.append(limit)
                rows = conn.execute(query, tuple(params)).fetchall()
                cells = tuple(
                    MetricNodeValueCell(
                        issuer_id=r["issuer_id"],
                        node_id=r["node_id"],
                        node_key=r["node_key"],
                        period=r["period"],
                        period_tag=r["period_tag"],
                        value=r["value"],
                        unit=r["unit"],
                        availability_status=r["availability_status"],
                        reason_codes=tuple(r["reason_codes"] or ()),
                        source_evidence_status=r["source_evidence_status"],
                        factor_validation_status=r["factor_validation_status"],
                        confidence=r["confidence"],
                        confidence_band=confidence_band(None if r["confidence"] is None else Decimal(r["confidence"])),
                        tree_version=r["tree_version"],
                        tree_sha256=r["tree_sha256"],
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
