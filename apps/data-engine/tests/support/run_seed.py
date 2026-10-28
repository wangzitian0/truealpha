"""Helper for seeding Dagster runs in unit tests (#1522)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import dagster as dg
from dagster._core.events import DagsterEvent, DagsterEventType
from dagster._core.events.log import EventLogEntry
from dagster._core.storage.dagster_run import DagsterRun, DagsterRunStatus


def seed_success_run(instance: dg.DagsterInstance, job_name: str, duration_seconds: float) -> str:
    """Record a SUCCESS run of `job_name` with elapsed runtime `duration_seconds`."""
    now = datetime.now(UTC).timestamp()
    start_time = now - duration_seconds
    end_time = now
    run_id = str(uuid.uuid4())
    run = DagsterRun(job_name=job_name, run_id=run_id, status=DagsterRunStatus.STARTING)
    instance.add_run(run)
    instance.handle_new_event(
        EventLogEntry(
            error_info=None,
            level=10,
            user_message="",
            run_id=run_id,
            timestamp=start_time,
            dagster_event=DagsterEvent(DagsterEventType.PIPELINE_START.value, job_name=job_name),
        )
    )
    instance.handle_new_event(
        EventLogEntry(
            error_info=None,
            level=10,
            user_message="",
            run_id=run_id,
            timestamp=end_time,
            dagster_event=DagsterEvent(DagsterEventType.PIPELINE_SUCCESS.value, job_name=job_name),
        )
    )
    return run_id
