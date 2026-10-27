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

The fifth is the runtime shape of the code server (`quality.runtime_shape`, #1467): whether the
memory cgroup that holds every run and the gRPC server saw an OOM kill, an unplanned restart, a new
peak above `UTILIZATION_CEILING` of its ceiling, or a process larger than its budget. The run reads the
cgroup of the container it executes in, which is the code server's. The sensor `runtime_shape_sampler` keeps the totals between two nightly runs.

The sixth is the fetch-key check (`quality.fetch_keys_once`, #1411 K7): whether the call ledger of the
UTC day that just ended shows any fetch key fetched twice. The source gateway serves a key that landed
from stored bytes; this check proves it from the ledger.

The seventh is the run health of every job (`quality.job_run_health`, #1466): whether the runs of the last
7 days in `dagster.runs` succeeded, job by job. It is a dashboard row and not a gate: a red verdict leaves
the run that records it green. `/admin/pipeline` shows the counts and the error line beside it.

Every check here records its verdict — green and red — in `mart.nightly_verdicts`
(`quality.nightly_verdicts`, #876 W1); `/health` publishes the newest per check and the
daily delivery report shows a red, stale or missing one. The names a run
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
from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.config import settings
from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES, declared_environment
from data_engine.environment_universes import any_environment_universe_names, scheduled_universe_names
from data_engine.lanes.capture import TICKS, is_head_scheduled
from data_engine.lanes.standards import HEAD_REPORTS_JOB_NAME, STANDARD_BACKFILL_JOB_NAME, run_universe
from data_engine.lanes.triggers import boot_deployment
from data_engine.quality import fetch_keys_once, job_run_health, runtime_shape, surface_proof
from data_engine.quality.nightly_verdicts import (
    TICK_TAG,
    check_name,
    newest_is_red,
    tick_from_config,
    tick_of,
    verdict,
)
from data_engine.sources import gateway

#: Run tag of a job whose run waits for other runs to finish (`_await_settled`). The run queue of
#: `dagster.yaml` caps the runs that carry it below the queue limit, so a run that they wait for always
#: finds a slot (#1467). `test_run_capacity.py` holds every job that waits to this tag.
WAITS_FOR_RUNS_TAG = "truealpha/waits_for_runs"

OUTPUT_INVARIANTS_JOB_NAME = "output_invariants_check"
# 00:15 UTC: after the canary (23:47) has published, before anything reads the day's head.
# The QQQ tick (23:20) can still be running then — since the moomoo origins it ran until 23:55
# on 2026-09-16, and a smaller Twelve Data share (#900) slows it further — so the surface proof
# waits for every universe to settle rather than judging a head its reports have not reached.
OUTPUT_INVARIANTS_CRON = "15 0 * * *"
#: How long the surface proof waits for a universe to settle, and how often it looks. Three
#: hours takes the proof past any tick that is merely slow (the verdict still lands hours
#: before the delivery report reads it at 07:00). A tick still running after that leaves its
#: head to age in `mart.served_head`, and a later proof fails on a stale scheduled head. This
#: proof names the universe IN-PROGRESS instead of calling its surfaces wrong.
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
#: The confidence report runs for the universes that the process environment schedules (the contract
#: lists them: staging runs TOPT only). `scheduled_universe_names()` gives them at the time of the tick.

MODEL_KEY_HEALTH_JOB_NAME = "model_key_health"


def model_key_health_cron(environment: str) -> str:
    """The daily cron of the key probe of ``environment``. The hour and the minute come from the
    model seat of the environment contract (#1444): the environments probe one shared account, so
    each takes its own slot. The slots start at 06:00 UTC, after every nightly model caller and
    an hour before the delivery report (07:00) reads the verdict, so a daemon that is merely slow
    still lands inside the day's check. The release fetch proof starts at 06:15 and every slot
    ends before it (`test_every_probe_slot_ends_before_the_release_fetch_proof_starts`)."""
    return load_environment_contract().model_seat.probe_cron(environment)


RELEASE_FETCH_PROOF_JOB_NAME = "release_fetch_proof"
# 06:15 UTC: after every scheduled tick of the night (22:15 through the 23:47 canary) has
# finished, and before the delivery report (07:00) reads the verdict. The verdict is pending
# until the deployment's first scheduled or forced tick has finished.
RELEASE_FETCH_PROOF_CRON = "15 6 * * *"

RUNTIME_SHAPE_JOB_NAME = "runtime_shape_check"
# 06:30 UTC: after every scheduled tick and nightly check of the night has finished (the last ones,
# the fetch keys check and the job run health, run at 06:20), and before the delivery report (07:00)
# reads the verdict.
RUNTIME_SHAPE_CRON = "30 6 * * *"

FETCH_KEYS_ONCE_JOB_NAME = "fetch_keys_once_check"
# 06:20 UTC: after the release fetch proof (06:15), and before the runtime shape check (06:30), which
# runs after every other nightly check (#1484). The check judges the UTC day that ended, and the
# ledger of that day is complete at 00:00 UTC.
FETCH_KEYS_ONCE_CRON = "20 6 * * *"

JOB_RUN_HEALTH_JOB_NAME = "job_run_health_check"
# 06:20 UTC: after every tick of the night and after the release fetch proof (06:15) started, and before the
# runtime shape check (06:30). No nightly check may fire from the shape check to the delivery report (07:00)
# (#1484), so this check cannot run after the shape check. It counts the finished runs of the 7 days: the
# shape check of this night counts from the next night.
JOB_RUN_HEALTH_CRON = "20 6 * * *"

#: Verdict names (`mart.nightly_verdicts.check_name`) this lane records.
OUTPUT_INVARIANTS_VERDICT = "output_invariants"
REPORT_SURFACE_VERDICT = "report_surface_proof"
CONFIDENCE_REPORT_VERDICT = "datahub_confidence_report"
MODEL_KEY_HEALTH_VERDICT = "model_key_health"
RELEASE_FETCH_PROOF_VERDICT = "release_fetch_proof"
RUNTIME_SHAPE_VERDICT = "runtime_shape"
FETCH_KEYS_ONCE_VERDICT = "fetch_keys_once"
JOB_RUN_HEALTH_VERDICT = "job_run_health"
NIGHTLY_VERDICTS: tuple[str, ...] = (
    OUTPUT_INVARIANTS_VERDICT,
    REPORT_SURFACE_VERDICT,
    *(check_name(CONFIDENCE_REPORT_VERDICT, universe) for universe in any_environment_universe_names()),
    MODEL_KEY_HEALTH_VERDICT,
    RELEASE_FETCH_PROOF_VERDICT,
    JOB_RUN_HEALTH_VERDICT,
    RUNTIME_SHAPE_VERDICT,
    FETCH_KEYS_ONCE_VERDICT,
)

#: The suite as the image carries it (Dockerfile), or the repository copy for local runs.
_IMAGE_COPY = Path("/app/common/runtime/src/truealpha_tools/output_invariants.py")
_REPO_COPY = (
    Path(__file__).resolve().parents[5] / "common" / "runtime" / "src" / "truealpha_tools" / "output_invariants.py"
)


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
        _await_settled(context)
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
    The output-invariants suite, the surface proof, and the confidence report all judge heads through it."""
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


#: Every served head. The check counts the scheduled heads, so it reads the fresh ones too.
_SERVED_HEADS_SQL = """
select universe_id, freshness, availability, staleness_reason
from mart.served_head
order by universe_id
"""

#: The finding when an environment serves no scheduled head. Zero heads prove nothing about
#: freshness: no run was ever governed, or the identity row hides every head (#28 V3).
NO_SCHEDULED_HEAD = "no scheduled governed head"


def stale_served_heads(
    connection: psycopg.Connection,
    *,
    universe_prefix: str = "",
    environment: str | None = None,
    require_head: bool = False,
) -> list[str]:
    """Return every scheduled served head where `mart.served_head.freshness != 'fresh'` or
    `availability != 'available'` (#1299, #1256, #54).

    With `require_head`, zero scheduled heads is also a finding (#28 V3: unknown is not OK).
    A head with no running schedule in this environment does not count (#1256)."""
    if environment is None:
        try:
            environment = declared_environment(connection)
        except Exception:  # noqa: BLE001 - test doubles that do not answer environment_identity fall back to settings
            environment = settings.app_env
    rows = connection.execute(_SERVED_HEADS_SQL).fetchall()
    out: list[str] = []
    scheduled = 0
    for row in rows:
        universe_id = str(row[0])
        if universe_prefix and not universe_id.startswith(universe_prefix):
            continue
        if not is_head_scheduled(universe_id, environment):
            continue
        scheduled += 1
        freshness = str(row[1])
        availability = str(row[2])
        if freshness == "fresh" and availability == "available":
            continue
        reason = f" ({row[3]})" if len(row) > 3 and row[3] else ""
        out.append(f"{universe_id} freshness={freshness} availability={availability}{reason}")
    if require_head and not scheduled:
        out.append(f"{NO_SCHEDULED_HEAD} in {environment}: freshness is not proven (#28 V3)")
    return out


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
            stale_heads = stale_served_heads(connection, require_head=True)
        for line in summary_lines(surfaces):
            context.log.info(line)
        failed = [surface for surface in surfaces if surface.mismatched]
        settling_surfaces = [surface for surface in surfaces if surface.state == "IN-PROGRESS"]
        status = "pass" if not failed and not stale_heads else "fail"
        context.add_output_metadata(
            {
                "status": status,
                "surfaces": len(surfaces),
                "mismatched": len(failed),
                "in_progress": len(settling_surfaces),
            }
        )
        outcome.summary = (
            verdict_counts(surfaces).split(";", 1)[0]
            + (
                f"; in progress: {', '.join(surface.surface for surface in settling_surfaces)}"
                if settling_surfaces
                else ""
            )
            + (f"; mismatched: {', '.join(surface.surface for surface in failed)}" if failed else "")
            + (f"; stale served_head: {', '.join(stale_heads)}" if stale_heads else "")
        )
        if failed or stale_heads:
            reasons = [surface.line for surface in failed] + [f"mart.served_head {item}" for item in stale_heads]
            raise dg.Failure(
                "report surface proof: "
                + "; ".join(reasons)
                + " — the App shows a head the pointer does not name or "
                + "mart.served_head.freshness != 'fresh' (#855 C1, #1299)"
            )
    return json.dumps({"status": "pass", "surfaces": [surface.line for surface in surfaces]})


@dg.job(name=OUTPUT_INVARIANTS_JOB_NAME, tags={WAITS_FOR_RUNS_TAG: "true"})
def output_invariants_job() -> None:
    # Two independent verdicts in one nightly run: the numbers are possible (the suite), and
    # every surface serves the head the pointer names (the proof). Neither depends on the other, and a
    # failure of one does not stop the other. The in-process executor runs them one after the other;
    # the second wait for a settled universe ends at once when the first saw every universe settle.
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
            stale_heads = stale_served_heads(connection, universe_prefix=UNIVERSE_PREFIXES.get(config.universe, ""))
            if stale_heads:
                outcome.summary = f"stale served_head: {', '.join(stale_heads)}"
                raise dg.Failure(
                    f"datahub confidence report: mart.served_head is not fresh ({', '.join(stale_heads)}) (#1299, #54)"
                )
            report = compile_report(
                connection,
                universe=config.universe,
                executed_at=executed_at,
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
            "status": "pass",
            "report_id": report_id,
            "universe_id": report["universe_id"],
            "run_id": report["run_id"],
            "sources_connected": ", ".join(report["sources_connected"]),
            **{f"{name}_mean_confidence": family["mean_confidence"] for name, family in report["families"].items()},
            "close_agreement_rate": str(report["accuracy"]["close"]["agreement_rate"]),
            "sec_oracle_issuers_compared": report["accuracy"]["sec_oracle"]["issuers_compared"],
        }
    )
    return json.dumps({"status": "pass", "report_id": report_id, "summary": summary_line(report)})


@dg.job(name=CONFIDENCE_REPORT_JOB_NAME, tags={WAITS_FOR_RUNS_TAG: "true"})
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
    for universe in scheduled_universe_names():
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


def build_model_key_health_schedule(environment: str) -> dg.ScheduleDefinition:
    """The daily key probe of ``environment``. The slot comes from the contract (`model_key_health_cron`)."""

    @dg.schedule(
        name="model_key_health_schedule",
        job=model_key_health_job,
        cron_schedule=model_key_health_cron(environment),
        execution_timezone="UTC",
        default_status=dg.DefaultScheduleStatus.RUNNING,
    )
    def model_key_health_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        tick = context.scheduled_execution_time.isoformat()
        return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})

    return model_key_health_schedule


model_key_health_schedule = build_model_key_health_schedule(gateway.capacity_environment())


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


@dg.op
def run_runtime_shape(context: dg.OpExecutionContext) -> str:
    """Judge the memory cgroup of this container against the declared capacity (#1467).

    The run executes in the code server container, so the cgroup it reads is the one that holds the gRPC
    server and every other run. The sensor `runtime_shape_sampler` keeps the totals between two checks, so
    a container that a deploy replaced still counts. A fact that cannot be read is a red verdict, never a
    green one."""
    tick = tick_of(context)
    with verdict(RUNTIME_SHAPE_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick) as outcome:
        judgement = runtime_shape.check(
            context.instance,
            tick=tick.isoformat() if tick is not None else None,
            capacity=load_environment_contract().code_server_capacity,
        )
        context.log.info(f"runtime shape: {judgement.summary}")
        context.add_output_metadata({"red": len(judgement.red), **judgement.facts})
        outcome.summary = judgement.summary
        if judgement.red:
            raise dg.Failure(f"runtime shape: {judgement.summary} (#1467, #28 V5)")
    return json.dumps({"red": list(judgement.red), "summary": judgement.summary})


@dg.job(name=RUNTIME_SHAPE_JOB_NAME)
def runtime_shape_job() -> None:
    run_runtime_shape()


@dg.schedule(
    job=runtime_shape_job,
    cron_schedule=RUNTIME_SHAPE_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def runtime_shape_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


class FetchKeysOnceConfig(dg.Config):
    """``day`` replays the check for one UTC day (ISO 8601 date). Empty means the day before the tick."""

    day: str | None = None


@dg.op
def run_fetch_keys_once(context: dg.OpExecutionContext, config: FetchKeysOnceConfig) -> str:
    """Judge the call ledger of the UTC day that ended: no fetch key fetched twice (#1411 K7, #1418 B2).

    Red names the keys. A day with no successful vendor call is pending, not green."""
    tick = tick_of(context)
    with verdict(FETCH_KEYS_ONCE_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick) as outcome:
        day = fetch_keys_once.judged_day(config.day, tick, now=datetime.now(UTC))
        with psycopg.connect(settings.database_url) as connection:
            judgement = fetch_keys_once.evaluate(connection, day=day)
        context.log.info(f"fetch keys once: {judgement.summary}")
        for key, rows in judgement.duplicates:
            context.log.warning(f"fetch key fetched {rows} times on {day}: {key}")
        for source, endpoint, rows in judgement.unkeyed_by_endpoint:
            context.log.info(f"unkeyed calls on {day}: {source}/{endpoint} x{rows}")
        context.add_output_metadata(
            {
                "day": str(day),
                "keyed": judgement.keyed,
                "unkeyed": judgement.unkeyed,
                "duplicate_keys": len(judgement.duplicates),
            }
        )
        outcome.summary = judgement.summary
        outcome.pending = judgement.pending
        if judgement.red:
            raise dg.Failure(f"fetch keys once: {judgement.summary} (#1411 K7, #1418 B2)")
    return json.dumps({"day": str(day), "red": judgement.red, "summary": judgement.summary})


@dg.job(name=FETCH_KEYS_ONCE_JOB_NAME)
def fetch_keys_once_job() -> None:
    run_fetch_keys_once()


@dg.schedule(
    job=fetch_keys_once_job,
    cron_schedule=FETCH_KEYS_ONCE_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def fetch_keys_once_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


RUNTIME_SHAPE_SAMPLER_NAME = "runtime_shape_sampler"
RUNTIME_SHAPE_SAMPLER_INTERVAL_SECONDS = runtime_shape.SAMPLE_INTERVAL_SECONDS


@dg.sensor(
    name=RUNTIME_SHAPE_SAMPLER_NAME,
    minimum_interval_seconds=RUNTIME_SHAPE_SAMPLER_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def runtime_shape_sampler_sensor(context: dg.SensorEvaluationContext):
    """Fold the memory cgroup of the code server into the totals of `runtime_shape` (#1467).

    Dagster evaluates a sensor inside the code server process, so the cgroup it reads is the one that holds
    every run. A deploy replaces the container and its counters; this sensor keeps what the old container
    showed, with a loss of at most one interval. It launches no run."""
    tracker = runtime_shape.sample(
        context.instance,
        deployment=boot_deployment(),
        capacity=load_environment_contract().code_server_capacity,
    )
    yield dg.SkipReason(
        f"sampled: {tracker.kills} OOM kill(s), {tracker.peaks_over_limit} peak(s) over the limit,"
        f" {tracker.processes_over_size} run process(es) and {tracker.servers_over_size} server(s) over their size so far"
    )


class _RedJobRunHealth(dg.Failure):
    """The verdict is red. `verdict()` records it and re-raises; the op catches this and ends green."""


@dg.op
def run_job_run_health(context: dg.OpExecutionContext) -> str:
    """Judge the finished runs of every job in the last 7 days against the rest of the fleet (#1466).

    A red verdict is a dashboard row, not a gate: the op records it and ends green, so this job does not add
    its own failure to the history that it judges. A history with no finished run is pending, never green."""
    tick = tick_of(context)
    now = tick or datetime.now(UTC)
    judgement = None
    try:
        with verdict(JOB_RUN_HEALTH_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick) as outcome:
            with psycopg.connect(settings.database_url, autocommit=True) as connection:
                runs = job_run_health.read_runs(connection, now=now)
            judgement = job_run_health.judge(runs, now=now)
            context.log.info(f"job run health: {judgement.summary}")
            context.add_output_metadata({"jobs": len(judgement.jobs), "red": len(judgement.red)})
            outcome.summary = judgement.summary
            if not judgement.jobs:
                outcome.pending = True
                outcome.summary = f"no finished run in {job_run_health.WINDOW_DAYS} days; nothing to judge"
            if judgement.red:
                raise _RedJobRunHealth(f"job run health: {judgement.summary} (#1466)")
    except _RedJobRunHealth as red:
        context.log.warning(str(red))
    assert judgement is not None
    return json.dumps({"red": [job.job for job in judgement.red], "summary": judgement.summary})


@dg.job(name=JOB_RUN_HEALTH_JOB_NAME)
def job_run_health_job() -> None:
    run_job_run_health()


@dg.schedule(
    job=job_run_health_job,
    cron_schedule=JOB_RUN_HEALTH_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def job_run_health_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


OUTPUT_INVARIANTS_SENSOR_NAME = "output_invariants_on_advance"
OUTPUT_INVARIANTS_SENSOR_INTERVAL_SECONDS = 60


@dg.sensor(
    name=OUTPUT_INVARIANTS_SENSOR_NAME,
    job=output_invariants_job,
    minimum_interval_seconds=OUTPUT_INVARIANTS_SENSOR_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def output_invariants_on_advance_sensor(context: dg.SensorEvaluationContext):
    """Re-evaluate `output_invariants_job` when a new build or governed head lands while
    `output_invariants` or `report_surface_proof` is red (#1346, #1295, #1227).

    Instead of waiting up to 24 hours for the 00:15 UTC cron after a fix deploys or a
    governed pointer advances, this sensor checks whether the newest verdict is red and,
    if so, launches one run per `(image_digest, head_run_ids)` state (`run_key`).
    """
    build = (settings.data_engine_image_digest or "").strip() or "unversioned"
    try:
        with psycopg.connect(settings.database_url) as connection:
            rows = connection.execute(
                "select universe_id, coalesce(run_id, head_run_id, '') from mart.served_head order by universe_id"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - sensor never crashes the daemon on a transient DB blip
        yield dg.SkipReason(f"cannot read mart.served_head: {type(exc).__name__}")
        return
    heads = ",".join(f"{u}={r}" for u, r in rows)
    state_key = f"heal:{build[:19]}:{heads}"
    if context.cursor == state_key:
        yield dg.SkipReason("already evaluated on current build and governed heads")
        return
    red = [name for name in (OUTPUT_INVARIANTS_VERDICT, REPORT_SURFACE_VERDICT) if newest_is_red(name)]
    context.update_cursor(state_key)
    if not red:
        yield dg.SkipReason("newest output_invariants and report_surface_proof verdicts are green")
        return
    tick = datetime.now(UTC).replace(microsecond=0).isoformat()
    yield dg.RunRequest(
        run_key=state_key,
        tags={TICK_TAG: tick},
    )


defs = dg.Definitions(
    jobs=[
        output_invariants_job,
        datahub_confidence_report_job,
        model_key_health_job,
        release_fetch_proof_job,
        runtime_shape_job,
        fetch_keys_once_job,
        job_run_health_job,
    ],
    schedules=[
        output_invariants_schedule,
        datahub_confidence_report_schedule,
        model_key_health_schedule,
        release_fetch_proof_schedule,
        runtime_shape_schedule,
        fetch_keys_once_schedule,
        job_run_health_schedule,
    ],
    sensors=[output_invariants_on_advance_sensor, runtime_shape_sampler_sensor],
)
