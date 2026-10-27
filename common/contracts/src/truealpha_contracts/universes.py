"""The one accessor of the universe record (#1522).

`environment_contract.v1.json` holds one record per universe: key, id prefix, lane name, membership,
roles, report sample and tick cron. Every reader of a universe asks this module. No other file keeps
its own copy or list of a universe key, an id prefix, a lane name, a member list or a tick time.

A universe has three spellings, and each has one lookup:

* the key (``qqq``) names the record: `record(key)`;
* the lane name (``universe-list:qqq``) is the name the lanes, the run tags and the stored verdicts
  give the universe: `lane_name(key)` and `key_of_lane(name)`;
* the id (``universe:qqq-us-2026-03-31``) starts with the id prefix of the record, and the longest
  prefix wins: `universe_of(universe_id)`. The id is never split.

A key, id or lane name outside the record raises `UnknownUniverse`. There is no default universe.

`mart.current_pointer` is keyed `(environment, universe_id, universe_version, factor_id)`. A consumer
that resolves it without the universe serves whichever pipeline advanced last (the 24-cell canary run
once displaced the 84-cell TOPT run in all five consumers). A consumer therefore filters the pointer
by `record(key).id_prefix`.
"""

from __future__ import annotations

from truealpha_contracts.environment_contract import (
    UniverseRecord,
    UniverseRole,
    UnknownUniverse,
    load_environment_contract,
)

__all__ = [
    "UniverseRecord",
    "UnknownUniverse",
    "id_prefix_like",
    "key_of_lane",
    "keys",
    "lane_name",
    "record",
    "served_keys",
    "universe_of",
    "universe_with_role",
]


def record(key: str) -> UniverseRecord:
    """The record of the universe ``key``. A key outside the record raises."""
    return load_environment_contract().record(key)


def keys() -> tuple[str, ...]:
    """Every universe key of the record, in contract order."""
    return tuple(load_environment_contract().universes)


def served_keys() -> tuple[str, ...]:
    """The keys of the universes that serve a governed head: every record except a sentinel."""
    contract = load_environment_contract()
    return tuple(key for key, universe in contract.universes.items() if not universe.is_sentinel)


def lane_name(key: str) -> str:
    """The name that the lanes give the universe ``key``."""
    return record(key).lane_name


def key_of_lane(name: str) -> str:
    """The key of the universe that the lanes call ``name``. A name outside the record raises."""
    return load_environment_contract().universe_of_lane(name)


def universe_of(universe_id: str) -> str:
    """The key of the universe that governs ``universe_id``: the longest id prefix wins. The id is not split."""
    return load_environment_contract().universe_of_id(universe_id)


def universe_with_role(role: UniverseRole) -> str:
    """The key of the one universe that holds ``role``. Zero or several universes raise."""
    holders = [key for key, universe in load_environment_contract().universes.items() if role in universe.roles]
    if len(holders) != 1:
        raise UnknownUniverse(f"the role {role!r} needs exactly one universe, the record holds {holders}")
    return holders[0]


def id_prefix_like(key: str) -> str:
    """The SQL ``like`` pattern of every universe id of ``key``."""
    return f"{record(key).id_prefix}%"
