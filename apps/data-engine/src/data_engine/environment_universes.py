"""The universes that an environment runs, read from the environment contract (#1410, #1416).

`environment_contract.v1.json` lists the universes of each environment and the status of each:
``scheduled`` or ``stopped``. This module turns that list into the names that the lanes use: ``topt``
and ``universe-list:qqq``. No lane keeps a universe list of its own.
"""

from __future__ import annotations

from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.config import settings

#: An environment that the contract does not name (local runs, CI) takes the rule of staging.
UNRECOGNISED_ENVIRONMENT_RULE = "staging"


def rule_environment(app_env: str | None = None) -> str:
    """The environment of the contract whose rule applies to ``app_env`` (default: the process environment)."""
    contract = load_environment_contract()
    environment = contract.normalize_environment(settings.app_env if app_env is None else app_env)
    return environment if environment in contract.environments else UNRECOGNISED_ENVIRONMENT_RULE


def universe_name(key: str) -> str:
    """The name that the lanes give a universe key: ``topt`` stays, any other key is ``universe-list:<key>``."""
    return key if key == "topt" else f"universe-list:{key}"


def scheduled_universe_names(app_env: str | None = None) -> tuple[str, ...]:
    """The universes that ``app_env`` runs on a schedule, in contract order."""
    scheduled = load_environment_contract().scheduled_universes(rule_environment(app_env))
    return tuple(universe_name(key) for key in scheduled)


def any_environment_universe_names() -> tuple[str, ...]:
    """The universes that at least one environment runs on a schedule, in contract order.

    A lane declares the verdicts of these universes. ``nightly_verdicts.is_registered`` then drops the
    verdicts of a universe that the process environment does not run.
    """
    contract = load_environment_contract()
    keys: list[str] = []
    for environment in contract.environments:
        keys.extend(key for key in contract.scheduled_universes(environment) if key not in keys)
    return tuple(universe_name(key) for key in keys)
