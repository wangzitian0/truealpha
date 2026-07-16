"""Durable preparation boundary for a manually triggered Production TOPT run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from truealpha_contracts.capture_control import CaptureCheckpoint, CheckpointPhase

from data_engine.datahub.production_topt.planning import ManualProductionToptPlan
from data_engine.datahub.repository import PostgresCaptureControlRepository, ToptCaptureStatus


def persist_manual_production_plan(
    connection: Connection[Any],
    plan: ManualProductionToptPlan,
    *,
    recorded_at: datetime,
) -> ToptCaptureStatus:
    """Atomically persist dispatch intent; this function performs no source calls."""

    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("planned checkpoint time must be timezone-aware")
    if recorded_at < plan.campaign.cutoff:
        raise ValueError("planned checkpoint cannot precede the Production cutoff")

    repository = PostgresCaptureControlRepository(connection)
    checkpoint = CaptureCheckpoint(
        run_id=plan.run.run_id,
        sequence=1,
        phase=CheckpointPhase.PLANNED,
        completed_obligation_ids=(),
        recorded_at=recorded_at,
    )
    with connection.transaction():
        repository.put_schedule_policy(plan.schedule_policy)
        repository.put_campaign(plan.campaign)
        repository.put_list_version(plan.list_version)
        repository.bind_campaign_list(plan.campaign.campaign_id, plan.list_version.list_version_id)
        repository.put_run(plan.run)
        for obligation, request, work_item, binding in zip(
            plan.obligations,
            plan.source_requests,
            plan.work_items,
            plan.bindings,
            strict=True,
        ):
            repository.put_obligation(plan.campaign.campaign_id, obligation)
            repository.put_source_request(request)
            repository.put_work_item(work_item, plan.schedule_policy.retry)
            repository.put_binding(binding)
        repository.put_checkpoint(checkpoint)
    return repository.status(plan.run.run_id)
