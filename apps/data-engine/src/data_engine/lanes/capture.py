"""Datahub capture lane: real-source ticks and their schedules (#731)."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import dagster as dg
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.strategy import ExclusionReason
from truealpha_contracts.universes import job_name, keys, lane_name, op_name, record, schedule_name

from data_engine.config import settings
from data_engine.datahub.a1_evidence import ACCEPTED_SERVICE_OBJECTIVES, ServiceObjectives, register_run_evidence
from data_engine.datahub.oracle_outcomes import record_oracle_outcomes_safely
from data_engine.datahub.production_topt.composition import (
    ensure_served_head_artifacts,
    live_version_for,
    run_topt_pipeline,
)
from data_engine.datahub.production_topt.corroboration_audit import CorroborationTally, corroboration_tally
from data_engine.datahub.production_topt.fund_consolidation import (
    materialize_fund_consolidation,
)
from data_engine.datahub.production_topt.fund_consolidation import (
    summary_line as fund_consolidation_summary,
)
from data_engine.datahub.production_topt.issuer_registry import registry_unavailable_issuers
from data_engine.datahub.production_topt.plausibility_gate import judge_issuers, judge_strategy
from data_engine.datahub.strategy_bridge import (
    persist_strategy_input_coverage,
    run_strategy_replay_for_cutoff,
    seed_strategy_inputs_from_capture,
    undeclared_primary_listing_issuers,
)
from data_engine.environment_universes import rule_environment
from data_engine.external_op import external_op
from data_engine.sources import gateway


class ToptLiveTickConfig(dg.Config):
    executed_at: str
    force_fetch: bool = False


@dataclass(frozen=True)
class UniverseTick:
    key: str
    job_name: str
    op_name: str
    schedule_name: str
    log_label: str
    label_prefix: str
    objectives: ServiceObjectives | None
    run_strategy: bool
    consolidate_funds: bool
    cron: str | None

    @property
    def lane_name(self) -> str:
        return lane_name(self.key)

    @property
    def universe_head_kind(self) -> str | None:
        return None if record(self.key).membership.kind == "frozen_corpus" else self.lane_name


DATA_UNIVERSE_OBJECTIVES = ServiceObjectives(
    minimum_coverage=ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage,
    minimum_availability=Decimal("0.70"),
    minimum_independent_origin_groups=ACCEPTED_SERVICE_OBJECTIVES.minimum_independent_origin_groups,
    minimum_corroborated_share=Decimal("0.90"),
)

SENTINEL_OBJECTIVES = ServiceObjectives(
    minimum_coverage=ACCEPTED_SERVICE_OBJECTIVES.minimum_coverage,
    minimum_availability=Decimal("0.70"),
    minimum_independent_origin_groups=ACCEPTED_SERVICE_OBJECTIVES.minimum_independent_origin_groups,
    minimum_corroborated_share=Decimal("0"),
)


def tick_cron(key: str, app_env: str) -> str:
    universe = record(key)
    return universe.cron_for(rule_environment(app_env)) or next(iter(universe.cron.values()))


def universe_tick(key: str, app_env: str = settings.app_env) -> UniverseTick:
    universe = record(key)
    objectives = (
        SENTINEL_OBJECTIVES if universe.is_sentinel else (None if universe.runs_strategy else DATA_UNIVERSE_OBJECTIVES)
    )
    return UniverseTick(
        key=key,
        job_name=job_name(key),
        op_name=op_name(key),
        schedule_name=schedule_name(key),
        log_label=f"{key} tick" if universe.is_sentinel else f"{key} live tick",
        label_prefix=f"production-{key}",
        objectives=objectives,
        run_strategy=universe.runs_strategy,
        consolidate_funds=universe.consolidates_funds,
        cron=tick_cron(key, app_env),
    )


TICKS: tuple[UniverseTick, ...] = tuple(universe_tick(key) for key in keys())

TOPT_LIVE_JOB_NAME = universe_tick("topt").job_name
QQQ_LIVE_JOB_NAME = universe_tick("qqq").job_name
SP500_LIVE_JOB_NAME = universe_tick("sp500").job_name
CANARY_JOB_NAME = universe_tick("canary").job_name


def is_universe_scheduled(universe: str, environment: str) -> bool:
    return load_environment_contract().is_universe_scheduled(universe, rule_environment(environment))


def is_universe_list_refresh_scheduled(universe: str, environment: str) -> bool:
    return load_environment_contract().is_universe_list_refresh_scheduled(universe, rule_environment(environment))


def tick_schedule_status(tick: UniverseTick, app_env: str) -> dg.DefaultScheduleStatus:
    return (
        dg.DefaultScheduleStatus.RUNNING
        if is_universe_scheduled(tick.key, app_env)
        else dg.DefaultScheduleStatus.STOPPED
    )


@contextmanager
def _summary_on_failure(context: dg.OpExecutionContext, label: str, tally: CorroborationTally) -> Iterator[None]:
    try:
        yield
    except Exception:
        context.log.warning(f"{label} failed; {tally.summary()}; {tally.refusals()}")
        raise


def _registry_unavailable(
    context: dg.OpExecutionContext, connection: Any, tick: UniverseTick, run_id: str, verdict: Any
) -> list[str]:
    flagged = registry_unavailable_issuers(connection, run_id=run_id)
    for issuer_id in flagged:
        context.log.warning(f"{tick.log_label}: issuer {issuer_id} excluded as registry_unavailable")
    judged = set(verdict.issuers.values())
    if not tick.run_strategy and judged and judged <= set(flagged):
        raise dg.Failure(
            f"{tick.log_label}: the issuer registry classified none of {len(judged)} issuers; "
            "the run would publish no operating numerator (#1323)"
        )
    return flagged


def _run_tick(context: dg.OpExecutionContext, config: ToptLiveTickConfig, tick: UniverseTick) -> str:
    cutoff = datetime.fromisoformat(config.executed_at)
    version = live_version_for(cutoff)
    with (
        gateway.probe_scope(config.force_fetch),
        corroboration_tally() as corroborations,
        _summary_on_failure(context, f"{tick.log_label} {config.executed_at}", corroborations),
        gateway.capacity_scope(),
        psycopg.connect(settings.database_url) as connection,
    ):
        ensure_served_head_artifacts(connection)
        pipeline = run_topt_pipeline(
            connection,
            cutoff=cutoff,
            version=version,
            universe_head_kind=tick.universe_head_kind,
            label_prefix=tick.label_prefix,
            force_fetch=config.force_fetch,
        )
        issuers = judge_issuers(connection, run_id=pipeline.run_id)
        registry_unavailable = _registry_unavailable(context, connection, tick, pipeline.run_id, issuers)
        undeclared = undeclared_primary_listing_issuers(connection, pipeline.run_id, cutoff=cutoff)
        excluded = {
            **issuers.excluded_issuers(),
            **dict.fromkeys(undeclared, ExclusionReason.PRIMARY_LISTING_UNDECLARED),
            **dict.fromkeys(registry_unavailable, ExclusionReason.REGISTRY_UNAVAILABLE),
        }
        strategy: dict[str, Any] = {}
        if tick.run_strategy:
            seeded = seed_strategy_inputs_from_capture(connection, pipeline.run_id, cutoff=cutoff)
            l2_complete, l2_total = persist_strategy_input_coverage(connection, pipeline.run_id, cutoff=cutoff)
            strategy_run_id, decision_count, snapshot_id = run_strategy_replay_for_cutoff(
                connection,
                cutoff=cutoff,
                executed_at=cutoff,
                risk_free_rate=pipeline.risk_free_rate,
                capture_run_id=pipeline.run_id,
                plausibility_refusals=excluded,
            )
            strategy = {
                "strategy_inputs_seeded": seeded,
                "l2_input_coverage": f"{l2_complete}/{l2_total}",
                "l2_complete": l2_complete,
                "strategy_run_id": strategy_run_id,
                "decision_count": decision_count,
                "snapshot_id": snapshot_id,
            }
        fund_consolidation: dict[str, Any] = {}
        if tick.consolidate_funds:
            consolidations = materialize_fund_consolidation(connection, run_id=pipeline.run_id, cutoff=cutoff)
            context.log.info(fund_consolidation_summary(consolidations))
            fund_consolidation = {
                "funds_consolidated": len(consolidations),
                "funds_refused": sum(1 for item in consolidations if item.result.value is None),
            }
        verdict = judge_strategy(
            connection,
            issuers,
            strategy_run_id=strategy.get("strategy_run_id"),
            l2_complete=strategy.get("l2_complete"),
        )
        for line in verdict.lines():
            context.log.info(line)
        registration = register_run_evidence(
            connection,
            run_id=pipeline.run_id,
            release_manifest_id=pipeline.release_manifest_id,
            quality_report=pipeline.quality,
            **({"objectives": tick.objectives} if tick.objectives is not None else {}),
        )
        connection.commit()
        oracle_rows = record_oracle_outcomes_safely(connection, pipeline.run_id, compared_at=cutoff, log=context.log)

    served_by_failover = int(pipeline.quality.get("served_by_failover_count", 0))
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
    summary += f"; {corroborations.summary()}; {corroborations.refusals()}; served by failover {served_by_failover}"

    degraded = f" (DEGRADED: {registration.summary})" if registration.unmet else ""
    if registration.advanced and registration.unmet:
        context.log.warning(f"{summary}; pointer sequence {registration.sequence}{degraded}")
    elif registration.advanced:
        context.log.info(f"{summary}; pointer sequence {registration.sequence}")
    else:
        context.log.info(f"{summary}; pointer already at sequence {registration.sequence}{degraded}")

    context.add_output_metadata(
        {
            "capture_run_id": pipeline.run_id,
            "plausibility_excluded": sorted({f"{v.rule} {v.listing_id}" for v in verdict.refusals}),
            "registry_unavailable_excluded": registry_unavailable,
            "quality_report_id": pipeline.quality_report_id,
            "forced_fetch": pipeline.forced_fetch,
            "independent_reconciliation": pipeline.quality["independent_reconciliation"],
            "risk_free_series_id": pipeline.risk_free_rate.series_id,
            "risk_free_rate": str(pipeline.risk_free_rate.value),
            "risk_free_observation_date": pipeline.risk_free_rate.observation_date.isoformat(),
            "risk_free_source": pipeline.risk_free_rate.source,
            "risk_free_fetch_id": pipeline.risk_free_rate.fetch_id,
            **strategy,
            **fund_consolidation,
            "corroborations_refused": corroborations.total,
            "capacity_refused": corroborations.capacity_refused,
            "budget_exhausted": corroborations.budget_exhausted,
            "served_by_failover": served_by_failover,
            "oracle_outcomes_written": oracle_rows is not None,
            "oracle_outcomes_rows": oracle_rows or 0,
            "pointer_advanced": registration.advanced,
            "pointer_sequence": registration.sequence,
            "unmet_service_objectives": registration.summary,
            "is_degraded": registration.is_degraded,
        }
    )
    return pipeline.run_id


def build_schedule(
    tick: UniverseTick,
    job: dg.JobDefinition,
    *,
    cron: str | None = None,
    default_status: dg.DefaultScheduleStatus | None = None,
) -> dg.ScheduleDefinition | None:
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
            run_key=executed_at,
            run_config=dg.RunConfig(ops={tick.op_name: ToptLiveTickConfig(executed_at=executed_at, force_fetch=False)}),
        )

    return tick_schedule


def build_tick(tick: UniverseTick) -> tuple[dg.OpDefinition, dg.JobDefinition, dg.ScheduleDefinition | None]:
    @external_op(name=tick.op_name)
    def tick_op(context: dg.OpExecutionContext, config: ToptLiveTickConfig) -> str:
        return _run_tick(context, config, tick)

    @dg.job(name=tick.job_name)
    def tick_job() -> None:
        tick_op()

    return tick_op, tick_job, build_schedule(tick, tick_job)


_BUILT = {tick.key: build_tick(tick) for tick in TICKS}
TICK_BY_JOB: dict[str, UniverseTick] = {tick.job_name: tick for tick in TICKS}

run_topt_live_tick, topt_live_pipeline_job, topt_live_schedule = _BUILT["topt"]
run_qqq_live_tick, qqq_live_pipeline_job, qqq_live_schedule = _BUILT["qqq"]
run_sp500_live_tick, sp500_live_pipeline_job, sp500_live_schedule = _BUILT["sp500"]
run_canary_live_tick, canary_live_pipeline_job, canary_daily_schedule = _BUILT["canary"]


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    schedules = (
        [schedule for _, _, schedule in _BUILT.values() if schedule is not None]
        if app_env == settings.app_env
        else [
            build_schedule(
                tick,
                _BUILT[tick.key][1],
                cron=tick_cron(tick.key, app_env),
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
