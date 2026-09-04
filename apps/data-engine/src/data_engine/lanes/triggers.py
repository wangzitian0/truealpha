"""The DB-mediated manual trigger (#495): a sensor over the capture lane's jobs
(#731 split of `data_engine.dagster_defs`; behaviour unchanged)."""

from datetime import UTC

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.lanes.capture import TICK_BY_JOB, TOPT_LIVE_JOB_NAME, ToptLiveTickConfig
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


defs = dg.Definitions(sensors=[pipeline_trigger_sensor])
