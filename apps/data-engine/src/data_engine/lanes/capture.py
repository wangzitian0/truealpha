"""The datahub capture lane: real-source ticks and their schedules (#731 split of
`data_engine.dagster_defs`; the behaviour is unchanged, only the file moved).

Each tick drives one universe through the generic capture executor and its
per-semantic adapters, freezes + materializes, persists the quality report, advances
that universe's governed pointer, and records any unmet service objectives for
`/admin/datahub` observability (#536). TOPT additionally seeds the strategy inputs and runs the
frozen strategy (#27/#171). No fixture data is seeded anywhere in this lane (#429 I2).

Hermeticity: no database or network work at import; every op opens its connection
lazily from DATABASE_URL. `executed_at` comes from the schedule's tick time, never the
wall clock: distinct ticks -> distinct content-addressed runs; a retried tick
reproduces the same identities (idempotent retry).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import dagster as dg
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.strategy import live_risk_free_rate

from data_engine.config import settings
from data_engine.datahub.a1_evidence import ACCEPTED_SERVICE_OBJECTIVES, ServiceObjectives, register_run_evidence
from data_engine.datahub.production_topt.composition import live_version_for, run_topt_pipeline
from data_engine.datahub.production_topt.corroboration_audit import CorroborationTally, corroboration_tally
from data_engine.datahub.production_topt.fund_consolidation import (
    materialize_fund_consolidation,
)
from data_engine.datahub.production_topt.fund_consolidation import (
    summary_line as fund_consolidation_summary,
)
from data_engine.datahub.production_topt.plausibility_gate import judge_issuers, judge_strategy
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
    never read from the wall clock inside the run.

    `force_fetch` is set by an operator (#874) or by staging's boot canary
    (`triggers.boot_canary_forces_fetch`, owner decision 2026-09-17). A schedule
    never sets it. The run fetches every obligation instead of reusing observations
    committed in the last twelve hours (#635), under a capture identity of its own.
    Launch it through the admin page or Dagster GraphQL
    (docs/datahub-quality-report.md). The run plan, the quality report and this op's
    output metadata record `forced_fetch`.
    """

    executed_at: str
    force_fetch: bool = False


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
    #: Whether this tick materializes the module-5 fund consolidation (#727/#36). Only
    #: the universe whose members ARE the fund's holdings can value them: QQQ's filed
    #: lines consolidated against TOPT's 20 core rows would resolve a fraction of the
    #: fund and refuse on coverage — a true answer to a question nobody asked. One flag
    #: while one fund has a universe; a fund->universe registry is the multi-fund
    #: tranche's problem (stated the same way in the App's fund-valuation reader).
    consolidate_funds: bool
    cron: str | None


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

SP500_LIVE_JOB_NAME = "sp500_live_pipeline"
# 01:15 UTC: after the nightly invariants (00:15 UTC) and confidence report (00:45 UTC),
# well clear of the 22:15/22:45 TOPT, 23:20 QQQ, and 23:47 canary capture windows (#67 / #1281).
SP500_LIVE_CRON = "15 1 * * *"
# The contract holds the status of the SP500 universe: `environment_contract.v1.json` lists it as
# stopped, with the precondition that unlocks it. This lane declares no status of its own.
SP500_UNIVERSE_HEAD = "universe-list:sp500"
SP500_PHASE1_OBJECTIVES = ServiceObjectives(
    minimum_coverage=ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage,
    minimum_availability=Decimal("0.70"),
    minimum_confidence_score=ACCEPTED_SERVICE_OBJECTIVES.minimum_confidence_score,
    minimum_independent_origin_groups=ACCEPTED_SERVICE_OBJECTIVES.minimum_independent_origin_groups,
    minimum_corroborated_share=Decimal("0.90"),
)


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
        consolidate_funds=False,
        cron=TOPT_LIVE_CRON,
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
        # QQQ's core rows are the fund's own holdings: the only tick that can value them.
        consolidate_funds=True,
        cron=QQQ_LIVE_CRON,
    ),
    UniverseTick(
        key="sp500",
        job_name=SP500_LIVE_JOB_NAME,
        op_name="run_sp500_live_tick",
        schedule_name="sp500_live_schedule",
        log_label="sp500 live tick",
        universe_head_kind=SP500_UNIVERSE_HEAD,
        label_prefix="production-sp500",
        objectives=SP500_PHASE1_OBJECTIVES,
        run_strategy=False,
        consolidate_funds=False,
        cron=SP500_LIVE_CRON,
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
        consolidate_funds=False,
        cron=CANARY_DAILY_CRON,
    ),
)

