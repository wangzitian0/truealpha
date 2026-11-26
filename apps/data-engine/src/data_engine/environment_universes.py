"""Scheduled universe names from the environment contract (#1410, #1416)."""

from __future__ import annotations

import dagster as dg
from infra2_sdk.config import resolve_tier
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.universes import lane_name

from data_engine.config import settings

UNRECOGNISED_ENVIRONMENT_RULE = "staging"


def rule_environment(environment: str | None = None) -> str:
    contract = load_environment_contract()
    raw = settings.environment if environment is None else environment
    resolved = contract.normalize_environment(raw)
    if resolved in contract.environments:
        return resolved
    try:
        tier = resolve_tier(raw).value
        if tier in contract.environments:
            return tier
    except ValueError:
        pass
    return UNRECOGNISED_ENVIRONMENT_RULE


def live_vendor_calls_enabled(environment: str | None = None) -> bool:
    contract = load_environment_contract()
    raw = settings.environment if environment is None else environment
    normalized = contract.normalize_environment(raw)
    if normalized not in contract.environments:
        try:
            normalized = resolve_tier(raw).value
        except ValueError:
            normalized = ""
    if normalized in contract.environments:
        return bool(contract.environments[normalized].live_vendor_calls)
    if (raw or "").strip().lower() in {"dev", "ci", "local_dev"}:
        return bool(contract.environments[UNRECOGNISED_ENVIRONMENT_RULE].live_vendor_calls)
    return False


def contract_schedule_status(environment: str | None = None) -> dg.DefaultScheduleStatus:
    return (
        dg.DefaultScheduleStatus.RUNNING if live_vendor_calls_enabled(environment) else dg.DefaultScheduleStatus.STOPPED
    )


def scheduled_universe_names(environment: str | None = None) -> tuple[str, ...]:
    return tuple(
        lane_name(key) for key in load_environment_contract().scheduled_universes(rule_environment(environment))
    )


def any_environment_universe_names() -> tuple[str, ...]:
    contract = load_environment_contract()
    keys: list[str] = []
    for environment in contract.environments:
        keys.extend(key for key in contract.scheduled_universes(environment) if key not in keys)
    return tuple(lane_name(key) for key in keys)
