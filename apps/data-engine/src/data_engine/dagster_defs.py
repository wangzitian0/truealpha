"""Deployable Dagster definitions across all lanes (#27, #731)."""

import logging

import dagster as dg
from infra2_sdk.runtime.otel import init_worker

from data_engine.config import settings
from data_engine.lanes import LANE_MODULES, lane_definitions
from data_engine.lanes.capture import (
    CANARY_JOB_NAME,
    QQQ_LIVE_JOB_NAME,
    SP500_LIVE_JOB_NAME,
    TICKS,
    TOPT_LIVE_JOB_NAME,
    LiveTickConfig,
    canary_daily_schedule,
    qqq_live_schedule,
    run_sp500_live_tick,
    run_topt_live_tick,
    sp500_live_pipeline_job,
    sp500_live_schedule,
    topt_live_schedule,
)
from data_engine.lanes.entity_identity import ENTITY_BACKFILL_JOB_NAME, entity_identity_backfill_sensor
from data_engine.lanes.standards import (
    FORM4_DAILY_CRON,
    FORM4_DAILY_SCHEDULE_NAME,
    NPORT_MONTHLY_CRON,
    NPORT_MONTHLY_SCHEDULE_NAME,
    STANDARD_BACKFILL_JOB_NAME,
    STANDARDS_PLANE_CRONS,
    TENK_ANNUAL_CRON,
    TENK_ANNUAL_SCHEDULE_NAME,
    THIRTEENF_QUARTERLY_CRON,
    THIRTEENF_QUARTERLY_SCHEDULE_NAME,
    form4_daily_schedule,
    nport_monthly_schedule,
    tenk_annual_schedule,
    thirteenf_quarterly_schedule,
)
from data_engine.lanes.triggers import pipeline_trigger_sensor
from data_engine.lanes.universe_refresh import (
    SP500_UNIVERSE_REFRESH_CRON,
    SP500_UNIVERSE_REFRESH_SCHEDULE_NAME,
    UNIVERSE_REFRESH_JOB_NAME,
    sp500_universe_refresh_schedule,
)
from data_engine.run_launcher import MonitoredRunLauncher

__all__ = [
    "CANARY_JOB_NAME",
    "ENTITY_BACKFILL_JOB_NAME",
    "FORM4_DAILY_CRON",
    "FORM4_DAILY_SCHEDULE_NAME",
    "LANE_MODULES",
    "MonitoredRunLauncher",
    "NPORT_MONTHLY_CRON",
    "NPORT_MONTHLY_SCHEDULE_NAME",
    "QQQ_LIVE_JOB_NAME",
    "SP500_LIVE_JOB_NAME",
    "SP500_UNIVERSE_REFRESH_CRON",
    "SP500_UNIVERSE_REFRESH_SCHEDULE_NAME",
    "STANDARD_BACKFILL_JOB_NAME",
    "STANDARDS_PLANE_CRONS",
    "TENK_ANNUAL_CRON",
    "TENK_ANNUAL_SCHEDULE_NAME",
    "THIRTEENF_QUARTERLY_CRON",
    "THIRTEENF_QUARTERLY_SCHEDULE_NAME",
    "TICKS",
    "TOPT_LIVE_JOB_NAME",
    "UNIVERSE_REFRESH_JOB_NAME",
    "LiveTickConfig",
    "build_definitions",
    "canary_daily_schedule",
    "defs",
    "entity_identity_backfill_sensor",
    "form4_daily_schedule",
    "nport_monthly_schedule",
    "pipeline_trigger_sensor",
    "qqq_live_schedule",
    "run_sp500_live_tick",
    "run_topt_live_tick",
    "sp500_live_pipeline_job",
    "sp500_live_schedule",
    "sp500_universe_refresh_schedule",
    "tenk_annual_schedule",
    "thirteenf_quarterly_schedule",
    "topt_live_schedule",
]

init_worker("truealpha", strict=True, require_issued_identity=True, log_level=logging.INFO)


def with_in_process_executor(definitions: dg.Definitions) -> dg.Definitions:
    return dg.Definitions.merge(definitions, dg.Definitions(executor=dg.in_process_executor))


def build_definitions(environment: str = settings.environment) -> dg.Definitions:
    return with_in_process_executor(dg.Definitions.merge(*lane_definitions(environment).values()))


defs = with_in_process_executor(dg.Definitions.merge(*lane_definitions().values()))