#: An environment that the contract does not name (local runs, CI) takes the rule of staging: the TOPT tick
#: and the canary run, nothing else.
UNRECOGNISED_ENVIRONMENT_RULE = "staging"


def tick_schedule_status(tick: UniverseTick, app_env: str) -> dg.DefaultScheduleStatus:
    """The default status of `tick`'s schedule in `app_env` (#1310, #1416).

    `environment_contract.v1.json` is the only source of "scheduled" and "stopped". An environment
    that the contract does not name follows `UNRECOGNISED_ENVIRONMENT_RULE`.
    """
    contract = load_environment_contract()
    env = contract.normalize_environment(app_env)
    if env not in contract.environments:
        env = UNRECOGNISED_ENVIRONMENT_RULE
    return (
        dg.DefaultScheduleStatus.RUNNING
        if contract.is_universe_scheduled(tick.key, env)
        else dg.DefaultScheduleStatus.STOPPED
    )


def is_universe_scheduled(universe: str, environment: str) -> bool:
    """Whether `universe` has an active schedule in `environment` (#1118, #1416)."""
    key = universe.removeprefix("universe-list:")
    for tick in TICKS:
        if tick.key == key:
            if tick.cron is None:
                return False
            return tick_schedule_status(tick, environment) == dg.DefaultScheduleStatus.RUNNING
    return load_environment_contract().is_universe_scheduled(universe, environment)


def is_head_scheduled(universe_id: str, environment: str) -> bool:
    """Whether `universe_id` has a running schedule in `environment` (#1256, #1416).

    Delegates to `environment_contract.v1.json` and fails closed (returns True) when
    `environment` is omitted or unrecognised, or when `universe_id` maps to no tick key.
    """
    return load_environment_contract().is_head_scheduled(universe_id, environment)


@contextmanager
def _summary_on_failure(context: dg.OpExecutionContext, label: str, tally: CorroborationTally) -> Iterator[None]:
    """A tick that fails — a capture refused for its shortfall (#538) included — still
    logs what the origins lost and the gate refused before it: an exhausted budget is
    the likeliest cause of a primary shortfall, and the run log is where one looks."""
    try:
        yield
    except Exception:
        context.log.warning(f"{label} failed; {tally.summary()}; {tally.refusals()}")
        raise


