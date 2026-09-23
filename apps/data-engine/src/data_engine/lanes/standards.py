"""The standard→wide-row loop's slow plane (#735 / #733): a weekly backfill of each
registered standard's open cells over each universe, and the same run in probe mode as
the data-source research instrument — and the head reports (module 6 and the coverage
report for the governed head, #855), which follow the pointer rather than the clock.

Two sensors close the gaps a fixed schedule leaves:

- `head_reports_on_pointer_advance` launches the head reports for a universe when its
  governed pointer advances. The 23:30 schedule it replaces read the previous QQQ head on
  2026-09-16, because that night's QQQ tick ran until 23:55 (the moomoo origins had made it
  ~20 minutes slower), and the 00:15 proof went red on a report one head behind.
- `standard_backfill_catchup` backfills a registered standard that has never been backfilled
  over a universe in this environment, instead of leaving it to the next Sunday. Production
  had no segment facts on 2026-09-17 for exactly this reason: both of its Sunday runs
  predated the release that made the weekly run cover every standard (#806), and the release
  that did reached production on a Tuesday.

Theme purity and the coverage report record their verdict per universe in
`mart.nightly_verdicts` (#876 W1) from whichever job ran them; the daily fallback keeps both
fresh, and deploy-freshness pages on a red, stale or missing one. `NIGHTLY_VERDICTS` below
names what this lane records."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
from truealpha_contracts.standards import STANDARDS

from data_engine.config import settings
from data_engine.datahub import question_coverage
from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES
from data_engine.datahub.standards.backfill import never_backfilled
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


#: Tags on the runs this lane's schedules and sensors launch: the universe the run writes
#: reports for, and — for a run launched on a pointer advance — the head it follows. What the
#: nightly surface proof waits on is read from the run's CONFIG (`run_universe`), so a run an
#: operator launched by hand, untagged, is waited on too.
UNIVERSE_TAG = "truealpha/universe"
HEAD_RUN_TAG = "truealpha/head_run"


def run_universe(run_config: Mapping[str, Any]) -> str:
    """The universe a run of this lane's jobs writes reports for, from its run config.

    Every op of a run is configured with the same universe (the schedules and sensors build
    them so); an op left unconfigured takes the config's own default, as the run itself does.
    """
    for op in (run_config.get("ops") or {}).values():
        universe = ((op or {}).get("config") or {}).get("universe")
        if universe:
            return str(universe)
    return str(StandardBackfillConfig.model_fields["universe"].default)


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


#: The key a head-reports start op sets, to the head's run id, when the fallback finds that
#: head's reports already written; the ops after it pass it on and recompute nothing.
REPORTS_CURRENT = "reports_current"


def reports_current(upstream_summary: str) -> str | None:
    """The head an upstream op found already reported, or None. The weekly backfill's summary
    is a JSON list and never says so; only the head-reports start op's object can."""
    try:
        parsed = json.loads(upstream_summary)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get(REPORTS_CURRENT):
        return str(parsed[REPORTS_CURRENT])
    return None


