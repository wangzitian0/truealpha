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

from datetime import datetime
from decimal import Decimal

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


@dg.op
def run_topt_live_tick(context: dg.OpExecutionContext, config: ToptLiveTickConfig) -> str:
    cutoff = datetime.fromisoformat(config.executed_at)
    version = live_version_for(cutoff)

    # Lazy, run-time connection (DATABASE_URL). One transaction for the whole tick:
    # a mid-run failure leaves no partial run; the daemon's retry re-runs the tick
    # against the same content-addressed identities.
    with psycopg.connect(settings.database_url) as connection:
        pipeline = run_topt_pipeline(connection, cutoff=cutoff, version=version)
        seeded = seed_strategy_inputs_from_capture(connection, pipeline.run_id, cutoff=cutoff)
        # #496: the L2 funnel metric, same transaction as the seed it measures.
        l2_complete, l2_total = persist_strategy_input_coverage(connection, pipeline.run_id, cutoff=cutoff)
        strategy_run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
            connection, cutoff=cutoff, executed_at=cutoff, risk_free_rate=Decimal("0.05")
        )
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
        )
        connection.commit()

    tick = (
        f"topt live tick {config.executed_at}: capture {pipeline.run_id} "
        f"(available {pipeline.quality['available_count']}/{pipeline.quality['requested_count']}, "
        f"reconciliation {pipeline.quality['independent_reconciliation']}), "
        f"{seeded} strategy inputs, strategy run {strategy_run_id} ({decision_count} decisions)"
    )
    if registration.accepted:
        context.log.info(f"{tick}; pointer sequence {registration.sequence}")
    else:
        # #536 acceptance: which objective failed is readable from the run log and the
        # op's output metadata, without opening the database.
        context.log.warning(
            f"{tick}; POINTER WITHHELD at sequence {registration.sequence} "
            f"— unmet service objectives: {registration.summary}"
        )
    context.add_output_metadata(
        {
            "capture_run_id": pipeline.run_id,
            "quality_report_id": pipeline.quality_report_id,
            "independent_reconciliation": pipeline.quality["independent_reconciliation"],
            "strategy_inputs_seeded": seeded,
            "l2_input_coverage": f"{l2_complete}/{l2_total}",
            "strategy_run_id": strategy_run_id,
            "decision_count": decision_count,
            "snapshot_id": snapshot_id,
            "pointer_advanced": registration.accepted,
            "pointer_sequence": registration.sequence,
            "unmet_service_objectives": registration.summary,
        }
    )
    return pipeline.run_id


@dg.job(name=TOPT_LIVE_JOB_NAME)
def topt_live_pipeline_job() -> None:
    run_topt_live_tick()


@dg.schedule(
    job=topt_live_pipeline_job,
    cron_schedule=TOPT_LIVE_CRON,
    execution_timezone="UTC",
    # ENABLED by default: #27's appended acceptance (issue comment, 2026-07-20)
    # requires the schedule running in Staging; enabling it is the deliberate,
    # owner-authorized operator action recorded there — not an accidental default.
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def topt_live_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    executed_at = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        # run_key == the tick time: the daemon dedupes a re-evaluated tick to a
        # single run, so an identical tick retry is idempotent.
        run_key=executed_at,
        run_config=dg.RunConfig(ops={"run_topt_live_tick": ToptLiveTickConfig(executed_at=executed_at)}),
    )


# -- QQQ universe pipeline (#539 owner directive 2026-08-17) ---------------------------

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


@dg.op
def run_qqq_live_tick(context: dg.OpExecutionContext, config: ToptLiveTickConfig) -> str:
    """The QQQ data pipeline: capture → freeze → materialize → report → governed pointer.

    Deliberately NO strategy replay: the strategy definition is TOPT-scoped versioned
    configuration; QQQ phase 1 serves data (prices, facts, GPPE where headcount exists,
    valuation context), not selections.
    """
    cutoff = datetime.fromisoformat(config.executed_at)
    version = live_version_for(cutoff)
    with psycopg.connect(settings.database_url) as connection:
        pipeline = run_topt_pipeline(
            connection,
            cutoff=cutoff,
            version=version,
            universe_head_kind=QQQ_UNIVERSE_HEAD,
            label_prefix="production-qqq",
        )
        registration = register_run_evidence(
            connection,
            run_id=pipeline.run_id,
            release_manifest_id=pipeline.release_manifest_id,
            quality_report=pipeline.quality,
            objectives=QQQ_PHASE1_OBJECTIVES,
        )
        connection.commit()
    context.log.info(
        f"qqq live tick {config.executed_at}: capture {pipeline.run_id} "
        f"(available {pipeline.quality['available_count']}/{pipeline.quality['requested_count']}), "
        f"pointer_advanced={registration.accepted}"
    )
    context.add_output_metadata(
        {
            "capture_run_id": pipeline.run_id,
            "quality_report_id": pipeline.quality_report_id,
            "pointer_advanced": registration.accepted,
            "unmet_service_objectives": registration.summary,
        }
    )
    return pipeline.run_id


