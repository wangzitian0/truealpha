"""Deployable Dagster entrypoint for isolated Staging / Production (#27).

The infra2 deploy surface (`truealpha/truealpha/20.data_engine/compose.yaml`)
loads THIS module in both roles:

    dagster-webserver -m data_engine.dagster_defs     # loopback-only UI
    dagster-daemon    run -m data_engine.dagster_defs  # sole recurring-run authority

The ONE scheduled job here is the REAL-SOURCE pipeline (#27 appended acceptance,
#429 P1): capture all 84 TOPT obligations from live sources (Yahoo closes, SEC
company-facts, Twelve Data second price origin), freeze + materialize GPPE/three-tier
into `mart.topt_*`, persist the run's quality report, seed the captured cells into
`staging.strategy_backtest_inputs`, and run the frozen strategy over that captured
staging into `mart.strategy_*`. No fixture data is seeded anywhere in this job graph
(#429 invariant I2); the only corpus-derived objects are the frozen universe scope
and the frozen strategy definition — versioned configuration, not input data.

Hermeticity: no database or network work at import; the op opens its connection
lazily from DATABASE_URL. `cutoff`/`executed_at` come from the schedule's tick time,
never the wall clock: distinct ticks -> distinct content-addressed runs (the
two-cycle proof); a retried tick reproduces the same identities (idempotent retry).

The retired fixture-seeded canary lives in `fixture_canary_definitions()` — an
explicitly named, tests-only composition that is NOT part of the deployed `defs`.
"""

from datetime import datetime
from decimal import Decimal

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.a1_evidence import register_run_evidence
from data_engine.datahub.live_topt_pipeline import (
    live_version_for,
    run_live_topt_pipeline,
    run_strategy_replay_for_cutoff,
    seed_strategy_inputs_from_capture,
)

TOPT_LIVE_JOB_NAME = "topt_live_pipeline"
# Hourly: #27's evidence is two consecutive scheduled real-source cycles; an hourly
# cadence makes that observable within a working session while staying inside every
# source's limits (Twelve Data free tier: 21 fetches/tick, throttled 8s apart).
TOPT_LIVE_CRON = "15 * * * *"


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
        pipeline = run_live_topt_pipeline(connection, cutoff=cutoff, version=version)
        seeded = seed_strategy_inputs_from_capture(connection, pipeline.run_id, cutoff=cutoff)
        strategy_run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
            connection, cutoff=cutoff, executed_at=cutoff, risk_free_rate=Decimal("0.05")
        )
        # #378: register the run on the A1 evidence plane and advance the governed
        # pointer inside the same transaction, so consumers resolve THIS run through
        # mart.current_pointer_head the moment the tick commits.
        pointer_sequence = register_run_evidence(
            connection, run_id=pipeline.run_id, release_manifest_id=pipeline.release_manifest_id
        )
        connection.commit()

    context.log.info(
        f"topt live tick {config.executed_at}: capture {pipeline.run_id} "
        f"(available {pipeline.quality['available_count']}/{pipeline.quality['requested_count']}, "
        f"reconciliation {pipeline.quality['independent_reconciliation']}), "
        f"{seeded} strategy inputs, strategy run {strategy_run_id} ({decision_count} decisions)"
    )
    context.add_output_metadata(
        {
            "capture_run_id": pipeline.run_id,
            "quality_report_id": pipeline.quality_report_id,
            "independent_reconciliation": pipeline.quality["independent_reconciliation"],
            "strategy_inputs_seeded": seeded,
            "strategy_run_id": strategy_run_id,
            "decision_count": decision_count,
            "snapshot_id": snapshot_id,
            "pointer_sequence": pointer_sequence,
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


defs = dg.Definitions(
    jobs=[topt_live_pipeline_job],
    schedules=[topt_live_schedule],
)


# -- retired fixture canary (tests only; never deployed) -------------------------------

CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME = "core_strategy_fixture_canary"


def fixture_canary_definitions() -> dg.Definitions:
    """The retired golden-fixture canary, explicitly named as a fixture (#429 I2).

    Kept ONLY so tests can prove the fixture path still replays deterministically;
    it is deliberately excluded from the deployed `defs` above — the deployed job
    graph contains no fixture seeding.
    """
    import json

    from truealpha_contracts.strategy import LargeModelValueV0Definition

    from data_engine.core_strategy_replay import _load_corpus
    from data_engine.strategy_backtest_gateway import run_backtest_from_staging, seed_strategy_backtest_inputs
    from data_engine.strategy_replay_repository import write_replay

    class FixtureCanaryConfig(dg.Config):
        executed_at: str

    @dg.op
    def run_core_strategy_fixture_canary(context: dg.OpExecutionContext, config: FixtureCanaryConfig) -> str:
        executed_at = datetime.fromisoformat(config.executed_at)
        corpus = _load_corpus()
        definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
        with psycopg.connect(settings.database_url) as connection:
            seed_strategy_backtest_inputs(connection, corpus)
            decisions, snapshot_id = run_backtest_from_staging(connection, corpus, definition)
            run_id, decision_ids = write_replay(
                connection, decisions, definition, executed_at=executed_at, snapshot_id=snapshot_id
            )
            connection.commit()
        context.log.info(f"fixture canary: run {run_id}, {len(decision_ids)} decisions")
        return run_id

    @dg.job(name=CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME)
    def core_strategy_fixture_canary_job() -> None:
        run_core_strategy_fixture_canary()

    return dg.Definitions(jobs=[core_strategy_fixture_canary_job])
