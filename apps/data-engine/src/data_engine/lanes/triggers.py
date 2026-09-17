"""Sensors that launch the capture lane's jobs on request rather than on schedule.

Two requesters: the DB-mediated manual trigger (#495; a `staging.pipeline_trigger_requests`
row from the admin page or an operator) and, since 2026-09-07, a promoted build itself
(#712; `boot_canary_sensor` asks for one canary tick per deployment so the release gate
can read the build identity that run stamps). Split from `data_engine.dagster_defs` in
#731.
"""

import json
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
    launches. A request that asks for `force_fetch` (#874) launches a tick
    that skips the reuse window; one that does not is an ordinary tick.
    """
    with psycopg.connect(settings.database_url) as connection:
        pending = connection.execute(
            # `force_fetch` is read through `to_jsonb(request)` rather than by name:
            # migrations apply when llm-service boots, so this image can start before
            # the #874 column exists. On that schema no request can ask for a forced
            # fetch, and the sensor keeps launching ordinary ticks instead of failing
            # every poll (Copilot on #892).
            "select request_id, executed_at, dedupe_key, job_name, "
            "coalesce((to_jsonb(request)->>'force_fetch')::boolean, false) "
            "from staging.pipeline_trigger_requests request "
            "where consumed_at is null order by request_id limit 5"
        ).fetchall()
        for request_id, executed_at, dedupe_key, job_name, force_fetch in pending:
            run_key = f"manual:{dedupe_key}"
            # Dispatch by the request's declared job (#539 QQQ): the same thin
            # trigger drives any declared universe's pipeline; an unknown job name
            # falls back to TOPT, as it always has.
            tick = TICK_BY_JOB.get(job_name, TICK_BY_JOB[TOPT_LIVE_JOB_NAME])
            yield dg.RunRequest(
                run_key=run_key,
                job_name=tick.job_name,
                run_config=dg.RunConfig(
                    ops={
                        tick.op_name: ToptLiveTickConfig(
                            executed_at=executed_at.astimezone(UTC).isoformat(),
                            force_fetch=bool(force_fetch),
                        )
                    }
                ),
            )
            connection.execute(
                "update staging.pipeline_trigger_requests "
                "set consumed_at = clock_timestamp(), launched_run_key = %s where request_id = %s",
                (run_key, request_id),
            )
        connection.commit()


def boot_canary_forces_fetch(app_env: str) -> bool:
    """Owner decision 2026-09-17: staging's boot canary skips the #635 reuse window.

    A newly enabled source is then proven by real vendor bytes on every staging deploy,
    instead of by observations an earlier build committed in the last twelve hours.
    Production keeps the unforced canary and waits for the next cycle.

    Normalised like `capture._production_only` and `capture.live_topt_cron`. The
    comparison is exact: production, its `prod` alias, and any name this repository
    does not know get the unforced canary. An environment nobody declared does not
    spend vendor credits on its own.
    """
    return app_env.strip().lower() == "staging"


def _boot_deployment() -> str:
    """The deployment this process belongs to: image digest plus configuration hash.

    `TRUEALPHA_CONFIGURATION_SHA256` is infra2's hash over the data-engine compose
    artifacts and public env, which includes the per-environment source flags
    (`MOOMOO_*_ORIGIN_ENABLED`). Enabling a source by flag redeploys the same digest
    under a new configuration, and that deploy is the one that has to prove the source.
    """
    return f"{settings.data_engine_image_digest.strip()}|{settings.configuration_sha256.strip()}"


def _boot_cursor(cursor: str | None) -> tuple[str, int]:
    """(deployment, ordinal) from the sensor cursor.

    A cursor from before #885 is the bare digest. It reads as ordinal 0, and its
    deployment string can never equal a `digest|configuration` one, so the first
    deploy of this code launches.
    """
    if not cursor:
        return "", 0
    try:
        state = json.loads(cursor)
        return str(state["deployment"]), int(state["ordinal"])
    except (ValueError, TypeError, KeyError):
        return cursor, 0


@dg.sensor(
    jobs=[job for job in (capture_defs.jobs or []) if job.name == CANARY_JOB_NAME],
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def boot_canary_sensor(context: dg.SensorEvaluationContext):
    """#712: a deployed build proves itself with one canary tick, unasked.

    The run plan stamps the build that produced it (`mart.data_engine_identity`),
    llm-service `/health` reports that build, and the release gate compares it with
    the tag's registry digest. Until this sensor the run came from an operator
    (`trigger_canary.sh`) or from the next scheduled tick hours later, so the gate could
    only report. Now the first evaluation of a deployment launches the canary. The
    compose injects `TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST`; local and CI have no digest
    and skip.

    One run per deployment (#885 item 8). A deployment is the image digest plus the
    configuration hash (`_boot_deployment`). The cursor records the last deployment
    and its ordinal. When the deployment changes, the ordinal goes up by one and the
    run key is `boot:<digest>:deploy-<ordinal>`. Before #885 the key was
    `boot:<digest>`. Dagster dedupes a sensor's run keys against every run it ever
    launched. A rollback to an earlier digest therefore never ran its canary, and the
    prod health gate waited for an identity that no run stamped before the next
    scheduled tick.

    - The ordinal comes from the persisted cursor, so the key is deterministic. If the
      daemon dies after launching and before storing the cursor, the next evaluation
      yields the same key and Dagster dedupes it.
    - Containers restarting inside one deployment, and the three containers of one
      deployment, still produce one run.
    - The git sha does not identify a deployment: a rollback redeploys the same tag
      and the same digest. Container start time is not used either: `restart: always`
      would turn every crash into another canary, a forced one on staging.
    - Clearing the cursor in the Dagster UI restarts the ordinal at 1. If this digest
      was the first deployment after #885, that key already exists and Dagster
      dedupes it; the next deployment launches normally.

    On staging the canary forces a fresh vendor fetch (`boot_canary_forces_fetch`).
    That is 12 Twelve Data credits (6 canary listings x 2) out of staging's
    320/day share (#900).

    Read through `settings` since #784: the identity values are declared in the
    data-engine environment manifest, and a value the manifest declares is resolved
    by the settings model that declares it -- not fetched from the process environment
    beside it, where nothing reconciles it and boot validation cannot require it.
    """
    digest = (settings.data_engine_image_digest or "").strip()
    if not digest.startswith("sha256:"):
        yield dg.SkipReason("no data-engine image digest in the environment (local/CI)")
        return
    deployment = _boot_deployment()
    last_deployment, last_ordinal = _boot_cursor(context.cursor)
    if last_deployment == deployment:
        yield dg.SkipReason(f"boot canary already requested for {digest[:19]}… (deploy {last_ordinal})")
        return
    ordinal = last_ordinal + 1
    tick = TICK_BY_JOB[CANARY_JOB_NAME]
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    force_fetch = boot_canary_forces_fetch(settings.app_env)
    context.update_cursor(json.dumps({"deployment": deployment, "ordinal": ordinal}, sort_keys=True))
    yield dg.RunRequest(
        run_key=f"boot:{digest}:deploy-{ordinal}",
        job_name=tick.job_name,
        run_config=dg.RunConfig(
            ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at, force_fetch=force_fetch)}
        ),
        tags={
            "truealpha/boot_canary": digest,
            "truealpha/build": settings.git_commit_sha or "unknown",
            "truealpha/configuration": settings.configuration_sha256 or "unknown",
        },
    )


defs = dg.Definitions(sensors=[pipeline_trigger_sensor, boot_canary_sensor])
