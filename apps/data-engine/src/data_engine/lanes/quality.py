"""Data-quality standing checks that run where the data lives (#674, #725 item A, #581).

`tools/output_invariants.py` is the only check that asks whether a published NUMBER is
possible. Until 2026-09-07 its only scheduled host was a GitHub runner that cannot reach
the loopback Postgres, so the step announced itself unconfigured and exited 0 every day;
run by hand against production that day it found two stale invariants and no new data
defect. This lane runs the same suite, from the copy baked into the image, against the
environment's own database after the nightly ticks — a red Dagster run, in the daemon's
log and in `dagster.runs`, is the verdict.

The second job here is the datahub CONFIDENCE & ACCURACY report (owner standard,
2026-09-15): after the invariants have judged the day's heads, grade every (metric family,
subject) cell of each governed head into high / medium / low / missing from what its
origins asserted, sample ten well-known subjects across every origin, re-derive a sample
of fundamentals from SEC through the independent oracle, and append one content-addressed
`mart.datahub_confidence_report` row per universe.

The third is the model-provider key probe (#876 W2): one minimal ask a day through the
source gateway, so a revoked key is a red verdict by 07:00 instead of a human reading the
ledger (#832).

The fourth is the release fetch proof (`quality.release_fetch_proof`): whether the first
scheduled or forced tick of the current deployment fetched from every registered origin
itself. Production's boot canary reuses the night's observations, so nothing else shows that
a promoted release's fetch path works.

Every check here records its verdict — green and red — in `mart.nightly_verdicts`
(`quality.nightly_verdicts`, #876 W1); `/health` publishes the newest per check and the
scheduled deploy-freshness workflow pages on a red, stale or missing one. The names a run
can record are `NIGHTLY_VERDICTS` below.
"""

import contextlib
import io
import json
import runpy
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dagster as dg
import psycopg
from truealpha_contracts.common import CaptureEnvironment

from data_engine.config import settings
from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES
from data_engine.lanes.capture import TICKS
from data_engine.lanes.standards import HEAD_REPORTS_JOB_NAME, STANDARD_BACKFILL_JOB_NAME, run_universe
from data_engine.quality import surface_proof
from data_engine.quality.nightly_verdicts import TICK_TAG, check_name, tick_from_config, tick_of, verdict
from data_engine.sources import gateway

OUTPUT_INVARIANTS_JOB_NAME = "output_invariants_check"
# 00:15 UTC: after the canary (23:47) has published, before anything reads the day's head.
# The QQQ tick (23:20) can still be running then — since the moomoo origins it ran until 23:55
# on 2026-09-16, and a smaller Twelve Data share (#900) slows it further — so the surface proof
# waits for every universe to settle rather than judging a head its reports have not reached.
OUTPUT_INVARIANTS_CRON = "15 0 * * *"
#: How long the surface proof waits for a universe to settle, and how often it looks. Three
#: hours takes the proof past any tick that is merely slow (the verdict still lands hours
#: before deploy-freshness reads it at 07:00); a tick still running after that is the governed
#: pointer's freshness check to page on (`tools/datahub_freshness.py`), and the proof names the
#: universe IN-PROGRESS instead of calling its surfaces wrong.
SURFACE_SETTLE_TIMEOUT = timedelta(hours=3)
SURFACE_SETTLE_POLL_SECONDS = 60.0
#: How long after a head's pointer row is written its reports may still be missing without the
#: proof calling it a mismatch: the pointer sensor looks every 30 s and a head-reports run
#: takes about a minute. Past this, missing reports mean the sensor did not follow the head.
HEAD_REPORTS_GRACE = timedelta(minutes=10)

CONFIDENCE_REPORT_JOB_NAME = "datahub_confidence_report"
# 00:45 UTC: after the invariants (00:15) have had their say on the same heads, and the
# SEC oracle's handful of company-facts reads land in a quiet window for the shared
# 10 req/s seat. Manual runs launch the same job by name with an explicit `executed_at`.
# The report waits for a settled universe first, like the surface proof: a slow QQQ tick
# can still commit after 00:20, and the pointer sensor then rewrites that head's reports.
CONFIDENCE_REPORT_CRON = "45 0 * * *"
CONFIDENCE_REPORT_UNIVERSES = ("universe-list:qqq", "topt")

