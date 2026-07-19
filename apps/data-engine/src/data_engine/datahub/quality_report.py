"""Row-complete DataHub quality report for a capture run (#61 / #404).

Library form of the report the one-shot script produced, so the deployed Dagster
pipeline (#27) can persist it inside the same transaction as the capture it
grades. Computes, over the exact requested denominator, the terminal/coverage/
availability/freshness/independent-reconciliation/lineage/mean-confidence
figures from the capture tables, and persists one append-only
`mart.datahub_quality_report` row.

`independent_reconciliation` is computed by the accepted fusion engine
`reconcile_source_assertions` (#343): every multi-source market-price cell's
assertions are reconciled under a declared tolerance/priority policy, the
per-cell outcome is persisted in the report payload, and only AGREED cells
count as independently reconciled — a raw origin count never does.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from truealpha_contracts import canonical_sha256
from truealpha_contracts.universe import SubjectKind, SubjectRef
from truealpha_contracts.reconciliation import (
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationPolicy,
    SourceAssertion,
    reconcile_source_assertions,
)

# Declared fusion policy for the dual-origin market-price cells (init.md rule 12):
# yahoo-chart is the pinned primary, twelve-data the independent second origin; a
# 0.1% relative tolerance absorbs vendor rounding, and disagreement beyond it
# abstains and reports rather than letting either source win silently.
RECONCILIATION_POLICY = ReconciliationPolicy(
    policy_version="market-price-fusion:v1",
    source_priority=("yahoo-chart:v1", "twelve-data:v1"),
    absolute_tolerance=Decimal("0"),
    relative_tolerance=Decimal("0.001"),
    minimum_independent_origin_groups=2,
)
_SOURCE_BY_PARSER = {
    "production-topt-live-parser:v1": ("yahoo-chart:v1", "origin:yahoo:v1", "close"),
    "twelve-data-parser:v1": ("twelve-data:v1", "origin:twelve-data:v1", "price"),
}


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
    reconciliation = _reconcile_market_price_cells(conn, run_id)
    independent = sum(1 for cell in reconciliation.values() if cell["outcome"] == ReconciliationOutcome.AGREED.value)
    confidences = [r[5] for r in rows if r[5] is not None]
    mean_conf = (sum(confidences) / requested) if requested else Decimal(0)

    def ratio(n: int) -> str:
        return str((Decimal(n) / Decimal(requested)).quantize(Decimal("0.0001"))) if requested else "0"

    return {
        "reconciliation_policy_id": RECONCILIATION_POLICY.policy_id,
        "reconciliation_cells": reconciliation,
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


def _reconcile_market_price_cells(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    """Run the accepted fusion engine over every market-price cell's assertions.

    Each observation (Yahoo primary + Twelve Data second origin) becomes a
    SourceAssertion; the declared policy reconciles them. Returns per-listing
    outcomes for the report payload. Single-assertion cells honestly resolve
    INSUFFICIENT_INDEPENDENT_ORIGINS — counting origins never reconciles values.
    """
    status = conn.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return {}
    cutoff = status[0]
    rows = conn.execute(
        """
        select o.subject_id, o.parser_version, o.knowable_at, o.confidence,
               o.normalized_payload_sha256, o.observation_id,
               v.source_vintage_id, v.raw_object_id, p.normalized_payload,
               (ob.partition_key)::date as partition_date
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        where ob.run_id = %s and o.semantic_type = 'market-price'
        order by o.subject_id
        """,
        (run_id,),
    ).fetchall()

    by_listing: dict[str, list[tuple[str, Any, Decimal, str, str, str, str, dict]]] = {}
    partition: date | None = None
    for subject_id, parser, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, payload, part in rows:
        if parser not in _SOURCE_BY_PARSER:
            continue
        partition = partition or part
        source_id, origin_group, value_key = _SOURCE_BY_PARSER[parser]
        value = payload.get(value_key)
        if value is None:
            continue
        by_listing.setdefault(subject_id, []).append(
            (
                source_id,
                knowable_at,
                Decimal(str(confidence)),
                payload_sha,
                obs_id,
                vintage_id,
                raw_object,
                {"origin_group": origin_group, "value": value},
            )
        )

    outcomes: dict[str, dict[str, Any]] = {}
    for listing_id, entries in sorted(by_listing.items()):
        cell = ReconciliationCell(
            requirement_id=f"data-requirement:{canonical_sha256({'requirement': 'market-price:v1'})}",
            subject=SubjectRef(kind=SubjectKind.LISTING, id=listing_id),
            field_name="close",
            field_semantics_id=f"field-semantics:{canonical_sha256({'field': 'market-price-close:v1'})}",
            unit="USD",
            valid_from=partition or cutoff.date(),
            valid_to=cutoff.date(),
        )
        assertions = tuple(
            SourceAssertion(
                cell_id=cell.cell_id,
                observation_id=obs_id,
                source_id=source_id,
                origin_group_id=extra["origin_group"],
                knowable_at=knowable_at,
                normalized_value_sha256=payload_sha,
                numeric_value=Decimal(str(extra["value"])),
                confidence_assessment_id=f"confidence-assessment:{payload_sha}",
                confidence_score=confidence,
                lineage_node_ids=(vintage_id, raw_object),
                lineage_complete=True,
            )
            for source_id, knowable_at, confidence, payload_sha, obs_id, vintage_id, raw_object, extra in entries
        )
        result = reconcile_source_assertions(
            cell=cell, assertions=assertions, policy=RECONCILIATION_POLICY, cutoff=cutoff
        )
        outcomes[listing_id] = {
            "outcome": result.outcome.value,
            "origin_groups": len(result.origin_group_ids),
            "selected_source": next(
                (a.source_id for a in assertions if a.assertion_id == result.selected_assertion_id), None
            ),
            "selected_value": None if result.selected_numeric_value is None else str(result.selected_numeric_value),
            "conflicting": len(result.conflicting_assertion_ids),
        }
    return outcomes


def persist(conn: psycopg.Connection[Any], report: dict[str, Any]) -> str:
    content_sha256 = canonical_sha256(report)
    report_id = f"datahub-quality-report:{content_sha256}"
    conn.execute(
        """
        insert into mart.datahub_quality_report (report_id, content_sha256, run_id, requested_count, payload)
        values (%s, %s, %s, %s, %s) on conflict (report_id) do nothing
        """,
        (report_id, content_sha256, report["run_id"], report["requested_count"], Jsonb(report)),
    )
    return report_id