def _already_current(context: dg.OpExecutionContext, check: str, config: StandardBackfillConfig, run_id: str) -> str:
    """A fallback run over a head whose reports exist: a green verdict that says so, and
    nothing recomputed or appended. The verdict is still written, because the fallback is what
    keeps the check fresh on a day the pointer does not move."""
    with verdict(
        check_name(check, config.universe),
        registered=NIGHTLY_VERDICTS,
        run_id=context.run_id,
        tick=tick_from_config(config.executed_at),
    ) as outcome:
        outcome.summary = f"reports already current on {run_id[:24]}; nothing recomputed"
    context.log.info("%s for %s: %s", check, config.universe, outcome.summary)
    return json.dumps({"universe": config.universe, "run_id": run_id, REPORTS_CURRENT: run_id})


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
    current = reports_current(backfill_summary)
    if current is not None:
        return _already_current(context, THEME_PURITY_VERDICT, config, current)
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
        # #826 pinned the literal here because `settings.app_env` found no head on staging:
        # the column held the capture TIER, which staging's real-vendor capture stamps
        # `production`. #756 then made the column the DATABASE's declared identity and
        # converted every view, leaving this literal reading a lineage that stopped advancing
        # the day that migration landed. `governed_head` now resolves the identity itself, so
        # there is no environment to name and no second reading to drift back to.
        head = governed_head(connection, universe_prefix=prefix)
        if head is None:
            context.log.warning("no governed head for %s; no theme purity rows", config.universe)
            outcome.pending = True
            outcome.summary = "no governed head; no rows"
            return json.dumps({"universe": config.universe, "rows": 0, "reason": "no_governed_head"})
        # The classifier is told which issuer it is judging (#849): the ticker, from the same
        # universe corpus the backfill labels its own asks with.
        tickers = {issuer.issuer_id: issuer.ticker for issuer in universe_issuers(connection, config.universe)}
        rows = materialize_theme_purity(connection, run_id=head.run_id, cutoff=head.cutoff, tickers=tickers)
        connection.commit()
        published = sum(1 for row in rows if row.result.value is not None)
        # Counts only: the purity values themselves are research output, and this line is public.
        # A row is one (issuer, theme) judgement, so the count is of rows, not issuers.
        outcome.summary = f"{published}/{len(rows)} theme-purity rows published on {head.run_id[:24]}"
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
    current = reports_current(purity_summary)
    if current is not None:
        return _already_current(context, QUESTION_COVERAGE_VERDICT, config, current)
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
            outcome.pending = True
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


def backfill_run_config(executed_at: str, universe: str, standard: str = "") -> dg.RunConfig:
    """One universe's backfill → purity → coverage, every op configured alike. `standard` bounds
    the backfill to one registered standard; empty runs them all."""
    return dg.RunConfig(
        ops={
            "run_standard_backfill": StandardBackfillConfig(
                executed_at=executed_at, universe=universe, standard=standard
            ),
            # #772: module 6 consumes the partitions the backfill just landed.
            "run_theme_purity": StandardBackfillConfig(executed_at=executed_at, universe=universe),
            # #748: the coverage report follows, for the same universe and tick.
            "run_question_coverage": StandardBackfillConfig(executed_at=executed_at, universe=universe),
        }
    )


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
            run_config=backfill_run_config(executed_at, universe),
            tags={UNIVERSE_TAG: universe},
        )


STANDARD_BACKFILL_CATCHUP_SENSOR_NAME = "standard_backfill_catchup"
#: Fifteen minutes between looks. The first look on a build only arms the sensor (see below),
#: so a catch-up never starts inside the deploy walk that follows a promotion (#855 A5: a
#: backfill sharing the database timed a walk out on v0.0.60).
STANDARD_BACKFILL_CATCHUP_INTERVAL_SECONDS = 900


