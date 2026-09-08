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


@dg.op
def run_question_coverage(context: dg.OpExecutionContext, config: StandardBackfillConfig, backfill_summary: str) -> str:
    """#748: after the week's backfill, count the six questions on the governed head —
    answered / unavailable-by-reason / missing — and append the report."""
    from data_engine.datahub.question_coverage import compile_report, persist, summary_line

    executed_at = datetime.fromisoformat(config.executed_at)
    with psycopg.connect(settings.database_url) as connection:
        report = compile_report(connection, universe=config.universe, executed_at=executed_at)
        if report is None:
            context.log.warning("no governed head for %s; no coverage report", config.universe)
            return json.dumps({"universe": config.universe, "report": None})
        report_id = persist(connection, report)
        connection.commit()
    context.log.info("question coverage %s: %s", report_id, summary_line(report))
    context.add_output_metadata(
        {
            "report_id": report_id,
            "universe_id": report["universe_id"],
            "denominator": report["denominator"],
            **{f"{q}_answered": entry["answered"] for q, entry in report["questions"].items()},
            **{f"{q}_missing": entry["missing"] for q, entry in report["questions"].items()},
        }
    )
    return json.dumps({"report_id": report_id, "summary": summary_line(report)})


@dg.job(name=STANDARD_BACKFILL_JOB_NAME)
def standard_backfill_pipeline_job() -> None:
    run_question_coverage(run_standard_backfill())


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
                ops={
                    "run_standard_backfill": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                    # #748: the coverage report follows the backfill for the same universe and tick.
                    "run_question_coverage": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                }
            ),
        )


defs = dg.Definitions(jobs=[standard_backfill_pipeline_job], schedules=[standard_backfill_schedule])
