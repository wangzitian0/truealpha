"""Deployable Dagster entrypoint for isolated Staging / Production (#27).

The infra2 deploy surface (`truealpha/truealpha/20.data_engine/compose.yaml`)
loads THIS module in both roles:

    dagster-webserver -m data_engine.dagster_defs     # loopback-only UI
    dagster-daemon    run -m data_engine.dagster_defs  # sole recurring-run authority

Since #731 the module owns no job of its own: `defs` is the merge of every lane
registered in `data_engine.lanes.LANE_MODULES` (capture ticks + schedules, the weekly
universe refresh, the manual-trigger sensor, the entity identity backfill). A lane adds
or retires a job in its own module; nothing here lists a job by name, and `test_dagster_defs.py` asserts the root
is exactly the union of what the lanes declare. The names re-exported below keep the
operator commands and tests that address `data_engine.dagster_defs` working, and they
are also what makes each lane statically reachable for `tools/reachability_ratchet.py`
(`LANE_MODULES` is resolved by `import_module`, which an AST walk cannot follow).

Every job runs in the in-process executor (#1467). The code server container holds the gRPC server
and every run process in one memory cgroup. The default executor starts one more process per step,
and each process that imports this module holds at least a fresh import. One process per run is the premise of
`code_server_capacity` in `environment_contract.v1.json`, which holds the run queue limit of
`dagster.yaml` and the budget of one run. The jobs are linear chains, except `output_invariants_check`: its two independent
steps wait for the same condition and work for seconds, so one after the other costs seconds.

`dagster.yaml` loads the run launcher by module name (#1442). The import of
`MonitoredRunLauncher` below makes that module statically reachable too, and
`test_dagster_instance_config.py` holds the name in `dagster.yaml` equal to this class.

No fixture data is seeded anywhere in the deployed composition (#429 invariant I2).
"""

import dagster as dg
from truealpha_runtime.telemetry import init_telemetry

from data_engine.config import settings
from data_engine.lanes import LANE_MODULES, lane_definitions
from data_engine.lanes.capture import (
    CANARY_JOB_NAME,
    QQQ_LIVE_JOB_NAME,
    SP500_LIVE_JOB_NAME,
    TICKS,
    TOPT_LIVE_JOB_NAME,
    ToptLiveTickConfig,
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
    "ToptLiveTickConfig",
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

# OTLP telemetry hook for infra2's shared SigNoz (#1034). It is off in deployed data-engine.
# Infra2 issues no OTEL_* variable to the dagster-* roles, and the three roles run on the host
# network, which cannot reach the collector (infra2 ops.observability section 4.2). So
# init_telemetry() returns None there. If an endpoint is set, a missing identity refuses the
# import, and the code server reports a failed location load.
init_telemetry()


def with_in_process_executor(definitions: dg.Definitions) -> dg.Definitions:
    """Return `definitions` with the in-process executor as the default executor of every job (#1467)."""
    return dg.Definitions.merge(definitions, dg.Definitions(executor=dg.in_process_executor))


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    """Build the deployable Dagster Definitions across all registered lanes (#731, #67)."""
    return with_in_process_executor(dg.Definitions.merge(*lane_definitions(app_env).values()))


defs = with_in_process_executor(dg.Definitions.merge(*lane_definitions().values()))
