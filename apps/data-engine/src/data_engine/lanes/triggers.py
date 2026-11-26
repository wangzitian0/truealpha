"""Sensors triggering capture jobs on manual requests or boot (#495, #712)."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import dagster as dg
from truealpha_contracts.common import CaptureEnvironment

from data_engine import db
from data_engine.config import settings
from data_engine.contracts.evidence_graph import (
    CurrentPointer,
    CurrentPointerKey,
    EvidenceNodeKind,
    EvidenceNodeRef,
)
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.lanes.backtest import BACKTEST_JOB_NAME, NightlyBacktestConfig, nightly_backtest_pipeline_job
from data_engine.lanes.capture import (
    CANARY_JOB_NAME,
    TICK_BY_JOB,
    TOPT_LIVE_JOB_NAME,
    LiveTickConfig,
    UniverseTick,
)
from data_engine.lanes.capture import defs as capture_defs

BOOT_CANARY_TAG = "truealpha/boot_canary"


def consume_head_rollback_requests(connection: Any) -> list[str]:
    """Advance append-only mart.current_pointer rows for any unconsumed rollback requests."""
    reg = connection.execute("select to_regclass('staging.head_rollback_requests')").fetchone()
    if not reg or reg[0] is None:
        return []
    pending = connection.execute(
        "select request_id, environment, universe_id, universe_version, factor_id, target_run_id, requested_at "
        "from staging.head_rollback_requests where consumed_at is null order by request_id limit 5"
    ).fetchall()
    if not pending:
        return []
    repo = PostgresEvidenceGraphRepository(connection)
    advanced: list[str] = []
    for request_id, environment, universe_id, universe_version, factor_id, target_run_id, requested_at in pending:
        key = CurrentPointerKey(
            environment=CaptureEnvironment(str(environment)),
            universe_id=str(universe_id),
            universe_version=str(universe_version),
            factor_id=str(factor_id),
        )
        current = repo.head(key)
        if current is not None and current.target_run.node_id == str(target_run_id):
            pointer_id = current.pointer_id
        else:
            cutoff_row = connection.execute(
                "select c.cutoff from raw.capture_runs r "
                "join raw.capture_campaigns c using (campaign_id) "
                "where r.run_id = %s",
                (target_run_id,),
            ).fetchone()
            target_cutoff = cutoff_row[0] if cutoff_row and cutoff_row[0] is not None else requested_at
            pointer = CurrentPointer(
                key=key,
                target_run=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=str(target_run_id)),
                sequence=0 if current is None else current.sequence + 1,
                previous_run=None if current is None else current.target_run,
                advanced_at=target_cutoff,
            )
            repo.advance(pointer)
            pointer_id = pointer.pointer_id
        connection.execute(
            "update staging.head_rollback_requests "
            "set consumed_at = clock_timestamp(), result_pointer_id = %s where request_id = %s",
            (pointer_id, request_id),
        )
        advanced.append(pointer_id)
    return advanced


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
        run_config=dg.RunConfig(ops={tick.op_name: LiveTickConfig(executed_at=executed_at, force_fetch=force_fetch)}),
        tags=tags,
    )


@dg.sensor(
    jobs=[*list(capture_defs.jobs or []), nightly_backtest_pipeline_job],
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def pipeline_trigger_sensor(context: dg.SensorEvaluationContext):
    with db.connect() as connection:
        consume_head_rollback_requests(connection)
        pending = connection.execute(
            "select request_id, executed_at, dedupe_key, job_name, "
            "coalesce((to_jsonb(request)->>'force_fetch')::boolean, false), "
            "coalesce(to_jsonb(request)->'config_json', '{}'::jsonb) "
            "from staging.pipeline_trigger_requests request "
            "where consumed_at is null order by request_id limit 5"
        ).fetchall()
        for request_id, executed_at, dedupe_key, job_name, force_fetch, raw_config in pending:
            run_key = f"manual:{dedupe_key}"
            exec_iso = executed_at.astimezone(UTC).isoformat()
            if job_name == BACKTEST_JOB_NAME:
                cfg = raw_config if isinstance(raw_config, dict) else {}
                custom_def = cfg.get("custom_definition")
                custom_def_json = (
                    json.dumps(custom_def)
                    if isinstance(custom_def, dict)
                    else (custom_def if isinstance(custom_def, str) else None)
                )
                overrides = cfg.get("overrides_json")
                if isinstance(overrides, dict):
                    overrides = json.dumps(overrides)
                try:
                    op_cfg = NightlyBacktestConfig(
                        executed_at=exec_iso,
                        definition_resource=cfg.get("definition_resource"),
                        custom_definition_json=custom_def_json,
                        release_manifest_id=cfg.get("release_manifest_id"),
                        exploratory=bool(cfg.get("exploratory", False)),
                        overrides_json=overrides,
                        start_cutoff=cfg.get("start_cutoff"),
                        end_cutoff=cfg.get("end_cutoff"),
                    )
                except Exception as exc:  # noqa: BLE001
                    context.log.warning(f"Skipping invalid backtest trigger request {request_id}: {exc}")
                    connection.execute(
                        "update staging.pipeline_trigger_requests "
                        "set consumed_at = clock_timestamp(), launched_run_key = %s where request_id = %s",
                        (f"invalid:{dedupe_key}", request_id),
                    )
                    continue
                yield dg.RunRequest(
                    run_key=run_key,
                    job_name=BACKTEST_JOB_NAME,
                    run_config=dg.RunConfig(ops={"run_nightly_backtest_op": op_cfg}),
                )
            else:
                tick = TICK_BY_JOB.get(job_name)
                if tick is None:
                    context.log.warning(f"Skipping unrecognized trigger job_name={job_name!r} (request {request_id})")
                    connection.execute(
                        "update staging.pipeline_trigger_requests "
                        "set consumed_at = clock_timestamp(), launched_run_key = %s where request_id = %s",
                        (f"invalid:{dedupe_key}", request_id),
                    )
                    continue
                yield _tick_run_request(run_key, tick, exec_iso, force_fetch=bool(force_fetch))
            connection.execute(
                "update staging.pipeline_trigger_requests "
                "set consumed_at = clock_timestamp(), launched_run_key = %s where request_id = %s",
                (run_key, request_id),
            )
        connection.commit()


def boot_canary_forces_fetch(environment: str) -> bool:
    return environment.strip().lower() == "staging"


def boot_deployment() -> str:
    return f"{settings.image_digest.strip()}|{settings.configuration_sha256.strip()}"


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
    digest = (settings.image_digest or "").strip()
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
    force_fetch = boot_canary_forces_fetch(settings.environment)
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
