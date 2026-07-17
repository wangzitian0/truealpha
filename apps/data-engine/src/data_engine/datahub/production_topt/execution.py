"""Durable preparation boundary for a manually triggered Production TOPT run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts import canonical_sha256
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

    durable_release = connection.execute(
        """
        select 1 from staging.contract_objects
        where contract_id = %s and contract_kind = 'release_manifest'
        """,
        (plan.release_manifest_id,),
    ).fetchone()
    if durable_release is None:
        raise LookupError(f"release manifest is not durably persisted: {plan.release_manifest_id}")

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
        plan_payload = {
            "run_id": plan.run.run_id,
            "release_manifest_id": plan.release_manifest_id,
        }
        plan_sha256 = canonical_sha256(plan_payload)
        inserted = connection.execute(
            """
            insert into raw.production_topt_run_plans (
                run_id, release_manifest_id, content_sha256, payload
            ) values (%s, %s, %s, %s)
            on conflict (run_id) do nothing returning run_id
            """,
            (
                plan.run.run_id,
                plan.release_manifest_id,
                plan_sha256,
                Jsonb(plan_payload),
            ),
        ).fetchone()
        if inserted is None:
            existing = connection.execute(
                """
                select release_manifest_id, content_sha256, payload
                from raw.production_topt_run_plans where run_id = %s
                """,
                (plan.run.run_id,),
            ).fetchone()
            if existing != (plan.release_manifest_id, plan_sha256, plan_payload):
                raise ValueError("Production TOPT run is already bound to a different release plan")
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
