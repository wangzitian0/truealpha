"""Per-lane Dagster definitions, merged into the deployable root (#731).

Every module named in `LANE_MODULES` exposes a module-level `defs: dagster.Definitions`
owning the jobs, schedules and sensors of one lane. `data_engine.dagster_defs` merges
them and nothing else lists a job by name: a lane adds or retires a job by editing its
own module only, and `test_dagster_defs.py` asserts the root is exactly the union of
what the lanes declare (no frozen job set anywhere).

Registering a lane is one line here. A module under this package that is not listed
is a defect the same test turns red on, so a lane cannot exist half-wired.

A lane that records nightly verdicts (#876) also declares their names in a module-level
`NIGHTLY_VERDICTS`; `nightly_verdict_names()` is the union, and
`common/runtime/tests/test_nightly_verdicts.py` holds it equal to the set
`tools/nightly_verdicts.py` watches.
"""

from __future__ import annotations

from importlib import import_module

import dagster as dg

LANE_MODULES: tuple[str, ...] = (
    # datahub: the real-source capture ticks (TOPT, QQQ, canary) and their schedules
    "data_engine.lanes.capture",
    # datahub: weekly constituents + N-PORT holdings + identity enrichment
    "data_engine.lanes.universe_refresh",
    # datahub: daily multi-resolution TOPT OHLCV refresh + PIT universe mask (#938)
    "data_engine.lanes.market_data",
    # datahub: both daily series of the QQQ listings outside TOPT, within the Twelve Data budget (#1277)
    "data_engine.lanes.qqq_prices",
    # the DB-mediated manual trigger (#495): a sensor over the capture lane's jobs
    "data_engine.lanes.triggers",
    # data quality: the output-invariant suite against this environment's own database
    "data_engine.lanes.quality",
    # the standard→wide-row loop's slow plane (#735 / #733): weekly backfill + probe
    "data_engine.lanes.standards",
    # datahub: the entity identity backfill (#877), sensor-launched, never at boot
    "data_engine.lanes.entity_identity",
    # the strategy's point-in-time input history, monthly and quarter-end cutoffs (#1139, #758)
    "data_engine.lanes.strategy_history",
    # the nightly backtest of large_model_value_v0 at its quarter-end cutoffs (#758, Milestone M3)
    "data_engine.lanes.backtest",
    # macro base data: the U.S. Treasury par yields, the measured risk-free rate included (#1255)
    "data_engine.lanes.macro_data",
)


def production_only_schedule_status(app_env: str) -> dg.DefaultScheduleStatus:
    """Return RUNNING when `app_env` is production (`production` / `prod`), else STOPPED."""
    return (
        dg.DefaultScheduleStatus.RUNNING
        if app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    )


def lane_definitions(app_env: str | None = None) -> dict[str, dg.Definitions]:
    """Import every registered lane and return its `defs`, keyed by module name."""
    definitions: dict[str, dg.Definitions] = {}
    for name in LANE_MODULES:
        module = import_module(name)
        if app_env is not None and hasattr(module, "build_definitions"):
            definitions[name] = module.build_definitions(app_env)
        else:
            definitions[name] = module.defs
    return definitions


def nightly_verdict_names() -> frozenset[str]:
    """Every verdict name a registered lane can record in `mart.nightly_verdicts`."""
    names: set[str] = set()
    for name in LANE_MODULES:
        names.update(getattr(import_module(name), "NIGHTLY_VERDICTS", ()))
    return frozenset(names)
