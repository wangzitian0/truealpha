"""Data-engine internal governed mart read (#405 / #41 / #362)."""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from truealpha_contracts.universes import id_prefix_like, universe_with_role


class PostgresToptReadRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def current_run_id(self) -> str | None:
        pattern = (id_prefix_like(universe_with_role("strategy")),)
        row = self._connection.execute(
            "select target_run_id from mart.current_pointer_head "
            "where environment = (select environment from mart.environment_identity) "
            "and factor_id = 'gross_profit_per_employee' and universe_id like %s "
            "order by advanced_at desc limit 1",
            pattern,
        ).fetchone()
        if row is None:
            row = self._connection.execute(
                "select s.run_id from mart.topt_capture_status s "
                "join mart.datahub_quality_report q on q.run_id = s.run_id "
                "where s.environment = (select environment from mart.environment_identity) "
                "and s.complete and s.universe_id like %s "
                "order by q.created_at desc, q.report_id desc limit 1",
                pattern,
            ).fetchone()
        return None if row is None else row[0]

    def gppe_results(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self._connection.execute(
            "select coalesce(ei.listing_id, nullif(r.payload->>'listing_id', '')) as listing_id, "
            "r.payload->>'availability' as availability, r.payload->>'gppe' as gppe, "
            "r.payload->>'confidence' as confidence "
            "from mart.topt_gppe_results r "
            "left join lateral (select ei.listing_id from mart.entity_identity ei "
            "where ei.entity_id::text = (r.payload->>'listing_id') "
            "or (ei.entity_id::text = r.issuer_id and ei.kind = 'issuer') "
            "order by (ei.entity_id::text = (r.payload->>'listing_id')) desc limit 1) ei on true "
            "where r.run_id = %s "
            "order by coalesce(ei.listing_id, nullif(r.payload->>'listing_id', '')) limit %s",
            (run_id, limit),
        ).fetchall()
        return [{"listing_id": row[0], "availability": row[1], "gppe": row[2], "confidence": row[3]} for row in rows]

    def quality_report(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "select payload from mart.datahub_quality_report where run_id = %s order by created_at desc limit 1",
            (run_id,),
        ).fetchone()
        return None if row is None else row[0]
