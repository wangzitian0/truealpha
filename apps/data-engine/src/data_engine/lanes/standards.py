"""The standard→wide-row loop's slow plane (#735 / #733): a weekly backfill of each
registered standard's open cells over each universe, and the same run in probe mode as
the data-source research instrument — and the daily head reports (module 6 and the coverage
report for today's head, #855).

Theme purity and the coverage report record their verdict per universe in
`mart.nightly_verdicts` (#876 W1) from whichever job ran them; the daily job keeps both
fresh, and deploy-freshness pages on a red, stale or missing one. `NIGHTLY_VERDICTS` below
names what this lane records."""

import json
from datetime import datetime

import dagster as dg
import psycopg
from truealpha_contracts.common import CaptureEnvironment
from truealpha_contracts.standards import STANDARDS

from data_engine.config import settings
from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES
from data_engine.datahub.standards.backfill import run_standard_backfill as _run_standard_backfill
from data_engine.datahub.standards.planner import universe_issuers
from data_engine.quality.nightly_verdicts import check_name, tick_from_config, verdict

STANDARD_BACKFILL_JOB_NAME = "standard_backfill_pipeline"
# Sunday 09:07 UTC: after Saturday's universe refresh has published any membership
# change, well clear of every capture window. Weekly matches the cadence of the
# disclosures it fills (annual filings); the planner makes a quiet week cost nothing
# because a closed cell is never re-fetched.
STANDARD_BACKFILL_CRON = "7 9 * * 0"
STANDARD_BACKFILL_UNIVERSES = ("universe-list:qqq", "topt")

#: Verdict names (`mart.nightly_verdicts.check_name`) this lane records, per universe.
THEME_PURITY_VERDICT = "theme_purity"
QUESTION_COVERAGE_VERDICT = "question_coverage"
NIGHTLY_VERDICTS: tuple[str, ...] = tuple(
    check_name(check, universe)
    for check in (THEME_PURITY_VERDICT, QUESTION_COVERAGE_VERDICT)
    for universe in STANDARD_BACKFILL_UNIVERSES
)


class StandardBackfillConfig(dg.Config):
    """`executed_at` is the cutoff (tick time, ISO 8601), never the wall clock. `mode`
    is `backfill` (land cited facts) or `probe` (report only — the source-research
    instrument). `max_issuers` bounds a manual run; 0 means every open cell."""

    executed_at: str
    universe: str = "universe-list:qqq"
    #: Empty runs EVERY registered standard, which is what the schedule does. A name here
    #: bounds a manual run to one metric.
    #:
    #: This used to default to `"employees_total"`, so the weekly schedule — which passes no
    #: standard — ran exactly one metric. `segment_revenue` was registered with a plane and an
    #: adapter (#804/#805) and was never invoked by anything deployed: the loop was
    #: generalized (#799/#800) while the last enumerated metric list in the repository sat
    #: here, one name long. init.md rule 22 is that the registry is the list; a default that
    #: names a metric is that list wearing a different hat.
    standard: str = ""
    mode: str = "backfill"
    max_issuers: int = 0


def standards_to_run(selected: str) -> tuple[str, ...]:
    """Which standards one run covers: the named one, or every registered one.

    Sorted so a run's order is a property of the registry rather than of dict insertion —
    two runs of the same week do the same thing in the same order.
    """
    if selected:
        if selected not in STANDARDS:
            raise ValueError(f"unknown standard {selected!r}; registered: {sorted(STANDARDS)}")
        return (selected,)
    return tuple(sorted(STANDARDS))


@dg.op
def run_standard_backfill(context: dg.OpExecutionContext, config: StandardBackfillConfig) -> str:
    cutoff = datetime.fromisoformat(config.executed_at)
    if config.mode not in ("backfill", "probe"):
        raise ValueError(f"mode must be backfill or probe, got {config.mode!r}")
    names = standards_to_run(config.standard)
    summaries = []
    with psycopg.connect(settings.database_url) as connection:
        for name in names:
            report = _run_standard_backfill(
                connection,
                universe=config.universe,
                standard_name=name,
                cutoff=cutoff,
                mode=config.mode,  # type: ignore[arg-type]
                max_issuers=config.max_issuers,
                log=context.log.info,
            )
            summaries.append(report.summary())
            context.add_output_metadata(
                {
                    f"{name}_issuers": report.issuers,
                    f"{name}_open_cells": report.open,
                    f"{name}_open_by_reason": str(dict(report.open_by_reason)),
                    f"{name}_outcomes": str(dict(report.outcomes)),
                }
            )
    context.add_output_metadata({"universe": config.universe, "standards": ", ".join(names), "mode": config.mode})
    return json.dumps(summaries, sort_keys=True)


