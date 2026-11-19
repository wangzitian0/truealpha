"""Single accessor for environment contract universe records (#1522)."""

from __future__ import annotations

from truealpha_contracts.environment_contract import (
    UniverseRecord,
    UniverseRole,
    load_environment_contract,
)


def record(key: str) -> UniverseRecord:
    return load_environment_contract().record(key)


def keys() -> tuple[str, ...]:
    return tuple(load_environment_contract().universes)


def served_keys() -> tuple[str, ...]:
    return tuple(k for k, u in load_environment_contract().universes.items() if not u.is_sentinel)


def lane_name(key: str) -> str:
    return record(key).lane_name


def key_of_lane(name: str) -> str:
    return load_environment_contract().universe_of_lane(name)


def universe_of(universe_id: str) -> str:
    return load_environment_contract().universe_of_id(universe_id)


def universes_with_role(role: UniverseRole) -> tuple[str, ...]:
    """Return every universe key whose roles include `role` in declaration order."""
    return tuple(k for k, u in load_environment_contract().universes.items() if role in u.roles)


def job_name(key: str) -> str:
    record(key)
    return f"{key}_live_pipeline"


def op_name(key: str) -> str:
    record(key)
    return f"run_{key}_live_tick"


def schedule_name(key: str) -> str:
    return f"{key}_daily_schedule" if record(key).is_sentinel else f"{key}_live_schedule"


def id_prefix_like(key: str) -> str:
    return f"{record(key).id_prefix}%"
