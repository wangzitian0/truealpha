"""The entity identity backfill as a Dagster job, never a boot step (#877).

`staging.entity_backfill()` (migration 20260917T0604) fills the entity identity store from
what the pipeline already stored: every legacy issuer/instrument/listing id gets a derived
UUID and its aliases, and TOPT's LEI/CUSIP ids are joined to the plane's CIK/FIGI ids where
N-PORT and the knowledge graph prove it (docs/entity-identity.md). It used to run inside a
migration on every llm-service boot; on staging that held the boot for 34 s and failed the
v0.0.80 rollout. It runs here instead, and nothing waits for it.

Two launchers:

* `entity_identity_backfill_sensor` launches the job once per environment when the store is
  empty, when an observed or published id has no entity yet, or when N-PORT or knowledge-graph
  rows arrived since its last launch. Its cursor is the newest ingestion time among those
  inputs, so a quiet database costs one cheap `max()` read per evaluation. A failed run is
  retried at the next arriving evidence, or by hand.
* By hand, through the Dagster GraphQL API (no run config)::

    mutation {
      launchRun(executionParams: {
        selector: {repositoryLocationName: "data_engine.dagster_defs",
                   repositoryName: "__repository__",
                   jobName: "entity_identity_backfill"}
      }) { __typename ... on LaunchRunSuccess { run { runId } } }
    }

The function takes an advisory lock, so a hand launch and a sensor launch never interleave.
Its ids are derived, so a rerun, or a run in another environment, mints the same UUIDs.
"""

import json
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import dagster as dg
import psycopg

from data_engine.config import settings

ENTITY_BACKFILL_JOB_NAME = "entity_identity_backfill"


def run_entity_backfill(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    """Apply the backfill in the caller's transaction; returns its summary plus the time taken."""
    started = time.monotonic()
    row = connection.execute("select staging.entity_backfill()").fetchone()
    assert row is not None
    summary: dict[str, Any] = dict(row[0])
    summary["duration_seconds"] = round(time.monotonic() - started, 3)
    return summary


def _count(section: Mapping[str, Any]) -> int:
    return sum(int(value) for value in section.values())


@dg.op
def entity_identity_backfill_op(context: dg.OpExecutionContext) -> dg.Output[dict[str, Any]]:
    """Run staging.entity_backfill() once and record what it wrote, held back and failed."""
    with psycopg.connect(settings.database_url) as connection:
        summary = run_entity_backfill(connection)
        connection.commit()
    context.log.info(f"entity backfill: {json.dumps(summary, sort_keys=True)}")
    if summary["failed"]:
        # Rolled back per component inside the function; the rest committed. Loud, not fatal:
        # the next run retries exactly these.
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
        return datetime.fromisoformat(cursor)
    except ValueError:
        return None


def evaluate_backfill_due(connection: psycopg.Connection[Any], cursor: str | None) -> tuple[str | None, str | None]:
    """(reason to launch or None, the cursor to store). One `max()` read when nothing arrived."""
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
    """Launch the backfill when the store is empty or behind what the pipeline stored."""
    with psycopg.connect(settings.database_url, autocommit=True) as connection:
        reason, next_cursor = evaluate_backfill_due(connection, context.cursor)
    if next_cursor is not None and next_cursor != context.cursor:
        context.update_cursor(next_cursor)
    if reason is None:
        return dg.SkipReason("entity store is current with the stored evidence")
    context.log.info(f"entity backfill due: {reason} (evidence up to {next_cursor})")
    return dg.RunRequest(run_key=f"entity-backfill:{next_cursor}", job_name=ENTITY_BACKFILL_JOB_NAME)


defs = dg.Definitions(jobs=[entity_identity_backfill_job], sensors=[entity_identity_backfill_sensor])
