"""Row-complete DataHub quality report for a capture run (#61 / #404).

Library form of the report the one-shot script produced, so the deployed Dagster
pipeline (#27) can persist it inside the same transaction as the capture it
grades. Computes, over the exact requested denominator, the terminal/coverage/
availability/freshness/independent-reconciliation/lineage/mean-confidence
figures from the capture tables, and persists one append-only
`mart.datahub_quality_report` row.

`independent_reconciliation` is still the origin-count proxy (>=2 distinct
source_request_ids); replacing it with `reconcile_source_assertions` is #343.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from truealpha_contracts import canonical_sha256


def latest_run(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute(
        "select run_id from mart.topt_capture_status order by cutoff desc, run_id desc limit 1"
    ).fetchone()
    if row is None:
        raise ValueError("no capture run found")
    return row[0]


def build_report(conn: psycopg.Connection[Any], run_id: str) -> dict[str, Any]:
    status = conn.execute(
        """
        select obligation_count, terminal_count, success_count, unchanged_count,
               unavailable_count, skipped_count, failed_count, complete
        from mart.topt_capture_status where run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if status is None:
        raise ValueError(f"no capture status for run {run_id}")
    requested = status[0]

    # Per-obligation observation facts for this run.
    rows = conn.execute(
        """
        select ob.obligation_id,
               count(distinct o.observation_id)                          as obs,
               count(distinct o.observation_id) filter (
                   where p.observation_id is not null and v.source_vintage_id is not null
                     and f.id is not null)                                as lineaged,
               bool_or(o.freshness_state = 'fresh')                       as fresh,
               count(distinct v.source_request_id)                        as sources,
               max(o.confidence)                                          as confidence
        from raw.capture_obligations ob
        left join staging.capture_observation_obligations oo
               on oo.capture_obligation_id = ob.obligation_id
        left join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        left join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        left join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        left join raw.fetches f on f.id = v.raw_fetch_id
        where ob.run_id = %s
        group by ob.obligation_id
        """,
        (run_id,),
    ).fetchall()

    available = sum(1 for r in rows if r[1] > 0)
    lineage_complete = sum(1 for r in rows if (r[2] or 0) > 0)
    fresh = sum(1 for r in rows if r[3])
    independent = sum(1 for r in rows if (r[4] or 0) >= 2)  # >=2 origins = independently reconciled
    confidences = [r[5] for r in rows if r[5] is not None]
    mean_conf = (sum(confidences) / requested) if requested else Decimal(0)

    def ratio(n: int) -> str:
        return str((Decimal(n) / Decimal(requested)).quantize(Decimal("0.0001"))) if requested else "0"

    return {
        "run_id": run_id,
        "requested_count": requested,
        "terminal_count": status[1],
        "available_count": available,
        "fresh_count": fresh,
        "independently_reconciled_count": independent,
        "lineage_complete_count": lineage_complete,
        "terminal_coverage": ratio(status[1]),
        "availability": ratio(available),
        "freshness": ratio(fresh),
        "independent_reconciliation": ratio(independent),
        "lineage_completeness": ratio(lineage_complete),
        "denominator_mean_confidence": str(Decimal(mean_conf).quantize(Decimal("0.0001"))),
        "complete": bool(status[7]),
    }


def persist(conn: psycopg.Connection[Any], report: dict[str, Any]) -> str:
    content_sha256 = canonical_sha256(report)
    report_id = f"datahub-quality-report:{content_sha256}"
    conn.execute(
        """
        insert into mart.datahub_quality_report (report_id, content_sha256, run_id, requested_count, payload)
        values (%s, %s, %s, %s, %s) on conflict (report_id) do nothing
        """,
        (report_id, content_sha256, report["run_id"], report["requested_count"], psycopg.types.json.Jsonb(report)),
    )
    return report_id