def _run_tick(context: dg.OpExecutionContext, config: ToptLiveTickConfig, tick: UniverseTick) -> str:
    """The one tick body every universe runs: capture → freeze → materialize → report →
    governed pointer, plus the strategy replay where the declaration asks for it."""
    cutoff = datetime.fromisoformat(config.executed_at)
    version = live_version_for(cutoff)
    # Lazy, run-time connection (DATABASE_URL). One transaction for the whole tick:
    # a mid-run failure leaves no partial run; the daemon's retry re-runs the tick
    # against the same content-addressed identities. Every vendor call inside is
    # attributed to this Dagster run in the external call ledger (#729) and admitted by
    # the rule-6 gate first: paced to its seat's declared window, refused once this
    # environment's share of the seat's daily budget is spent. Every corroboration the
    # capture loses — refused by its origin, by the gate, or not persisted — and every
    # call the gate refused is counted for the summary below (#885), which a failed tick
    # still logs.
    with (
        gateway.run_scope(f"dagster:{context.run_id}"),
        corroboration_tally() as corroborations,
        _summary_on_failure(context, f"{tick.log_label} {config.executed_at}", corroborations),
        gateway.capacity_scope(),
        psycopg.connect(settings.database_url) as connection,
    ):
        pipeline = run_topt_pipeline(
            connection,
            cutoff=cutoff,
            version=version,
            universe_head_kind=tick.universe_head_kind,
            label_prefix=tick.label_prefix,
            force_fetch=config.force_fetch,
        )
        # #1443/#1323: plausibility policy v4, in the same transaction, before anything consumes the
        # run. An issuer refusal excludes that issuer's decision with a named reason, and the run
        # publishes the others.
        issuers = judge_issuers(connection, run_id=pipeline.run_id)
        strategy: dict[str, Any] = {}
        if tick.run_strategy:
            seeded = seed_strategy_inputs_from_capture(connection, pipeline.run_id, cutoff=cutoff)
            # #496: the L2 funnel metric, same transaction as the seed it measures.
            l2_complete, l2_total = persist_strategy_input_coverage(connection, pipeline.run_id, cutoff=cutoff)
            # #877: the strategy run is bound to THIS capture. A forced tick (#874) shares
            # its cutoff with the scheduled one, so the cutoff cannot say which capture a
            # strategy run belongs to; readers resolve through the binding.
            strategy_run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
                connection,
                cutoff=cutoff,
                executed_at=cutoff,
                risk_free_rate=live_risk_free_rate(cutoff),
                capture_run_id=pipeline.run_id,
                plausibility_refusals=issuers.excluded_issuers(),
            )
            strategy = {
                "strategy_inputs_seeded": seeded,
                "l2_input_coverage": f"{l2_complete}/{l2_total}",
                "l2_complete": l2_complete,
                "strategy_run_id": strategy_run_id,
                "decision_count": decision_count,
                "snapshot_id": snapshot_id,
            }
        # #727/#36: the module-5 fund consolidation, in the same transaction as the core
        # rows it weights. Before the pointer gate on purpose — a run whose plausibility
        # is refused rolls this back with everything else, so mart.fund_virtual_company
        # can never describe a run that was never served.
        fund_consolidation: dict[str, Any] = {}
        if tick.consolidate_funds:
            consolidations = materialize_fund_consolidation(connection, run_id=pipeline.run_id, cutoff=cutoff)
            context.log.info(fund_consolidation_summary(consolidations))
            fund_consolidation = {
                "funds_consolidated": len(consolidations),
                "funds_refused": sum(1 for item in consolidations if item.result.value is None),
            }
        # #544: the run rule (an empty eligible set), before the pointer can advance. A refused
        # run raises here; the `with` unwinds without a commit, so mart.current_pointer stays on
        # the previous accepted run. Deferred violations (the nightly suite's exemption file,
        # issue + expiry) are logged and let through.
        verdict = judge_strategy(
            connection,
            issuers,
            strategy_run_id=strategy.get("strategy_run_id"),
            l2_complete=strategy.get("l2_complete"),
        )
        for line in verdict.lines():
            context.log.info(line)
        if verdict.refused:
            raise dg.Failure(
                f"plausibility policy {verdict.policy_version} refused {pipeline.run_id}: "
                + "; ".join(f"{v.rule} — {v.detail}" for v in verdict.run_refusals)
                + " (#544; the tick rolls back and the pointer stays on the previous accepted run)"
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
            **({"objectives": tick.objectives} if tick.objectives is not None else {}),
        )
        connection.commit()

    summary = (
        f"{tick.log_label} {config.executed_at}{' (forced fetch)' if pipeline.forced_fetch else ''}: "
        f"capture {pipeline.run_id} "
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
    # #885: a second origin that raised, refused its quantity or could not be persisted
    # leaves its cell single-origin without failing the tick; the count (per origin and
    # stage) is what tells a revoked key or a dead OpenD from "the vendor had nothing".
    # #729: so does every call the rule-6 gate refused, per seat — an exhausted budget is
    # `capacity refused N (twelvedata budget N)`, never a quiet run of empty cells.
    summary += f"; {corroborations.summary()}; {corroborations.refusals()}"
    # #862: cells the primary price source could not serve and a further registered origin
    # did. Zero is said, not omitted: a nonzero count is the primary's outage, visible here.
    served_by_failover = int(pipeline.quality.get("served_by_failover_count", 0))
    summary += f"; served by failover {served_by_failover}"
    if registration.advanced:
        if not registration.unmet:
            context.log.info(f"{summary}; pointer sequence {registration.sequence}")
        else:
            context.log.warning(
                f"{summary}; pointer sequence {registration.sequence} (DEGRADED: {registration.summary})"
            )
    else:
        # Retried tick or idempotent run where pointer was already at this run.
        context.log.info(
            f"{summary}; pointer already at sequence {registration.sequence}"
            + (f" (DEGRADED: {registration.summary})" if registration.unmet else "")
        )
    metadata: dict[str, Any] = {
        "capture_run_id": pipeline.run_id,
        "plausibility_excluded": sorted({f"{v.rule} {v.listing_id}" for v in verdict.refusals}),
        "quality_report_id": pipeline.quality_report_id,
        "forced_fetch": pipeline.forced_fetch,
        "independent_reconciliation": pipeline.quality["independent_reconciliation"],
        **strategy,
        **fund_consolidation,
        "corroborations_refused": corroborations.total,
        "capacity_refused": corroborations.capacity_refused,
        "budget_exhausted": corroborations.budget_exhausted,
        "served_by_failover": served_by_failover,
        "pointer_advanced": registration.advanced,
        "pointer_sequence": registration.sequence,
        "unmet_service_objectives": registration.summary,
        "is_degraded": registration.is_degraded,
    }
    context.add_output_metadata(metadata)
    return pipeline.run_id


def build_schedule(
    tick: UniverseTick,
    job: dg.JobDefinition,
    *,
    cron: str | None = None,
    default_status: dg.DefaultScheduleStatus | None = None,
) -> dg.ScheduleDefinition | None:
    """Build the schedule for a tick against `job`."""
    effective_cron = tick.cron if cron is None else cron
    if effective_cron is None:
        return None
    effective_status = tick_schedule_status(tick, settings.app_env) if default_status is None else default_status

    @dg.schedule(
        name=tick.schedule_name,
        job=job,
        cron_schedule=effective_cron,
        execution_timezone="UTC",
        default_status=effective_status,
    )
    def tick_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        executed_at = context.scheduled_execution_time.isoformat()
        return dg.RunRequest(
            # run_key == the tick time: the daemon dedupes a re-evaluated tick to a
            # single run, so an identical tick retry is idempotent. A schedule never
            # forces a fetch (#874): #635's reuse is what the nightly budget relies on.
            run_key=executed_at,
            run_config=dg.RunConfig(ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at, force_fetch=False)}),
        )

    return tick_schedule