MODEL_KEY_HEALTH_JOB_NAME = "model_key_health"
# 06:00 UTC: after every nightly model caller, an hour before deploy-freshness (07:00) reads
# the verdict, so a daemon that is merely slow still lands inside the day's check.
MODEL_KEY_HEALTH_CRON = "0 6 * * *"

RELEASE_FETCH_PROOF_JOB_NAME = "release_fetch_proof"
# 06:15 UTC: after every scheduled tick of the night (22:15 through the 23:47 canary) has
# finished, and before deploy-freshness (07:00) reads the verdict. The verdict is pending
# until the deployment's first scheduled or forced tick has finished.
RELEASE_FETCH_PROOF_CRON = "15 6 * * *"

#: Verdict names (`mart.nightly_verdicts.check_name`) this lane records.
OUTPUT_INVARIANTS_VERDICT = "output_invariants"
REPORT_SURFACE_VERDICT = "report_surface_proof"
CONFIDENCE_REPORT_VERDICT = "datahub_confidence_report"
MODEL_KEY_HEALTH_VERDICT = "model_key_health"
RELEASE_FETCH_PROOF_VERDICT = "release_fetch_proof"
NIGHTLY_VERDICTS: tuple[str, ...] = (
    OUTPUT_INVARIANTS_VERDICT,
    REPORT_SURFACE_VERDICT,
    *(check_name(CONFIDENCE_REPORT_VERDICT, universe) for universe in CONFIDENCE_REPORT_UNIVERSES),
    MODEL_KEY_HEALTH_VERDICT,
    RELEASE_FETCH_PROOF_VERDICT,
)

#: The suite as the image carries it (Dockerfile), or the repository copy for local runs.
_IMAGE_COPY = Path("/app/tools/output_invariants.py")
_REPO_COPY = Path(__file__).resolve().parents[5] / "tools" / "output_invariants.py"


def _suite_path() -> Path:
    for candidate in (_IMAGE_COPY, _REPO_COPY):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"output_invariants.py is neither at {_IMAGE_COPY} nor {_REPO_COPY}")


