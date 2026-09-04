"""The datahub capture lane: real-source ticks and their schedules (#731 split of
`data_engine.dagster_defs`; the behaviour is unchanged, only the file moved).

Each tick drives one universe through the generic capture executor and its
per-semantic adapters, freezes + materializes, persists the quality report and
advances that universe's governed pointer only if the report meets its declared
service objectives (#536). TOPT additionally seeds the strategy inputs and runs the
frozen strategy (#27/#171). No fixture data is seeded anywhere in this lane (#429 I2).

Hermeticity: no database or network work at import; every op opens its connection
lazily from DATABASE_URL. `executed_at` comes from the schedule's tick time, never the
wall clock: distinct ticks -> distinct content-addressed runs; a retried tick
reproduces the same identities (idempotent retry).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.a1_evidence import ACCEPTED_SERVICE_OBJECTIVES, ServiceObjectives, register_run_evidence
from data_engine.datahub.production_topt.composition import live_version_for, run_topt_pipeline
from data_engine.datahub.strategy_bridge import (
    persist_strategy_input_coverage,
    run_strategy_replay_for_cutoff,
    seed_strategy_inputs_from_capture,
)
from data_engine.sources import gateway

TOPT_LIVE_JOB_NAME = "topt_live_pipeline"


def live_topt_cron(app_env: str) -> str:
    """Daily, after the US close (2h+ in EDT, 5h+ in EST), so same-day closes and
    facts are settled — and 30 minutes APART per environment.

    Both environments used to fire at the same instant against ONE shared Twelve
    Data key whose free tier caps requests per MINUTE at 8. Two simultaneous ticks
    put ~15 req/min on that ceiling and each environment lost ~6 of its 21
    second-origin cells to rate limiting (2026-08-15/16 scheduled ticks: prod and
    staging both 15/21 agreed, 15+15 of 42 twelvedata objects landing in the shared
    window) — which froze the governed head for three days straight, because the
    #536 gate correctly refuses a run whose weakest cell lost corroboration. #491
    sized the DAILY budget; the per-minute collision of two same-instant consumers
    is what this offset removes. 22:45 is still comfortably after the close, so the
    staleness argument above holds for both environments.

    #27's two-consecutive-cycles proof was gathered on a temporary hourly cadence
    and is complete in both environments; hourly ALSO over-spent the shared daily
    tier once production joined staging (21 fetches/tick x 24 x 2 envs = 1008/day
    against an 800/day budget, #491). Daily spends 42/day across both. Testing or
    retry never needs a faster cron: launch the same job manually with an explicit
    `executed_at`.
    """
    # Alias-safe: a deploy configured APP_ENV=prod must never silently take the
    # staging slot and re-create the collision (Copilot review on #574). Only two
    # environments run this schedule; anything unrecognized shares staging's slot,
    # where a surprise collision costs a staging tick, not the production head.
    normalized = app_env.strip().lower()
    return "15 22 * * *" if normalized in ("production", "prod") else "45 22 * * *"


TOPT_LIVE_CRON = live_topt_cron(settings.app_env)


class ToptLiveTickConfig(dg.Config):
    """`executed_at` is injected by the schedule from its tick time (ISO 8601),
    never read from the wall clock inside the run."""

    executed_at: str


def _production_only(app_env: str) -> dg.DefaultScheduleStatus:
    """Production-only, by owner directive (2026-08-17: staging needs the TOPT tick and
    nothing more): outside production the schedule defaults to STOPPED and the job runs
    there only through the manual trigger."""
    return (
        dg.DefaultScheduleStatus.RUNNING
        if app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    )


@dataclass(frozen=True)
class UniverseTick:
    """One universe's scheduled tick, declared (#72 scope 4). The factory below turns a
    declaration into the op, the job and (when `cron` is set) the schedule; a new
    universe is one entry in `TICKS`, not sixty copied lines."""

    key: str
    job_name: str
    op_name: str
    schedule_name: str
    log_label: str
    #: None means the hand-curated TOPT corpus; otherwise a governed universe head.
    universe_head_kind: str | None
    label_prefix: str
    objectives: ServiceObjectives | None
    #: TOPT additionally seeds the strategy inputs and runs the frozen strategy;
    #: data-only universes serve prices, facts and valuation context, not selections.
    run_strategy: bool
    cron: str | None
    default_status: dg.DefaultScheduleStatus = dg.DefaultScheduleStatus.RUNNING


QQQ_LIVE_JOB_NAME = "qqq_live_pipeline"
# 23:20 UTC: after prod TOPT (22:15, ~7 min) and staging TOPT (22:45, ~7 min), so the
# three consumers of the shared 8-requests-per-minute Twelve Data key never overlap
# (#574's collision class, third consumer edition). A 102-listing tick at the 8s
# throttle runs ~15-30 minutes and finishes before midnight UTC.
QQQ_LIVE_CRON = "20 23 * * *"
QQQ_UNIVERSE_HEAD = "universe-list:qqq"
# Phase-1 service objectives for the QQQ universe, declared where the gate reads them
# rather than silently reusing TOPT's demand: ~90 of 101 issuers have no headcount
# fact yet (#564 fills them; the plane accepts inserts, never deploys) and a handful
# of foreign filers report under IFRS the ruleset does not map yet — those cells
# grade honestly unavailable, and a 0.95 availability floor would freeze the QQQ
# pointer on day one for gaps we have chosen to serve honestly. The corroboration
# band and coverage stay at full strength; availability rises back to 0.95 when the
# headcount plane covers the universe (tracked on #578/#564).
QQQ_PHASE1_OBJECTIVES = ServiceObjectives(
    minimum_coverage=ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage,
    minimum_availability=Decimal("0.70"),
    minimum_confidence_score=ACCEPTED_SERVICE_OBJECTIVES.minimum_confidence_score,
    minimum_independent_origin_groups=ACCEPTED_SERVICE_OBJECTIVES.minimum_independent_origin_groups,
    # 0.90 for a 102-listing universe with one primary+one corroborating vendor:
    # per-symbol publication-tail stragglers (#622's same-day narrowing grades them
    # insufficient, not conflict) should not hold 100+ corroborated cells behind the
    # previous head. Rises to the accepted 0.95 with the second origin's coverage.
    minimum_corroborated_share=Decimal("0.90"),
)

CANARY_JOB_NAME = "canary_live_pipeline"
CANARY_UNIVERSE_HEAD = "universe-list:canary"
# The canary judges itself by NAMED ORACLES (scripts/canary_assert.py), not by the
# pointer: its five issuers span every operating branch, ASML's honest headcount
# hole caps availability by design, and a post-deploy run may execute at any hour
# (mid-session captures grade insufficient under #625). The floors here only stop
# a catastrophic run from advancing the canary's own head; they are deliberately
# NOT the verification bar.
CANARY_OBJECTIVES = ServiceObjectives(
    minimum_coverage=ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage,
    minimum_availability=Decimal("0.70"),
    minimum_confidence_score=ACCEPTED_SERVICE_OBJECTIVES.minimum_confidence_score,
    minimum_independent_origin_groups=ACCEPTED_SERVICE_OBJECTIVES.minimum_independent_origin_groups,
    minimum_corroborated_share=Decimal("0"),
)

# 23:47 UTC: 27 minutes after the QQQ tick, so the canary's overlap names (AAPL,
# GOOGL, GOOG, ASML) reuse the night's committed observations under #635 and the
# daily proof costs ~2 vendor calls (HBAN/CINF only). Seven deploy-free days in a
# row meant seven days with ZERO full freeze→publish executions — a
# deploy-triggered verifier only verifies when you deploy; this makes the full
# chain prove itself nightly in BOTH environments (#648/#671).
CANARY_DAILY_CRON = "47 23 * * *"


TICKS: tuple[UniverseTick, ...] = (
    UniverseTick(
        key="topt",
        job_name=TOPT_LIVE_JOB_NAME,
        op_name="run_topt_live_tick",
        schedule_name="topt_live_schedule",
        log_label="topt live tick",
        universe_head_kind=None,
        label_prefix="production-topt",
        objectives=None,
        run_strategy=True,
        cron=TOPT_LIVE_CRON,
        # ENABLED by default: #27's appended acceptance (issue comment, 2026-07-20)
        # requires the schedule running in Staging; enabling it is the deliberate,
        # owner-authorized operator action recorded there — not an accidental default.
        default_status=dg.DefaultScheduleStatus.RUNNING,
    ),
    UniverseTick(
        key="qqq",
        job_name=QQQ_LIVE_JOB_NAME,
        op_name="run_qqq_live_tick",
        schedule_name="qqq_live_schedule",
        log_label="qqq live tick",
        universe_head_kind=QQQ_UNIVERSE_HEAD,
        label_prefix="production-qqq",
        objectives=QQQ_PHASE1_OBJECTIVES,
        run_strategy=False,
        cron=QQQ_LIVE_CRON,
        default_status=_production_only(settings.app_env),
    ),
    UniverseTick(
        key="canary",
        job_name=CANARY_JOB_NAME,
        op_name="run_canary_live_tick",
        schedule_name="canary_daily_schedule",
        log_label="canary tick",
        universe_head_kind=CANARY_UNIVERSE_HEAD,
        label_prefix="production-canary",
        objectives=CANARY_OBJECTIVES,
        run_strategy=False,
        cron=CANARY_DAILY_CRON,
        default_status=dg.DefaultScheduleStatus.RUNNING,
    ),
)


def _run_tick(context: dg.OpExecutionContext, config: ToptLiveTickConfig, tick: UniverseTick) -> str:
    """The one tick body every universe runs: capture → freeze → materialize → report →
    governed pointer, plus the strategy replay where the declaration asks for it."""
    cutoff = datetime.fromisoformat(config.executed_at)
    version = live_version_for(cutoff)
    # Lazy, run-time connection (DATABASE_URL). One transaction for the whole tick:
    # a mid-run failure leaves no partial run; the daemon's retry re-runs the tick
    # against the same content-addressed identities. Every vendor call inside is
    # attributed to this Dagster run in the external call ledger (#729).
    with gateway.run_scope(f"dagster:{context.run_id}"), psycopg.connect(settings.database_url) as connection:
        pipeline = run_topt_pipeline(
            connection,
            cutoff=cutoff,
            version=version,
            universe_head_kind=tick.universe_head_kind,
            label_prefix=tick.label_prefix,
        )
        strategy: dict[str, Any] = {}
        if tick.run_strategy:
            seeded = seed_strategy_inputs_from_capture(connection, pipeline.run_id, cutoff=cutoff)
            # #496: the L2 funnel metric, same transaction as the seed it measures.
            l2_complete, l2_total = persist_strategy_input_coverage(connection, pipeline.run_id, cutoff=cutoff)
            strategy_run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
                connection, cutoff=cutoff, executed_at=cutoff, risk_free_rate=Decimal("0.05")
            )
            strategy = {
                "strategy_inputs_seeded": seeded,
                "l2_input_coverage": f"{l2_complete}/{l2_total}",
                "strategy_run_id": strategy_run_id,
                "decision_count": decision_count,
                "snapshot_id": snapshot_id,
            }
        # #378: register the run on the A1 evidence plane and advance the governed
        # pointer inside the same transaction, so consumers resolve THIS run through
        # mart.current_pointer_head the moment the tick commits. #536: the advance is
        # gated on THIS run's report — a run that misses a declared service objective
        # commits its run, report and evidence but does not become the served head.
        registration = register_run_evidence(
            connection,
            run_id=pipeline.run_id,
            release_manifest_id=pipeline.release_manifest_id,
            quality_report=pipeline.quality,
            **({"objectives": tick.objectives} if tick.objectives is not None else {}),
        )
        connection.commit()

    summary = (
        f"{tick.log_label} {config.executed_at}: capture {pipeline.run_id} "
        f"(available {pipeline.quality['available_count']}/{pipeline.quality['requested_count']}"
    )
    if tick.run_strategy:
        summary += (
            f", reconciliation {pipeline.quality['independent_reconciliation']}), "
            f"{strategy['strategy_inputs_seeded']} strategy inputs, strategy run "
            f"{strategy['strategy_run_id']} ({strategy['decision_count']} decisions)"
        )
    else:
        summary += ")"
    if registration.accepted:
        context.log.info(f"{summary}; pointer sequence {registration.sequence}")
    else:
        # #536 acceptance: which objective failed is readable from the run log and the
        # op's output metadata, without opening the database.
        context.log.warning(
            f"{summary}; POINTER WITHHELD at sequence {registration.sequence} "
            f"— unmet service objectives: {registration.summary}"
        )
    metadata: dict[str, Any] = {
        "capture_run_id": pipeline.run_id,
        "quality_report_id": pipeline.quality_report_id,
        "independent_reconciliation": pipeline.quality["independent_reconciliation"],
        **strategy,
        "pointer_advanced": registration.accepted,
        "pointer_sequence": registration.sequence,
        "unmet_service_objectives": registration.summary,
    }
    context.add_output_metadata(metadata)
    return pipeline.run_id


def build_tick(tick: UniverseTick) -> tuple[dg.OpDefinition, dg.JobDefinition, dg.ScheduleDefinition | None]:
    """Op, job and schedule for one declaration. Op and job names are the declaration's:
    operators launch by them and the trigger sensor dispatches by them."""

    @dg.op(name=tick.op_name)
    def tick_op(context: dg.OpExecutionContext, config: ToptLiveTickConfig) -> str:
        return _run_tick(context, config, tick)

    @dg.job(name=tick.job_name)
    def tick_job() -> None:
        tick_op()

    schedule = None
    if tick.cron is not None:

        @dg.schedule(
            name=tick.schedule_name,
            job=tick_job,
            cron_schedule=tick.cron,
            execution_timezone="UTC",
            default_status=tick.default_status,
        )
        def tick_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
            executed_at = context.scheduled_execution_time.isoformat()
            return dg.RunRequest(
                # run_key == the tick time: the daemon dedupes a re-evaluated tick to a
                # single run, so an identical tick retry is idempotent.
                run_key=executed_at,
                run_config=dg.RunConfig(ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at)}),
            )

        schedule = tick_schedule
    return tick_op, tick_job, schedule


_BUILT = {tick.key: build_tick(tick) for tick in TICKS}
TICK_BY_JOB: dict[str, UniverseTick] = {tick.job_name: tick for tick in TICKS}

# The names operators, tests and the trigger sensor address, unchanged from the
# hand-written era.
run_topt_live_tick, topt_live_pipeline_job, topt_live_schedule = _BUILT["topt"]
run_qqq_live_tick, qqq_live_pipeline_job, qqq_live_schedule = _BUILT["qqq"]
run_canary_live_tick, canary_live_pipeline_job, canary_daily_schedule = _BUILT["canary"]

defs = dg.Definitions(
    jobs=[job for _, job, _ in _BUILT.values()],
    schedules=[schedule for _, _, schedule in _BUILT.values() if schedule is not None],
)
