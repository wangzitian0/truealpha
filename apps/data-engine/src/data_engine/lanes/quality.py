"""Data-quality checks against environment database (#674, #725, #581)."""

import contextlib
import io
import json
import runpy
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.universes import key_of_lane, lane_name, record, served_keys, universe_with_role

from data_engine.config import settings
from data_engine.datahub.oracle_outcomes import load_oracle_outcomes_rollup, record_oracle_outcomes_safely
from data_engine.datahub.question_coverage import NO_HEAD_SUMMARY, declared_environment
from data_engine.environment_universes import any_environment_universe_names, scheduled_universe_names
from data_engine.external_op import external_op
from data_engine.lanes.capture import TICKS
from data_engine.lanes.standards import HEAD_REPORTS_JOB_NAME, STANDARD_BACKFILL_JOB_NAME, run_universe
from data_engine.quality import job_run_health, surface_proof, topt_baseline
from data_engine.quality.nightly_verdicts import (
    TICK_TAG,
    check_name,
    newest_is_red,
    tick_from_config,
    tick_of,
    verdict,
)
from data_engine.sources import gateway

WAITS_FOR_RUNS_TAG = "truealpha/waits_for_runs"

OUTPUT_INVARIANTS_JOB_NAME = "output_invariants_check"
OUTPUT_INVARIANTS_CRON = "15 0 * * *"
SURFACE_SETTLE_TIMEOUT = timedelta(hours=3)
SURFACE_SETTLE_POLL_SECONDS = 60.0
HEAD_REPORTS_GRACE = timedelta(minutes=10)

CONFIDENCE_REPORT_JOB_NAME = "datahub_confidence_report"
CONFIDENCE_REPORT_CRON = "45 0 * * *"

MODEL_KEY_HEALTH_JOB_NAME = "model_key_health"


def model_key_health_cron(environment: str) -> str:
    return load_environment_contract().model_seat.probe_cron(environment)


JOB_RUN_HEALTH_JOB_NAME = "job_run_health_check"
JOB_RUN_HEALTH_CRON = "20 6 * * *"

TOPT_BASELINE_JOB_NAME = "topt_baseline_check"
TOPT_BASELINE_CRON = "25 6 * * *"

OUTPUT_INVARIANTS_VERDICT = "output_invariants"
REPORT_SURFACE_VERDICT = "report_surface_proof"
CONFIDENCE_REPORT_VERDICT = "datahub_confidence_report"
MODEL_KEY_HEALTH_VERDICT = "model_key_health"
JOB_RUN_HEALTH_VERDICT = "job_run_health"
TOPT_BASELINE_VERDICT = "topt_baseline"
NIGHTLY_VERDICTS: tuple[str, ...] = (
    OUTPUT_INVARIANTS_VERDICT,
    REPORT_SURFACE_VERDICT,
    *(check_name(CONFIDENCE_REPORT_VERDICT, universe) for universe in any_environment_universe_names()),
    MODEL_KEY_HEALTH_VERDICT,
    JOB_RUN_HEALTH_VERDICT,
    TOPT_BASELINE_VERDICT,
)

_IMAGE_COPY = Path("/app/common/runtime/src/truealpha_tools/output_invariants.py")
_REPO_COPY = (
    Path(__file__).resolve().parents[5] / "common" / "runtime" / "src" / "truealpha_tools" / "output_invariants.py"
)


def _suite_path() -> Path:
    for candidate in (_IMAGE_COPY, _REPO_COPY):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"output_invariants.py is neither at {_IMAGE_COPY} nor {_REPO_COPY}")


@external_op
def run_output_invariants(context: dg.OpExecutionContext) -> str:
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


UNFINISHED_RUN_STATUSES = (
    dg.DagsterRunStatus.QUEUED,
    dg.DagsterRunStatus.NOT_STARTED,
    dg.DagsterRunStatus.STARTING,
    dg.DagsterRunStatus.STARTED,
    dg.DagsterRunStatus.CANCELING,
)
IN_FLIGHT_MAX_AGE = timedelta(hours=12)


