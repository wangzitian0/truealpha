"""Compatibility import path for the released SDK environment contract (#820)."""

from infra2_sdk.runtime.environment import EnvironmentTier as EnvironmentTier
from infra2_sdk.runtime.environment import resolve_environment_tier as _resolve_tier

__all__ = ["EnvironmentTier", "resolve_environment_tier"]


def resolve_environment_tier(app_env: str, *, github_actions: bool = False) -> EnvironmentTier:
    """Keep the application keyword/error surface while sharing normalization."""
    try:
        return _resolve_tier(app_env, github_actions=github_actions)
    except ValueError:
        raise ValueError(f"unknown APP_ENV: {app_env!r}") from None