@dg.op
def run_theme_purity(context: dg.OpExecutionContext, config: StandardBackfillConfig, backfill_summary: str) -> str:
    """#772 (init.md §7 module 6): the theme-purity rows for this week's governed head.

    Sequenced after the backfill because it consumes what the backfill landed — the accepted
    segment partitions — and before the coverage report, which counts the column this writes.
    A run with no governed head writes nothing and says so; that is the honest state for a
    universe whose pointer has not advanced yet, not an error.

    Model spend is bounded by replay, not by a limit: the classification is keyed on
    (issuer, filing, theme), so the first week asks and every later week that sees the same
    filings replays (§9). A restated segment set is a new filing and is asked afresh, which
    is the behaviour you want.
    """
    from data_engine.datahub.production_topt.theme_purity import materialize_theme_purity, summary_line
    from data_engine.datahub.question_coverage import governed_head

    context.log.info("theme purity follows backfill: %s", backfill_summary[:200])
    # The cutoff is the governed HEAD's, not `config.executed_at`: these rows describe the
    # run the App serves, so the partitions they consume must be the ones knowable at that
    # run's cutoff. Selecting at the schedule time instead would let a filing that landed
    # after the head was published change a row attributed to it.
    prefix = UNIVERSE_PREFIXES.get(config.universe, config.universe)
    with (
        verdict(
            check_name(THEME_PURITY_VERDICT, config.universe),
            registered=NIGHTLY_VERDICTS,
            run_id=context.run_id,
            tick=tick_from_config(config.executed_at),
        ) as outcome,
        psycopg.connect(settings.database_url) as connection,
    ):
        # Resolved under the capture TIER the tick registers its pointer with
        # (`CaptureEnvironment.PRODUCTION`, which staging's real-vendor capture stamps too),
        # never under `APP_ENV`. Asking for `settings.app_env` found no head on staging on any
        # tick, while the coverage op in the same run found one (#826). Named here rather than
        # left to `governed_head`'s default, so a change to that default cannot bring it back.
        head = governed_head(connection, universe_prefix=prefix, environment=CaptureEnvironment.PRODUCTION.value)
        if head is None:
            context.log.warning("no governed head for %s; no theme purity rows", config.universe)
            outcome.summary = "no governed head; no rows"
            return json.dumps({"universe": config.universe, "rows": 0, "reason": "no_governed_head"})
        # The classifier is told which issuer it is judging (#849): the ticker, from the same
        # universe corpus the backfill labels its own asks with.
        tickers = {issuer.issuer_id: issuer.ticker for issuer in universe_issuers(connection, config.universe)}
        rows = materialize_theme_purity(connection, run_id=head.run_id, cutoff=head.cutoff, tickers=tickers)
        connection.commit()
        published = sum(1 for row in rows if row.result.value is not None)
        # Counts only: the purity values themselves are research output, and this line is public.
        outcome.summary = f"{published}/{len(rows)} issuers published on {head.run_id[:24]}"
    context.log.info(summary_line(rows))
    context.add_output_metadata(
        {"universe": config.universe, "run_id": head.run_id, "rows": len(rows), "published": published}
    )
    return json.dumps({"universe": config.universe, "run_id": head.run_id, "rows": len(rows), "published": published})


@dg.op
def run_question_coverage(context: dg.OpExecutionContext, config: StandardBackfillConfig, purity_summary: str) -> str:
    """#748: after the week's backfill of EVERY standard and module 6's purity rows, count
    the six questions on the governed head — answered / unavailable-by-reason / missing — and
    append the report."""
    from data_engine.datahub.question_coverage import compile_report, persist, summary_line

    # The backfill's summary is this op's only upstream: consuming it is what sequences the
    # report after the week's facts have landed, and logging it keeps the pair legible.
    context.log.info("coverage follows theme purity: %s", purity_summary[:400])
    executed_at = datetime.fromisoformat(config.executed_at)
    with (
        verdict(
            check_name(QUESTION_COVERAGE_VERDICT, config.universe),
            registered=NIGHTLY_VERDICTS,
            run_id=context.run_id,
            tick=tick_from_config(config.executed_at),
        ) as outcome,
        psycopg.connect(settings.database_url) as connection,
    ):
        report = compile_report(connection, universe=config.universe, executed_at=executed_at)
        if report is None:
            context.log.warning("no governed head for %s; no coverage report", config.universe)
            outcome.summary = "no governed head; no report"
            return json.dumps({"universe": config.universe, "report": None})
        report_id = persist(connection, report)
        connection.commit()
        outcome.summary = f"report persisted for {report['universe_id']}"
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
    run_question_coverage(run_theme_purity(run_standard_backfill()))


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
                    # #772: module 6 consumes the partitions the backfill just landed.
                    "run_theme_purity": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                    # #748: the coverage report follows, for the same universe and tick.
                    "run_question_coverage": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                }
            ),
        )


HEAD_REPORTS_JOB_NAME = "head_reports_pipeline"
#: 23:30 UTC, after the TOPT (22:45) and QQQ (23:20) ticks have advanced their heads and before
#: the nightly proof (00:15) asks whether every surface serves them.
HEAD_REPORTS_CRON = "30 23 * * *"


@dg.op
def head_reports_start(config: StandardBackfillConfig) -> str:
    """The daily job's stand-in for the backfill summary the purity op sequences after: no
    cells are extracted here, only the head's own reports are refreshed."""
    return json.dumps({"universe": config.universe, "mode": "head-reports", "executed_at": config.executed_at})


@dg.job(name=HEAD_REPORTS_JOB_NAME)
def head_reports_pipeline_job() -> None:
    """Module 6 and the coverage report for TODAY's head, every day (#855 C1/C2).

    The weekly backfill wrote both, so on every other day the head advanced and
    `/research/themes` and `/admin/datahub` kept serving the previous one while
    `/research/rankings` served the new one — the App contradicting its own pointer, with every
    gate green (measured on staging 2026-09-16: rankings on `capture-run:15a2…`, themes and
    coverage on `capture-run:8259…`). Purity replays every judgement it has already made, so
    a day with the same filings costs no model call; the coverage report is SQL.
    """
    run_question_coverage(run_theme_purity(head_reports_start()))


@dg.schedule(
    job=head_reports_pipeline_job,
    cron_schedule=HEAD_REPORTS_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def head_reports_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    for universe in STANDARD_BACKFILL_UNIVERSES:
        yield dg.RunRequest(
            run_key=f"{executed_at}:{universe}",
            run_config=dg.RunConfig(
                ops={
                    "head_reports_start": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                    "run_theme_purity": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                    "run_question_coverage": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                }
            ),
        )


defs = dg.Definitions(
    jobs=[standard_backfill_pipeline_job, head_reports_pipeline_job],
    schedules=[standard_backfill_schedule, head_reports_schedule],
)