def runs_in_flight(instance: dg.DagsterInstance, *, now: datetime | None = None) -> dict[str, str]:
    created_after = (now or datetime.now(UTC)) - IN_FLIGHT_MAX_AGE
    served = {lane_name(key) for key in served_keys()}
    ticks = {tick.job_name: tick.lane_name for tick in TICKS if tick.lane_name in served}
    found: dict[str, str] = {}
    for job_name in (*ticks, HEAD_REPORTS_JOB_NAME, STANDARD_BACKFILL_JOB_NAME):
        runs = instance.get_runs(
            filters=dg.RunsFilter(
                job_name=job_name, statuses=list(UNFINISHED_RUN_STATUSES), created_after=created_after
            )
        )
        for run in runs:
            universe = ticks.get(job_name) or run_universe(run.run_config or {})
            if universe in served:
                found.setdefault(
                    universe, f"{job_name} run {run.run_id[:8]} is {run.status.value.lower().replace('_', ' ')}"
                )
    return found


def settling(connection: psycopg.Connection, instance: dg.DagsterInstance, *, now: datetime) -> dict[str, str]:
    return {
        **surface_proof.fresh_heads_without_reports(connection, now=now, grace=HEAD_REPORTS_GRACE),
        **runs_in_flight(instance, now=now),
    }


_sleep = time.sleep


def _await_settled(context: dg.OpExecutionContext) -> None:
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


_SERVED_HEADS_SQL = (
    "select universe_id, freshness, availability, staleness_reason from mart.served_head order by universe_id"
)

NO_SCHEDULED_HEAD = "no scheduled governed head"


def stale_served_heads(
    connection: psycopg.Connection,
    *,
    universe_prefix: str = "",
    environment: str | None = None,
    require_head: bool = False,
) -> list[str]:
    if environment is None:
        try:
            environment = declared_environment(connection)
        except Exception:  # noqa: BLE001
            environment = settings.app_env
    out: list[str] = []
    scheduled = 0
    contract = load_environment_contract()
    for row in connection.execute(_SERVED_HEADS_SQL).fetchall():
        universe_id = str(row[0])
        if universe_prefix and not universe_id.startswith(universe_prefix):
            continue
        if not contract.is_head_scheduled(universe_id, environment):
            continue
        scheduled += 1
        freshness, availability = str(row[1]), str(row[2])
        if freshness == "fresh" and availability == "available":
            continue
        reason = f" ({row[3]})" if len(row) > 3 and row[3] else ""
        out.append(f"{universe_id} freshness={freshness} availability={availability}{reason}")
    if require_head and not scheduled:
        out.append(f"{NO_SCHEDULED_HEAD} in {environment}: freshness is not proven (#28 V3)")
    return out


@external_op
def run_report_surface_proof(context: dg.OpExecutionContext) -> str:
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
        context.add_output_metadata(
            {
                "status": "pass" if not failed and not stale_heads else "fail",
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
    run_output_invariants()
    run_report_surface_proof()


def _tick_schedule(name: str, job: dg.JobDefinition, cron_schedule: str) -> dg.ScheduleDefinition:
    @dg.schedule(
        name=name,
        job=job,
        cron_schedule=cron_schedule,
        execution_timezone="UTC",
        default_status=dg.DefaultScheduleStatus.RUNNING,
    )
    def _schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        tick = context.scheduled_execution_time.isoformat()
        return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})

    return _schedule


output_invariants_schedule = _tick_schedule("output_invariants_schedule", output_invariants_job, OUTPUT_INVARIANTS_CRON)


class ConfidenceReportConfig(dg.Config):
    executed_at: str
    universe: str = lane_name(universe_with_role("fund_consolidation"))


def _oracle_comparators_of(connection: Any, report: Mapping[str, Any]) -> dict[str, int]:
    raw_comparators = report.get("comparators")
    if raw_comparators is None:
        rollup = report.get("oracle_outcomes_rollup") or report.get("oracle_outcomes")
        if isinstance(rollup, Mapping) and "comparators" in rollup:
            raw_comparators = rollup["comparators"]
    if raw_comparators is None:
        raw_comparators = load_oracle_outcomes_rollup(connection, run_id=str(report["run_id"]))["comparators"]
    if not isinstance(raw_comparators, Mapping):
        return {}
    return {
        str(name): int(item.get("compared", 0) if isinstance(item, Mapping) else item)
        for name, item in raw_comparators.items()
    }


