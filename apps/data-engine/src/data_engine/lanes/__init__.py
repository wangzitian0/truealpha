"""Per-lane Dagster definitions registry (#731, #876)."""

from __future__ import annotations

from importlib import import_module

import dagster as dg

LANE_MODULES: tuple[str, ...] = (
    "data_engine.lanes.capture",
    "data_engine.lanes.universe_refresh",
    "data_engine.lanes.market_data",
    "data_engine.lanes.qqq_prices",
    "data_engine.lanes.triggers",
    "data_engine.lanes.quality",
    "data_engine.lanes.standards",
    "data_engine.lanes.entity_identity",
    "data_engine.lanes.strategy_history",
    "data_engine.lanes.backtest",
    "data_engine.lanes.macro_data",
)


def lane_definitions(app_env: str | None = None) -> dict[str, dg.Definitions]:
    """Import every registered lane and return its ``defs`` keyed by module."""
    definitions: dict[str, dg.Definitions] = {}
    for name in LANE_MODULES:
        module = import_module(name)
        definitions[name] = (
            module.build_definitions(app_env)
            if app_env is not None and hasattr(module, "build_definitions")
            else module.defs
        )
    return definitions