def build_tick(tick: UniverseTick) -> tuple[dg.OpDefinition, dg.JobDefinition, dg.ScheduleDefinition | None]:
    """Op, job and schedule for one declaration. Op and job names are the declaration's:
    operators launch by them and the trigger sensor dispatches by them."""

    @dg.op(name=tick.op_name)
    def tick_op(context: dg.OpExecutionContext, config: ToptLiveTickConfig) -> str:
        return _run_tick(context, config, tick)

    @dg.job(name=tick.job_name)
    def tick_job() -> None:
        tick_op()

    return tick_op, tick_job, build_schedule(tick, tick_job)


_BUILT = {tick.key: build_tick(tick) for tick in TICKS}
TICK_BY_JOB: dict[str, UniverseTick] = {tick.job_name: tick for tick in TICKS}

# The names operators, tests and the trigger sensor address, unchanged from the
# hand-written era.
run_topt_live_tick, topt_live_pipeline_job, topt_live_schedule = _BUILT["topt"]
run_qqq_live_tick, qqq_live_pipeline_job, qqq_live_schedule = _BUILT["qqq"]
run_sp500_live_tick, sp500_live_pipeline_job, sp500_live_schedule = _BUILT["sp500"]
run_canary_live_tick, canary_live_pipeline_job, canary_daily_schedule = _BUILT["canary"]


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    """Build all capture lane jobs and schedules for `app_env` (#67 / #1281)."""
    schedules = (
        [schedule for _, _, schedule in _BUILT.values() if schedule is not None]
        if app_env == settings.app_env
        else [
            build_schedule(
                tick,
                _BUILT[tick.key][1],
                cron=live_topt_cron(app_env) if tick.key == "topt" else tick.cron,
                default_status=tick_schedule_status(tick, app_env),
            )
            for tick in TICKS
        ]
    )
    return dg.Definitions(
        jobs=[job for _, job, _ in _BUILT.values()],
        schedules=[schedule for schedule in schedules if schedule is not None],
    )


defs = build_definitions()