@external_op
def run_confidence_report(context: dg.OpExecutionContext, config: ConfidenceReportConfig) -> str:
    from data_engine.datahub.confidence_report import compile_report, persist, summary_line

    executed_at = datetime.fromisoformat(config.executed_at)
    with verdict(
        check_name(CONFIDENCE_REPORT_VERDICT, config.universe),
        registered=NIGHTLY_VERDICTS,
        run_id=context.run_id,
        tick=tick_from_config(config.executed_at),
    ) as outcome:
        _await_settled(context)
        with psycopg.connect(settings.database_url) as connection:
            stale_heads = stale_served_heads(connection, universe_prefix=record(key_of_lane(config.universe)).id_prefix)
            if stale_heads:
                outcome.summary = f"stale served_head: {', '.join(stale_heads)}"
                raise dg.Failure(
                    f"datahub confidence report: mart.served_head is not fresh ({', '.join(stale_heads)}) (#1299, #54)"
                )
            report = compile_report(connection, universe=config.universe, executed_at=executed_at)
            if report is None:
                context.log.warning("no governed head for %s; no confidence report", config.universe)
                outcome.pending = True
                outcome.summary = NO_HEAD_SUMMARY
            else:
                report_id = persist(connection, report)
                connection.commit()
                oracle_rows = record_oracle_outcomes_safely(
                    connection, report["run_id"], compared_at=executed_at, log=context.log
                )
                comparators = _oracle_comparators_of(connection, report)
                if not comparators:
                    outcome.pending = True
                    outcome.summary = f"no comparator for {report['universe_id']}"
                else:
                    zero_comparators = sorted(name for name, count in comparators.items() if count <= 0)
                    if zero_comparators:
                        outcome.summary = (
                            f"comparator {', '.join(zero_comparators)} compared 0 rows for {report['universe_id']}"
                        )
                        raise dg.Failure(f"datahub confidence report: {outcome.summary} (#1523)")
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
            "oracle_outcomes_written": oracle_rows is not None,
            "oracle_outcomes_rows": oracle_rows or 0,
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
    t = context.scheduled_execution_time.isoformat()
    return [
        dg.RunRequest(
            run_key=f"{t}:{u}",
            run_config=dg.RunConfig(ops={"run_confidence_report": ConfidenceReportConfig(executed_at=t, universe=u)}),
        )
        for u in scheduled_universe_names()
    ]


