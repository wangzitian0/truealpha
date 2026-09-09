"""Sensors that launch the capture lane's jobs on request rather than on schedule.

Two requesters: the DB-mediated manual trigger (#495; a `staging.pipeline_trigger_requests`
row from the admin page or an operator) and, since 2026-09-07, a promoted build itself
(#712; `boot_canary_sensor` asks for one canary tick per image digest so the release gate
can read the build identity that run stamps). Split from `data_engine.dagster_defs` in
#731.
"""

from datetime import UTC, datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.lanes.capture import CANARY_JOB_NAME, TICK_BY_JOB, TOPT_LIVE_JOB_NAME, ToptLiveTickConfig
from data_engine.lanes.capture import defs as capture_defs


@dg.sensor(
    # Every declared tick is a manual-trigger target (#72 scope 4): the sensor's job
    # list is the capture lane's, not a copy of it.
    jobs=list(capture_defs.jobs or []),
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def pipeline_trigger_sensor(context: dg.SensorEvaluationContext):
    """#495: DB-mediated manual trigger. The admin UI INSERTs into
    `staging.pipeline_trigger_requests` (init.md §2.2 — services exchange
    data only through Postgres; app-web has no path to this daemon and must
    not get one); this sensor launches the SAME job with the requested
    `executed_at` — identical thin-trigger semantics to the schedule
    (idempotent, content-addressed, #491). `run_key = manual:<dedupe_key>`
    makes redelivery harmless: if the consume-UPDATE races a daemon restart
    after the yield, the daemon dedupes the run_key and no second run
    launches.
    """
    with psycopg.connect(settings.database_url) as connection:
        pending = connection.execute(
            "select request_id, executed_at, dedupe_key, job_name from staging.pipeline_trigger_requests "
            "where consumed_at is null order by request_id limit 5"
        ).fetchall()
        for request_id, executed_at, dedupe_key, job_name in pending:
            run_key = f"manual:{dedupe_key}"
            # Dispatch by the request's declared job (#539 QQQ): the same thin
            # trigger drives any declared universe's pipeline; an unknown job name
            # falls back to TOPT, as it always has.
            tick = TICK_BY_JOB.get(job_name, TICK_BY_JOB[TOPT_LIVE_JOB_NAME])
            yield dg.RunRequest(
                run_key=run_key,
                job_name=tick.job_name,
                run_config=dg.RunConfig(
                    ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at.astimezone(UTC).isoformat())}
                ),
            )
            connection.execute(
                "update staging.pipeline_trigger_requests "
                "set consumed_at = clock_timestamp(), launched_run_key = %s where request_id = %s",
                (run_key, request_id),
            )
        connection.commit()


@dg.sensor(
    jobs=[job for job in (capture_defs.jobs or []) if job.name == CANARY_JOB_NAME],
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def boot_canary_sensor(context: dg.SensorEvaluationContext):
    """#712: a promoted build proves itself with one canary tick, unasked.

    The run plan stamps the build that produced it (`mart.data_engine_identity`),
    llm-service `/health` reports that build, and the release gate compares it with
    the tag's registry digest. Until this sensor the run came from an operator
    (`trigger_canary.sh`) or from the next scheduled tick hours later, so the gate could
    only report. Now: the first evaluation on a build (the compose injects
    `TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST`) launches the canary with `run_key =
    boot:<digest>`. Dagster dedupes run keys per sensor across ticks and restarts, and
    the cursor remembers the digest, so three containers and any restart produce one
    run per build. Local and CI carry no digest and skip.

    Read through `settings` since #784: `TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST` is now declared
    in the data-engine environment manifest, and a value the manifest declares is resolved
    by the settings model that declares it -- not fetched from the process environment
    beside it, where nothing reconciles it and boot validation cannot require it.
    """
    digest = (settings.data_engine_image_digest or "").strip()
    if not digest.startswith("sha256:"):
        yield dg.SkipReason("no data-engine image digest in the environment (local/CI)")
        return
    if context.cursor == digest:
        yield dg.SkipReason(f"boot canary already requested for {digest[:19]}…")
        return
    tick = TICK_BY_JOB[CANARY_JOB_NAME]
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    context.update_cursor(digest)
    yield dg.RunRequest(
        run_key=f"boot:{digest}",
        job_name=tick.job_name,
        run_config=dg.RunConfig(ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at)}),
        tags={"truealpha/boot_canary": digest, "truealpha/build": settings.git_commit_sha or "unknown"},
    )


defs = dg.Definitions(sensors=[pipeline_trigger_sensor, boot_canary_sensor])