@dg.sensor(
    name=STANDARD_BACKFILL_CATCHUP_SENSOR_NAME,
    job=standard_backfill_pipeline_job,
    minimum_interval_seconds=STANDARD_BACKFILL_CATCHUP_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def standard_backfill_catchup_sensor(context: dg.SensorEvaluationContext):
    """A registered standard is backfilled over every universe as soon as a build that
    registers it is up, not at the next Sunday 09:07.

    The weekly schedule is the cadence the filings need; it is not a delivery path. Production
    on 2026-09-17 had no segment facts and no theme-purity row at all, while staging had 195:
    production's two Sunday runs (09-06, 09-13) ran releases whose schedule still backfilled
    `employees_total` only (the default #806 removed, in v0.0.52), and v0.0.56 reached
    production on Tuesday 09-15, so nothing would have backfilled segments there before Sunday
    09-20 while the nightly surface proof went red on `/research/themes` every night.

    What counts as backfilled is the health-log row every backfill writes at its END
    (`backfill.completed_metric`), so a run that died does not count. One run per universe,
    bounded to the one missing standard when only one is missing; `run_key` names what was
    missing, so Dagster launches it once per sensor — a run that fails is retried by the weekly
    schedule, never by a loop here.

    The cursor holds the build this sensor last armed on: the first look on a new build only
    arms, and the launch waits for the next look, fifteen minutes on.
    """
    build = (settings.data_engine_image_digest or "").strip() or "unversioned"
    if context.cursor != build:
        context.update_cursor(build)
        yield dg.SkipReason(
            f"armed on {build[:19]}; a catch-up waits for the next evaluation, clear of the deploy walk"
        )
        return
    with psycopg.connect(settings.database_url) as connection:
        missing = never_backfilled(connection, universes=STANDARD_BACKFILL_UNIVERSES, standards=standards_to_run(""))
    if not missing:
        yield dg.SkipReason("every registered standard has completed a backfill over every universe here")
        return
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    by_universe: dict[str, list[str]] = {}
    for universe, standard in missing:
        by_universe.setdefault(universe, []).append(standard)
    for universe, standards in by_universe.items():
        context.log.info("catch-up: %s never backfilled over %s here", ", ".join(standards), universe)
        yield dg.RunRequest(
            run_key=f"catchup:{universe}:{'+'.join(sorted(standards))}",
            run_config=backfill_run_config(executed_at, universe, standards[0] if len(standards) == 1 else ""),
            tags={UNIVERSE_TAG: universe},
        )


HEAD_REPORTS_JOB_NAME = "head_reports_pipeline"
HEAD_REPORTS_SENSOR_NAME = "head_reports_on_pointer_advance"
HEAD_REPORTS_SENSOR_INTERVAL_SECONDS = 30
#: The daily FALLBACK, 04:00 UTC: after the nightly proof's longest wait for a settling
#: universe (00:15 + 3 h, `lanes.quality`), so a fallback run is never what the proof waits
#: on, and before deploy-freshness (07:00) reads the verdicts it keeps fresh. The sensor below
#: is what follows the head; this only heals an advance the sensor missed, and on every other
#: day finds the reports current and recomputes nothing.
HEAD_REPORTS_CRON = "0 4 * * *"


class HeadReportsStartConfig(StandardBackfillConfig):
    """`only_if_stale` is the fallback's mode: when the newest stored report for the governed
    head already names that head, the run recomputes nothing and says so. A run launched on a
    pointer advance, or by hand, always recomputes."""

    only_if_stale: bool = False


@dg.op
def head_reports_start(context: dg.OpExecutionContext, config: HeadReportsStartConfig) -> str:
    """The daily job's stand-in for the backfill summary the purity op sequences after: no
    cells are extracted here, only the head's own reports are refreshed — or, in the
    fallback's mode, found current and left alone."""
    summary: dict[str, Any] = {"universe": config.universe, "mode": "head-reports", "executed_at": config.executed_at}
    if config.only_if_stale:
        prefix = UNIVERSE_PREFIXES.get(config.universe, config.universe)
        with psycopg.connect(settings.database_url) as connection:
            head = question_coverage.governed_head(connection, universe_prefix=prefix)
            if head is not None and question_coverage.stored_report_run(connection, head.universe_id) == head.run_id:
                summary[REPORTS_CURRENT] = head.run_id
        current = summary.get(REPORTS_CURRENT)
        context.log.info(
            "fallback for %s: %s",
            config.universe,
            f"reports already current on {current[:24]}" if current else "no current reports; recomputing",
        )
    return json.dumps(summary)


@dg.job(name=HEAD_REPORTS_JOB_NAME)
def head_reports_pipeline_job() -> None:
    """Module 6 and the coverage report for the governed head, whenever it moves (#855 C1/C2).

    The weekly backfill wrote both, so on every other day the head advanced and
    `/research/themes` and `/admin/datahub` kept serving the previous one while
    `/research/rankings` served the new one — the App contradicting its own pointer, with every
    gate green (measured on staging 2026-09-16: rankings on `capture-run:15a2…`, themes and
    coverage on `capture-run:8259…`). Purity replays every judgement it has already made, so
    a head with the same filings costs no model call; the coverage report is SQL.
    """
    run_question_coverage(run_theme_purity(head_reports_start()))


def head_reports_request(
    universe: str, executed_at: str, *, run_key: str, only_if_stale: bool, head_run_id: str | None = None
) -> dg.RunRequest:
    """One universe's head-reports run, every op configured alike."""
    tags = {UNIVERSE_TAG: universe}
    if head_run_id:
        tags[HEAD_RUN_TAG] = head_run_id
    return dg.RunRequest(
        run_key=run_key,
        run_config=dg.RunConfig(
            ops={
                "head_reports_start": HeadReportsStartConfig(
                    executed_at=executed_at, universe=universe, only_if_stale=only_if_stale
                ),
                "run_theme_purity": StandardBackfillConfig(executed_at=executed_at, universe=universe),
                "run_question_coverage": StandardBackfillConfig(executed_at=executed_at, universe=universe),
            }
        ),
        tags=tags,
    )


@dg.schedule(
    job=head_reports_pipeline_job,
    cron_schedule=HEAD_REPORTS_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def head_reports_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    for universe in STANDARD_BACKFILL_UNIVERSES:
        yield head_reports_request(universe, executed_at, run_key=f"{executed_at}:{universe}", only_if_stale=True)


def _followed(cursor: str | None) -> dict[str, str]:
    """universe -> the head run the sensor last acted on. A cursor that does not parse is
    treated as none: the first-sight rule below then keeps it from relaunching current heads."""
    try:
        parsed = json.loads(cursor or "{}")
    except ValueError:
        return {}
    return {str(key): str(value) for key, value in parsed.items()} if isinstance(parsed, dict) else {}


@dg.sensor(
    name=HEAD_REPORTS_SENSOR_NAME,
    job=head_reports_pipeline_job,
    minimum_interval_seconds=HEAD_REPORTS_SENSOR_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def head_reports_sensor(context: dg.SensorEvaluationContext):
    """Head reports follow the pointer, not the clock: one run per advance, per universe.

    Reads `mart.current_pointer_head` through `governed_head` — the same resolution the
    purity and coverage ops and the nightly proof use, under the capture tier the ticks
    register with (#826). The cursor remembers the head each universe was last reported on;
    `run_key = head:<universe>:<run id>` makes Dagster launch one run per head even across a
    restart that lost the cursor.

    A universe the cursor has never seen (a fresh sensor, a reset cursor) is launched only when
    the head's reports are missing: the schedule or an operator may already have written them.
    An advance the sensor sees happen is launched unconditionally — an earlier run that
    resolved the head late may have written this head's coverage report without its purity
    rows, and only this run writes both.
    """
    followed = _followed(context.cursor)
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    requests: list[dg.RunRequest] = []
    with psycopg.connect(settings.database_url) as connection:
        for universe in STANDARD_BACKFILL_UNIVERSES:
            head = question_coverage.governed_head(connection, universe_prefix=UNIVERSE_PREFIXES[universe])
            if head is None or followed.get(universe) == head.run_id:
                continue
            first_sight = universe not in followed
            followed[universe] = head.run_id
            if first_sight and question_coverage.stored_report_run(connection, head.universe_id) == head.run_id:
                context.log.info("%s: reports already current on %s", universe, head.run_id[:24])
                continue
            context.log.info("%s: head advanced to %s; launching its reports", universe, head.run_id[:24])
            requests.append(
                head_reports_request(
                    universe,
                    executed_at,
                    run_key=f"head:{universe}:{head.run_id}",
                    only_if_stale=False,
                    head_run_id=head.run_id,
                )
            )
    context.update_cursor(json.dumps(followed, sort_keys=True))
    if not requests:
        yield dg.SkipReason("no governed head has advanced since its reports were launched")
        return
    yield from requests


defs = dg.Definitions(
    jobs=[standard_backfill_pipeline_job, head_reports_pipeline_job],
    schedules=[standard_backfill_schedule, head_reports_schedule],
    sensors=[head_reports_sensor, standard_backfill_catchup_sensor],
)