@external_op
def run_model_key_health(context: dg.OpExecutionContext) -> str:
    from data_engine.quality.model_key_health import probe

    with verdict(
        MODEL_KEY_HEALTH_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
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
    return _tick_schedule("model_key_health_schedule", model_key_health_job, model_key_health_cron(environment))


model_key_health_schedule = build_model_key_health_schedule(gateway.capacity_environment())


class _RedDashboardVerdict(dg.Failure):
    pass


def _run_dashboard_verdict(
    context: dg.OpExecutionContext,
    verdict_name: str,
    evaluate: Callable[[Any, datetime], tuple[dict[str, Any], dict[str, Any], str | None]],
) -> str:
    payload: dict[str, Any] = {}
    tick = tick_of(context)
    now = tick or datetime.now(UTC)
    try:
        with verdict(verdict_name, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick) as outcome:
            metadata, payload, red_msg = evaluate(outcome, now)
            context.add_output_metadata(metadata)
            if red_msg is not None:
                raise _RedDashboardVerdict(red_msg)
    except _RedDashboardVerdict as red:
        context.log.warning(str(red))
    return json.dumps(payload)


@external_op
def run_job_run_health(context: dg.OpExecutionContext) -> str:
    def _evaluate(outcome: Any, now: datetime) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        with psycopg.connect(settings.database_url, autocommit=True) as connection:
            runs = job_run_health.read_runs(connection, now=now)
        judgement = job_run_health.judge(runs, now=now)
        context.log.info(f"job run health: {judgement.summary}")
        outcome.summary = judgement.summary
        if not judgement.jobs:
            outcome.pending = True
            outcome.summary = f"no finished run in {job_run_health.WINDOW_DAYS} days; nothing to judge"
        red_msg = f"job run health: {judgement.summary} (#1466)" if judgement.red else None
        return (
            {"jobs": len(judgement.jobs), "red": len(judgement.red)},
            {"red": [job.job for job in judgement.red], "summary": judgement.summary},
            red_msg,
        )

    return _run_dashboard_verdict(context, JOB_RUN_HEALTH_VERDICT, _evaluate)


@dg.job(name=JOB_RUN_HEALTH_JOB_NAME)
def job_run_health_job() -> None:
    run_job_run_health()


job_run_health_schedule = _tick_schedule("job_run_health_schedule", job_run_health_job, JOB_RUN_HEALTH_CRON)


@external_op
def run_topt_baseline(context: dg.OpExecutionContext) -> str:
    contract = load_environment_contract()

    def _evaluate(outcome: Any, now: datetime) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        with psycopg.connect(settings.database_url, autocommit=True) as connection:
            gppe = topt_baseline.read_q1(connection)
            quota = topt_baseline.read_quota(
                connection, now=now, environment=gateway.capacity_environment(), contract=contract
            )
        judgement = topt_baseline.judge(
            gppe=gppe,
            quota=quota,
            server=topt_baseline.read_server(budget_mib=contract.code_server_capacity.server_mib),
            schema=topt_baseline.read_schema(settings.database_url),
        )
        context.log.info(f"topt baseline: {judgement.summary}")
        outcome.summary = judgement.summary
        red_msg = None if judgement.ok else f"topt baseline: {judgement.summary} (#1664)"
        return (
            {"failed": len(judgement.failed), "not_measured": len(judgement.unmeasured)},
            {"failed": [row.key for row in judgement.failed], "summary": judgement.summary},
            red_msg,
        )

    return _run_dashboard_verdict(context, TOPT_BASELINE_VERDICT, _evaluate)


@dg.job(name=TOPT_BASELINE_JOB_NAME)
def topt_baseline_job() -> None:
    run_topt_baseline()


topt_baseline_schedule = _tick_schedule("topt_baseline_schedule", topt_baseline_job, TOPT_BASELINE_CRON)


OUTPUT_INVARIANTS_SENSOR_NAME = "output_invariants_on_advance"
OUTPUT_INVARIANTS_SENSOR_INTERVAL_SECONDS = 60


@dg.sensor(
    name=OUTPUT_INVARIANTS_SENSOR_NAME,
    job=output_invariants_job,
    minimum_interval_seconds=OUTPUT_INVARIANTS_SENSOR_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def output_invariants_on_advance_sensor(context: dg.SensorEvaluationContext):
    build = (settings.data_engine_image_digest or "").strip() or "unversioned"
    try:
        with psycopg.connect(settings.database_url) as connection:
            rows = connection.execute(
                "select universe_id, coalesce(run_id, head_run_id, '') from mart.served_head order by universe_id"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        yield dg.SkipReason(f"cannot read mart.served_head: {type(exc).__name__}")
        return
    state_key = f"heal:{build[:19]}:{','.join(f'{u}={r}' for u, r in rows)}"
    if context.cursor == state_key:
        yield dg.SkipReason("already evaluated on current build and governed heads")
        return
    red = [name for name in (OUTPUT_INVARIANTS_VERDICT, REPORT_SURFACE_VERDICT) if newest_is_red(name)]
    context.update_cursor(state_key)
    if not red:
        yield dg.SkipReason("newest output_invariants and report_surface_proof verdicts are green")
        return
    tick = datetime.now(UTC).replace(microsecond=0).isoformat()
    yield dg.RunRequest(run_key=state_key, tags={TICK_TAG: tick})


defs = dg.Definitions(
    jobs=[
        output_invariants_job,
        datahub_confidence_report_job,
        model_key_health_job,
        job_run_health_job,
        topt_baseline_job,
    ],
    schedules=[
        output_invariants_schedule,
        datahub_confidence_report_schedule,
        model_key_health_schedule,
        job_run_health_schedule,
        topt_baseline_schedule,
    ],
    sensors=[output_invariants_on_advance_sensor],
)
