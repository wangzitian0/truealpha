from enum import StrEnum


class EnvironmentTier(StrEnum):
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


def resolve_environment_tier(app_env: str, *, github_actions: bool = False) -> EnvironmentTier:
    normalized = app_env.strip().lower()
    if normalized in {"dev", "development", "local"}:
        return EnvironmentTier.LOCAL_DEV
    if normalized in {"test", "testing", "ci"}:
        return EnvironmentTier.GITHUB_CI if github_actions else EnvironmentTier.LOCAL_TEST
    if normalized == "preview":
        return EnvironmentTier.PREVIEW
    if normalized == "staging":
        return EnvironmentTier.STAGING
    if normalized in {"prod", "production"}:
        return EnvironmentTier.PRODUCTION
    raise ValueError(f"unknown APP_ENV: {app_env!r}")