@dg.op
def run_output_invariants(context: dg.OpExecutionContext) -> str:
    """Run the suite against this environment's database; fail the run on any verdict
    the suite fails on (a violation, an expired exemption, a stale exemption, missing
    coverage). The suite's own report is the run log."""
    with verdict(
        OUTPUT_INVARIANTS_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        module = runpy.run_path(str(_suite_path()), run_name="output_invariants")
        report = io.StringIO()
        with contextlib.redirect_stdout(report), contextlib.redirect_stderr(report):
            exit_code = module["main"](["--database-url", settings.database_url, "--require-coverage"])
        lines = report.getvalue().splitlines()
        for line in lines:
            context.log.info(line)
        outcome.summary = invariants_summary(lines, exit_code)
        if exit_code != 0:
            raise dg.Failure(f"output invariants: exit {exit_code} — the report above names what failed (#581)")
    return report.getvalue()


def invariants_summary(lines: list[str], exit_code: int) -> str:
    """The suite's verdict in counts and invariant ids — never its offending rows, which carry
    values and belong in the run log only."""
    marks = [line.split()[0] for line in lines if line.startswith("  ") and line.split()]
    held, deferred, empty = marks.count("ok"), marks.count("DEFERRED"), marks.count("EMPTY")
    failed = [
        line.removeprefix("invariant failed: ").split(":", 1)[0]
        for line in lines
        if line.startswith("invariant failed: ")
    ]
    counts = f"{held} held, {deferred} deferred, {empty} empty"
    if exit_code == 0:
        return counts
    named = ", ".join(failed) if failed else "none named, see the run log"
    return f"exit {exit_code}: {len(failed)} failed ({named}); {counts}"


#: A run in one of these has not finished; a queued run is committed to running.
UNFINISHED_RUN_STATUSES = (
    dg.DagsterRunStatus.QUEUED,
    dg.DagsterRunStatus.NOT_STARTED,
    dg.DagsterRunStatus.STARTING,
    dg.DagsterRunStatus.STARTED,
    dg.DagsterRunStatus.CANCELING,
)
#: An unfinished run created longer ago than this is not a reason to wait: no tick or report
#: run takes half a day, and a run whose worker died without marking it would otherwise excuse
#: its universe's mismatches every night.
IN_FLIGHT_MAX_AGE = timedelta(hours=12)


def _tick_universe(key: str | None) -> str:
    # A tick with no universe head is the hand-curated TOPT corpus (`UniverseTick`).
    return key or "topt"


def runs_in_flight(instance: dg.DagsterInstance, *, now: datetime | None = None) -> dict[str, str]:
    """universe -> the unfinished run that will move its head or rewrite its reports.

    The capture ticks move a head (the job names the universe); the head-reports and backfill
    jobs rewrite its purity rows and coverage report (the run config names it, so a run an
    operator launched untagged counts too). Runs created more than `IN_FLIGHT_MAX_AGE` before
    `now` are not counted."""
    created_after = (now or datetime.now(UTC)) - IN_FLIGHT_MAX_AGE
    ticks = {
        tick.job_name: _tick_universe(tick.universe_head_kind)
        for tick in TICKS
        if _tick_universe(tick.universe_head_kind) in UNIVERSE_PREFIXES
    }
    found: dict[str, str] = {}
    for job_name in (*ticks, HEAD_REPORTS_JOB_NAME, STANDARD_BACKFILL_JOB_NAME):
        runs = instance.get_runs(
            filters=dg.RunsFilter(
                job_name=job_name, statuses=list(UNFINISHED_RUN_STATUSES), created_after=created_after
            )
        )
        for run in runs:
            universe = ticks.get(job_name) or run_universe(run.run_config or {})
            if universe in UNIVERSE_PREFIXES:
                found.setdefault(
                    universe, f"{job_name} run {run.run_id[:8]} is {run.status.value.lower().replace('_', ' ')}"
                )
    return found


def settling(connection: psycopg.Connection, instance: dg.DagsterInstance, *, now: datetime) -> dict[str, str]:
    """Every universe the proof should not judge yet, with why: an unfinished run for it, or a
    head recorded within `HEAD_REPORTS_GRACE` whose reports do not name it yet."""
    pending = runs_in_flight(instance, now=now)
    for universe, why in surface_proof.fresh_heads_without_reports(
        connection, now=now, grace=HEAD_REPORTS_GRACE
    ).items():
        pending.setdefault(universe, why)
    return pending


#: Indirected so a test can wait without sleeping.
_sleep = time.sleep


def _await_settled(context: dg.OpExecutionContext) -> None:
    """Poll until no universe is settling, or until `SURFACE_SETTLE_TIMEOUT` has passed.
    The surface proof and the confidence report both judge heads through it."""
    deadline = time.monotonic() + SURFACE_SETTLE_TIMEOUT.total_seconds()
    while True:
        with psycopg.connect(settings.database_url) as connection:
            pending = settling(connection, context.instance, now=datetime.now(UTC))
        if not pending:
            return
        described = "; ".join(f"{universe}: {why}" for universe, why in sorted(pending.items()))
        if time.monotonic() >= deadline:
            context.log.warning(f"still settling after {SURFACE_SETTLE_TIMEOUT}: {described}")
            return
        context.log.info(f"waiting for {described}")
        _sleep(SURFACE_SETTLE_POLL_SECONDS)


@dg.op
def run_report_surface_proof(context: dg.OpExecutionContext) -> str:
    """Every report surface serves the governed head (#855 C1): replay each App reader's own
    run selection and the coverage report's recomputation against this environment's
    database; one line per surface in the log; fail the run on any surface that serves
    another run than the pointer names, or a report the tables no longer agree with.

    Judged on a settled head, never a moving one (2026-09-17: the proof read the QQQ head a
    tick had committed at 23:55 against a coverage report written at 23:31). It waits for every
    universe to settle, then proves inside ONE repeatable-read snapshot, so a commit that lands
    mid-proof cannot put a new head beside an old report. A universe still settling when the
    wait runs out is IN-PROGRESS: named, and not a failure."""
    from data_engine.quality.surface_proof import prove, summary_lines, verdict_counts

    with verdict(
        REPORT_SURFACE_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        _await_settled(context)
        with psycopg.connect(settings.database_url) as connection:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            connection.read_only = True
            now = datetime.now(UTC)
            pending = settling(connection, context.instance, now=now)
            surfaces = prove(connection, executed_at=now, settling=pending)
        for line in summary_lines(surfaces):
            context.log.info(line)
        failed = [surface for surface in surfaces if surface.mismatched]
        settling_surfaces = [surface for surface in surfaces if surface.state == "IN-PROGRESS"]
        context.add_output_metadata(
            {"surfaces": len(surfaces), "mismatched": len(failed), "in_progress": len(settling_surfaces)}
        )
        outcome.summary = (
            verdict_counts(surfaces).split(";", 1)[0]
            + (
                f"; in progress: {', '.join(surface.surface for surface in settling_surfaces)}"
                if settling_surfaces
                else ""
            )
            + (f"; mismatched: {', '.join(surface.surface for surface in failed)}" if failed else "")
        )
        if failed:
            raise dg.Failure(
                "report surface proof: "
                + "; ".join(surface.line for surface in failed)
                + " — the App shows a head the pointer does not name (#855 C1)"
            )
    return json.dumps([surface.line for surface in surfaces])


@dg.job(name=OUTPUT_INVARIANTS_JOB_NAME)
def output_invariants_job() -> None:
    # Two independent verdicts in one nightly run: the numbers are possible (the suite), and
    # every surface serves the head the pointer names (the proof). Neither waits on the other.
    run_output_invariants()
    run_report_surface_proof()


@dg.schedule(
    job=output_invariants_job,
    cron_schedule=OUTPUT_INVARIANTS_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def output_invariants_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    # The job takes no config, so the tick travels as a tag: the verdict is dated by it.
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


class ConfidenceReportConfig(dg.Config):
    """`executed_at` is the schedule's tick time (ISO 8601), never the wall clock. An empty
    `sample_subjects` takes the module's default (five QQQ names, five TOPT issuers);
    `oracle_issuers` bounds the live SEC re-derivation."""

    executed_at: str
    universe: str = "universe-list:qqq"
    sample_subjects: list[str] = []
    oracle_issuers: int = 5


@dg.op
def run_confidence_report(context: dg.OpExecutionContext, config: ConfidenceReportConfig) -> str:
    from data_engine.datahub.confidence_report import SecOracle, compile_report, persist, summary_line

    executed_at = datetime.fromisoformat(config.executed_at)
    # The oracle needs the SEC user agent; without one it reports "not compared" rather
    # than failing the report that carries every other section.
    oracle = SecOracle(settings.sec_user_agent) if settings.sec_user_agent else None
    with verdict(
        check_name(CONFIDENCE_REPORT_VERDICT, config.universe),
        registered=NIGHTLY_VERDICTS,
        run_id=context.run_id,
        tick=tick_from_config(config.executed_at),
    ) as outcome:
        # A head still moving (a tick committing, its reports being rewritten) is waited
        # out before the report reads it (2026-09-17: QQQ may commit around 00:20 and its
        # head reports follow a minute later).
        _await_settled(context)
        # Every SEC read is attributed to this Dagster run in the external call ledger (#729)
        # and admitted by the rule-6 gate first.
        with (
            gateway.run_scope(f"dagster:{context.run_id}"),
            gateway.capacity_scope(),
            psycopg.connect(settings.database_url) as connection,
        ):
            report = compile_report(
                connection,
                universe=config.universe,
                executed_at=executed_at,
                # The capture tier the ticks register their pointer with, never APP_ENV (#826).
                environment=CaptureEnvironment.PRODUCTION.value,
                sample_subjects=config.sample_subjects or None,
                oracle_issuers=config.oracle_issuers,
                oracle=oracle,
            )
            if report is None:
                context.log.warning("no governed head for %s; no confidence report", config.universe)
                outcome.summary = "no governed head; no report"
            else:
                report_id = persist(connection, report)
                connection.commit()
                outcome.summary = f"report persisted for {report['universe_id']}"
    if report is None:
        return json.dumps({"universe": config.universe, "report": None})
    context.log.info("confidence report %s: %s", report_id, summary_line(report))
    context.add_output_metadata(
        {
            "report_id": report_id,
            "universe_id": report["universe_id"],
            "run_id": report["run_id"],
            "sources_connected": ", ".join(report["sources_connected"]),
            **{
                f"{name}_{band}": family[band]
                for name, family in report["families"].items()
                for band in ("high", "medium", "low", "missing")
            },
            "close_agreement_rate": str(report["accuracy"]["close"]["agreement_rate"]),
            "sec_oracle_issuers_compared": report["accuracy"]["sec_oracle"]["issuers_compared"],
        }
    )
    return json.dumps({"report_id": report_id, "summary": summary_line(report)})


@dg.job(name=CONFIDENCE_REPORT_JOB_NAME)
def datahub_confidence_report_job() -> None:
    run_confidence_report()


@dg.schedule(
    job=datahub_confidence_report_job,
    cron_schedule=CONFIDENCE_REPORT_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def datahub_confidence_report_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    for universe in CONFIDENCE_REPORT_UNIVERSES:
        yield dg.RunRequest(
            run_key=f"{executed_at}:{universe}",
            run_config=dg.RunConfig(
                ops={"run_confidence_report": ConfidenceReportConfig(executed_at=executed_at, universe=universe)}
            ),
        )


@dg.op
def run_model_key_health(context: dg.OpExecutionContext) -> str:
    """One minimal ask of the seated model provider (#876 W2). A rejected key, an erroring or
    unreachable provider, or no seated key at all is a red run and a red verdict."""
    from data_engine.quality.model_key_health import probe

    with verdict(
        MODEL_KEY_HEALTH_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        # The ask lands in the external call ledger attributed to this run (#729).
        with gateway.run_scope(f"dagster:{context.run_id}"):
            health = probe()
        outcome.summary = health.summary
        context.log.info("model key health: %s", health.summary)
        context.add_output_metadata({"outcome": health.outcome, "status_code": health.status_code or 0})
        if not health.ok:
            raise dg.Failure(f"model key health: {health.summary} (#832, #876 W2)")
    return json.dumps({"ok": health.ok, "outcome": health.outcome, "status_code": health.status_code})


@dg.job(name=MODEL_KEY_HEALTH_JOB_NAME)
def model_key_health_job() -> None:
    run_model_key_health()


@dg.schedule(
    job=model_key_health_job,
    cron_schedule=MODEL_KEY_HEALTH_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def model_key_health_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


@dg.op
def run_release_fetch_proof(context: dg.OpExecutionContext) -> str:
    """Whether the current deployment's first scheduled or forced tick fetched from every
    registered origin itself (`quality.release_fetch_proof`). Pending records `ok = null`
    and leaves the run green; red fails the run."""
    from data_engine.quality import release_fetch_proof

    with verdict(
        RELEASE_FETCH_PROOF_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        with psycopg.connect(settings.database_url) as connection:
            proof = release_fetch_proof.evaluate(
                connection, context.instance, digest=settings.data_engine_image_digest or ""
            )
        context.log.info(f"release fetch proof: {proof.state}: {proof.summary}")
        context.add_output_metadata({"state": proof.state})
        outcome.summary = proof.summary
        outcome.pending = proof.ok is None
        if proof.ok is False:
            raise dg.Failure(f"release fetch proof: {proof.summary}")
    return json.dumps({"state": proof.state, "summary": proof.summary})


@dg.job(name=RELEASE_FETCH_PROOF_JOB_NAME)
def release_fetch_proof_job() -> None:
    run_release_fetch_proof()


@dg.schedule(
    job=release_fetch_proof_job,
    cron_schedule=RELEASE_FETCH_PROOF_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def release_fetch_proof_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


defs = dg.Definitions(
    jobs=[output_invariants_job, datahub_confidence_report_job, model_key_health_job, release_fetch_proof_job],
    schedules=[
        output_invariants_schedule,
        datahub_confidence_report_schedule,
        model_key_health_schedule,
        release_fetch_proof_schedule,
    ],
)