@dg.job(name=QQQ_LIVE_JOB_NAME)
def qqq_live_pipeline_job() -> None:
    run_qqq_live_tick()


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


@dg.op
def run_canary_live_tick(context: dg.OpExecutionContext, config: ToptLiveTickConfig) -> str:
    """#648: the post-deploy canary — the REAL pipeline over five hand-picked
    issuers (six listings, every operating branch), so 'deployed' and
    'verified' become one fact. Trigger-only: no schedule in either environment;
    the deploy lane inserts a trigger row and then asserts the named oracles."""
    cutoff = datetime.fromisoformat(config.executed_at)
    version = live_version_for(cutoff)
    with psycopg.connect(settings.database_url) as connection:
        pipeline = run_topt_pipeline(
            connection,
            cutoff=cutoff,
            version=version,
            universe_head_kind=CANARY_UNIVERSE_HEAD,
            label_prefix="production-canary",
        )
        registration = register_run_evidence(
            connection,
            run_id=pipeline.run_id,
            release_manifest_id=pipeline.release_manifest_id,
            quality_report=pipeline.quality,
            objectives=CANARY_OBJECTIVES,
        )
        connection.commit()
    context.log.info(
        f"canary tick {config.executed_at}: capture {pipeline.run_id} "
        f"(available {pipeline.quality['available_count']}/{pipeline.quality['requested_count']}), "
        f"pointer_advanced={registration.accepted}"
    )
    context.add_output_metadata(
        {
            "capture_run_id": pipeline.run_id,
            "quality_report_id": pipeline.quality_report_id,
            "pointer_advanced": registration.accepted,
            "unmet_service_objectives": registration.summary,
        }
    )
    return pipeline.run_id


@dg.job(name=CANARY_JOB_NAME)
def canary_live_pipeline_job() -> None:
    run_canary_live_tick()


# 23:47 UTC: 27 minutes after the QQQ tick, so the canary's overlap names (AAPL,
# GOOGL, GOOG, ASML) reuse the night's committed observations under #635 and the
# daily proof costs ~2 vendor calls (HBAN/CINF only). Seven deploy-free days in a
# row meant seven days with ZERO full freeze→publish executions — a
# deploy-triggered verifier only verifies when you deploy; this makes the full
# chain prove itself nightly in BOTH environments (#648/#671).
CANARY_DAILY_CRON = "47 23 * * *"


@dg.schedule(
    job=canary_live_pipeline_job,
    cron_schedule=CANARY_DAILY_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def canary_daily_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    executed_at = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=executed_at,
        run_config=dg.RunConfig(ops={"run_canary_live_tick": ToptLiveTickConfig(executed_at=executed_at)}),
    )


@dg.schedule(
    job=qqq_live_pipeline_job,
    cron_schedule=QQQ_LIVE_CRON,
    execution_timezone="UTC",
    # Production-only by owner directive ("staging 跑 topt 够了"): staging keeps the
    # schedule STOPPED and QQQ runs there only via the manual trigger.
    default_status=(
        dg.DefaultScheduleStatus.RUNNING
        if settings.app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    ),
)
def qqq_live_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    executed_at = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=executed_at,
        run_config=dg.RunConfig(ops={"run_qqq_live_tick": ToptLiveTickConfig(executed_at=executed_at)}),
    )


defs = dg.Definitions(
    jobs=[topt_live_pipeline_job, qqq_live_pipeline_job, canary_live_pipeline_job],
    schedules=[topt_live_schedule, qqq_live_schedule, canary_daily_schedule],
)
