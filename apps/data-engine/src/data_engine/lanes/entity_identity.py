"""Sensor-driven entity identity backfill job (#877)."""

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
from truealpha_runtime.mart_refresh import refresh_mart_materialized_views

from data_engine.config import settings
from data_engine.external_op import external_op

ENTITY_BACKFILL_JOB_NAME = "entity_identity_backfill"


def run_entity_backfill(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    started = time.monotonic()
    row = connection.execute("select staging.entity_backfill()").fetchone()
    assert row is not None
    summary: dict[str, Any] = dict(row[0])
    refresh_mart_materialized_views(connection)
    summary["duration_seconds"] = round(time.monotonic() - started, 3)
    return summary


def _count(section: Mapping[str, Any]) -> int:
    return sum(int(value) for value in section.values())


@external_op
def entity_identity_backfill_op(context: dg.OpExecutionContext) -> dg.Output[dict[str, Any]]:
    with psycopg.connect(settings.database_url) as connection:
        summary = run_entity_backfill(connection)
        connection.commit()
        refresh_mart_materialized_views(connection, concurrently=True, commit_before=True)
    context.log.info(f"entity backfill: {json.dumps(summary, sort_keys=True)}")
    if summary["failed"]:
        context.log.warning(f"entity backfill could not write {len(summary['failed'])} component(s)")
    return dg.Output(
        summary,
        metadata={
            "duration_seconds": summary["duration_seconds"],
            "entities_minted": _count(summary["minted"]),
            "aliases_written": _count(summary["aliases"]),
            "relations_written": _count(summary["relations"]),
            "claims_held_back": _count(summary["held_back"]),
            "components_failed": len(summary["failed"]),
            "summary": dg.MetadataValue.json(summary),
        },
    )


@dg.job(name=ENTITY_BACKFILL_JOB_NAME)
def entity_identity_backfill_job() -> None:
    entity_identity_backfill_op()


def _cursor_time(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        dt = datetime.fromisoformat(cursor)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except ValueError:
        return None


def evaluate_backfill_due(connection: psycopg.Connection[Any], cursor: str | None) -> tuple[str | None, str | None]:
    since = _cursor_time(cursor)
    row = connection.execute("select staging.entity_evidence_watermark()").fetchone()
    watermark: datetime | None = row[0] if row else None
    next_cursor = watermark.isoformat() if watermark is not None else cursor
    if since is not None and (watermark is None or watermark <= since):
        return None, next_cursor
    due = connection.execute("select staging.entity_backfill_due(%s)", (since,)).fetchone()
    return (due[0] if due else None), next_cursor


@dg.sensor(
    job=entity_identity_backfill_job,
    minimum_interval_seconds=600,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def entity_identity_backfill_sensor(context: dg.SensorEvaluationContext):
    with psycopg.connect(settings.database_url, autocommit=True) as connection:
        reason, next_cursor = evaluate_backfill_due(connection, context.cursor)
    if next_cursor is not None and next_cursor != context.cursor:
        context.update_cursor(next_cursor)
    if reason is None:
        return dg.SkipReason("entity store is current with the stored evidence")
    context.log.info(f"entity backfill due: {reason} (evidence up to {next_cursor})")
    return dg.RunRequest(run_key=f"entity-backfill:{next_cursor}", job_name=ENTITY_BACKFILL_JOB_NAME)


defs = dg.Definitions(jobs=[entity_identity_backfill_job], sensors=[entity_identity_backfill_sensor])
