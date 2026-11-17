"""Scheduled universe names from the environment contract (#1410, #1416)."""

from __future__ import annotations

from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.universes import lane_name

from data_engine.config import settings

UNRECOGNISED_ENVIRONMENT_RULE = "staging"


def rule_environment(app_env: str | None = None) -> str:
    contract = load_environment_contract()
    environment = contract.normalize_environment(settings.app_env if app_env is None else app_env)
    return environment if environment in contract.environments else UNRECOGNISED_ENVIRONMENT_RULE


def scheduled_universe_names(app_env: str | None = None) -> tuple[str, ...]:
    return tuple(lane_name(key) for key in load_environment_contract().scheduled_universes(rule_environment(app_env)))


def any_environment_universe_names() -> tuple[str, ...]:
    contract = load_environment_contract()
    keys: list[str] = []
    for environment in contract.environments:
        keys.extend(key for key in contract.scheduled_universes(environment) if key not in keys)
    return tuple(lane_name(key) for key in keys)
