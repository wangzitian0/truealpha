from __future__ import annotations

import os
import re
from functools import cached_property
from typing import Any

from infra2_sdk.config import EnvironmentTier
from infra2_sdk.config import resolve_tier as _resolve_tier
from infra2_sdk.storage import PostgresSettings, S3Settings
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:55432/truealpha"
POSTGRES_PROVIDED_BY = "truealpha/postgres:POSTGRES_PASSWORD"
_DEPLOYED_TIERS = frozenset(
    {
        EnvironmentTier.PREVIEW,
        EnvironmentTier.STAGING,
        EnvironmentTier.PRODUCTION,
    }
)


def is_deployed_tier(tier: EnvironmentTier) -> bool:
    return tier in _DEPLOYED_TIERS


def _schema_field(default: Any, source: str, group: str, **kw: Any) -> Any:
    meta_keys = ("injected", "empty_ok", "sensitive", "scope", "composed_from", "provided_by")
    extra: dict[str, Any] = {"source": source, "group": group, **{k: kw.pop(k) for k in meta_keys if k in kw}}
    return Field(default=default, json_schema_extra=extra, **kw)


def database_url_field(*, composed_from: str | None = None) -> Any:
    kw = {"composed_from": composed_from} if composed_from is not None else {}
    return _schema_field(
        DEFAULT_DATABASE_URL, "runtime", "postgres", sensitive=True, provided_by=POSTGRES_PROVIDED_BY, **kw
    )


def resolve_environment_tier(environment: str, *, github_actions: bool = False) -> EnvironmentTier:
    try:
        return _resolve_tier(environment, github_actions=github_actions)
    except ValueError:
        raise ValueError(f"unknown ENVIRONMENT: {environment!r}") from None


class CoreRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    environment: str = _schema_field("dev", "code", "runtime", injected=True)
    git_commit_sha: str = _schema_field("unknown", "release", "runtime")
    database_url: str = database_url_field()
    database_connect_timeout_seconds: int = _schema_field(5, "code", "postgres", ge=1, le=60)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            PostgresSettings(url=value)
        except ValueError as exc:
            raise ValueError("DATABASE_URL must use PostgreSQL") from exc
        return value

    @cached_property
    def environment_tier(self) -> EnvironmentTier:
        return resolve_environment_tier(self.environment, github_actions=os.getenv("GITHUB_ACTIONS") == "true")

    @property
    def is_deployed(self) -> bool:
        return is_deployed_tier(self.environment_tier)


class RuntimeSettings(CoreRuntimeSettings):
    s3_endpoint: str | None = _schema_field(None, "code", "s3", injected=True)
    s3_access_key: str = _schema_field("", "runtime", "s3", empty_ok=True, sensitive=True)
    s3_secret_key: SecretStr = _schema_field(SecretStr(""), "runtime", "s3", empty_ok=True)
    s3_bucket: str = _schema_field("truealpha-raw", "code", "s3")
    s3_region: str = _schema_field("us-east-1", "code", "s3")
    s3_raw_prefix: str = _schema_field("raw", "code", "s3")
    s3_connect_timeout_seconds: int = _schema_field(5, "code", "s3", ge=1, le=60)

    @field_validator("s3_bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        cleaned = value.strip()
        if not _S3_BUCKET_RE.fullmatch(cleaned) or ".." in cleaned:
            raise ValueError("S3_BUCKET must be a lowercase S3-compatible bucket name")
        return cleaned

    @property
    def may_create_bucket(self) -> bool:
        return self.environment_tier in {
            EnvironmentTier.LOCAL_DEV,
            EnvironmentTier.LOCAL_TEST,
            EnvironmentTier.GITHUB_CI,
        }

    def to_s3_settings(self) -> S3Settings:
        endpoint = (self.s3_endpoint or "").strip()
        access_key = self.s3_access_key.strip()
        secret_key = self.s3_secret_key.get_secret_value().strip()
        if not endpoint:
            raise ValueError("S3_ENDPOINT must not be empty")
        if not access_key or not secret_key:
            raise ValueError("S3_ACCESS_KEY and S3_SECRET_KEY must not be empty")
        return S3Settings(
            endpoint_url=endpoint,
            bucket=self.s3_bucket,
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=self.s3_region,
            use_path_style=True,
            connect_timeout_seconds=float(self.s3_connect_timeout_seconds),
            read_timeout_seconds=float(self.s3_connect_timeout_seconds),
        )
