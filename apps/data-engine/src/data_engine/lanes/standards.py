"""The standard→wide-row loop's slow plane (#735 / #733): a weekly backfill of each
registered standard's open cells over each universe, and the same run in probe mode as
the data-source research instrument."""

import json
from datetime import datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.standards.backfill import run_standard_backfill as _run_standard_backfill

STANDARD_BACKFILL_JOB_NAME = "standard_backfill_pipeline"
# Sunday 09:07 UTC: after Saturday's universe refresh has published any membership
# change, well clear of every capture window. Weekly matches the cadence of the
# disclosures it fills (annual filings); the planner makes a quiet week cost nothing
# because a closed cell is never re-fetched.
STANDARD_BACKFILL_CRON = "7 9 * * 0"
STANDARD_BACKFILL_UNIVERSES = ("universe-list:qqq", "topt")


class StandardBackfillConfig(dg.Config):
    """`executed_at` is the cutoff (tick time, ISO 8601), never the wall clock. `mode`
    is `backfill` (land cited facts) or `probe` (report only — the source-research
    instrument). `max_issuers` bounds a manual run; 0 means every open cell."""

    executed_at: str
    universe: str = "universe-list:qqq"
    standard: str = "employees_total"
    mode: str = "backfill"
    max_issuers: int = 0


@dg.op
def run_standard_backfill(context: dg.OpExecutionContext, config: StandardBackfillConfig) -> str:
    cutoff = datetime.fromisoformat(config.executed_at)
    if config.mode not in ("backfill", "probe"):
        raise ValueError(f"mode must be backfill or probe, got {config.mode!r}")
    with psycopg.connect(settings.database_url) as connection:
        report = _run_standard_backfill(
            connection,
            universe=config.universe,
            standard_name=config.standard,
            cutoff=cutoff,
            mode=config.mode,  # type: ignore[arg-type]
            max_issuers=config.max_issuers,
            log=context.log.info,
        )
    summary = report.summary()
    context.add_output_metadata(
        {
            "universe": config.universe,
            "standard": config.standard,
            "mode": config.mode,
            "issuers": report.issuers,
            "open_cells": report.open,
            "open_by_reason": str(dict(report.open_by_reason)),
            "outcomes": str(dict(report.outcomes)),
        }
    )
    return json.dumps(summary, sort_keys=True)


@dg.job(name=STANDARD_BACKFILL_JOB_NAME)
def standard_backfill_pipeline_job() -> None:
    run_standard_backfill()


@dg.schedule(
    job=standard_backfill_pipeline_job,
    cron_schedule=STANDARD_BACKFILL_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def standard_backfill_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    for universe in STANDARD_BACKFILL_UNIVERSES:
        yield dg.RunRequest(
            run_key=f"{executed_at}:{universe}",
            run_config=dg.RunConfig(
                ops={"run_standard_backfill": StandardBackfillConfig(executed_at=executed_at, universe=universe)}
            ),
        )


defs = dg.Definitions(jobs=[standard_backfill_pipeline_job], schedules=[standard_backfill_schedule])
