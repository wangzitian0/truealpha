"""Sensors that launch capture lane jobs on manual trigger or deployment boot (#495 / #712)."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.lanes.capture import (
    CANARY_JOB_NAME,
    TICK_BY_JOB,
    TOPT_LIVE_JOB_NAME,
    ToptLiveTickConfig,
    UniverseTick,
)
from data_engine.lanes.capture import defs as capture_defs

BOOT_CANARY_TAG = "truealpha/boot_canary"


def _tick_run_request(
    run_key: str,
    tick: UniverseTick,
    executed_at: str,
    *,
    force_fetch: bool,
    tags: Mapping[str, str] | None = None,
) -> dg.RunRequest:
    return dg.RunRequest(
        run_key=run_key,
        job_name=tick.job_name,
        run_config=dg.RunConfig(
            ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at, force_fetch=force_fetch)}
        ),
        tags=tags,
    )


@dg.sensor(
    jobs=list(capture_defs.jobs or []),
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def pipeline_trigger_sensor(context: dg.SensorEvaluationContext):
    with psycopg.connect(settings.database_url) as connection:
        pending = connection.execute(
            "select request_id, executed_at, dedupe_key, job_name, "
            "coalesce((to_jsonb(request)->>'force_fetch')::boolean, false) "
            "from staging.pipeline_trigger_requests request "
            "where consumed_at is null order by request_id limit 5"
        ).fetchall()
        for request_id, executed_at, dedupe_key, job_name, force_fetch in pending:
            run_key = f"manual:{dedupe_key}"
            tick = TICK_BY_JOB.get(job_name, TICK_BY_JOB[TOPT_LIVE_JOB_NAME])
            yield _tick_run_request(
                run_key, tick, executed_at.astimezone(UTC).isoformat(), force_fetch=bool(force_fetch)
            )
            connection.execute(
                "update staging.pipeline_trigger_requests "
                "set consumed_at = clock_timestamp(), launched_run_key = %s where request_id = %s",
                (run_key, request_id),
            )
        connection.commit()


def boot_canary_forces_fetch(app_env: str) -> bool:
    return app_env.strip().lower() == "staging"


def boot_deployment() -> str:
    return f"{settings.data_engine_image_digest.strip()}|{settings.configuration_sha256.strip()}"


def _boot_cursor(cursor: str | None) -> tuple[str, int]:
    if not cursor:
        return "", 0
    try:
        state = json.loads(cursor)
        return str(state["deployment"]), int(state["ordinal"])
    except (ValueError, TypeError, KeyError):
        return cursor, 0


@dg.sensor(
    jobs=[job for job in (capture_defs.jobs or []) if job.name in (CANARY_JOB_NAME, TOPT_LIVE_JOB_NAME)],
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def boot_canary_sensor(context: dg.SensorEvaluationContext):
    digest = (settings.data_engine_image_digest or "").strip()
    if not digest.startswith("sha256:"):
        yield dg.SkipReason("no data-engine image digest in the environment (local/CI)")
        return
    deployment = boot_deployment()
    last_deployment, last_ordinal = _boot_cursor(context.cursor)
    if last_deployment == deployment:
        yield dg.SkipReason(f"boot canary already requested for {digest[:19]}… (deploy {last_ordinal})")
        return
    ordinal = last_ordinal + 1
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    force_fetch = boot_canary_forces_fetch(settings.app_env)
    context.update_cursor(json.dumps({"deployment": deployment, "ordinal": ordinal}, sort_keys=True))
    build_tags = {
        "truealpha/build": settings.git_commit_sha or "unknown",
        "truealpha/configuration": settings.configuration_sha256 or "unknown",
    }
    yield _tick_run_request(
        f"boot:{digest}:deploy-{ordinal}",
        TICK_BY_JOB[CANARY_JOB_NAME],
        executed_at,
        force_fetch=force_fetch,
        tags={BOOT_CANARY_TAG: digest, **build_tags},
    )
    if force_fetch:
        yield _tick_run_request(
            f"boot:{digest}:deploy-{ordinal}:topt",
            TICK_BY_JOB[TOPT_LIVE_JOB_NAME],
            executed_at,
            force_fetch=False,
            tags=build_tags,
        )


defs = dg.Definitions(sensors=[pipeline_trigger_sensor, boot_canary_sensor])
