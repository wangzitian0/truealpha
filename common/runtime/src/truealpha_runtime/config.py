from __future__ import annotations

import os
from functools import cached_property
from typing import Any

from infra2_sdk.runtime.environment import EnvironmentTier, RuntimeEnvKey
from infra2_sdk.runtime.environment import resolve_environment_tier as _resolve_tier
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"
POSTGRES_PROVIDED_BY = "truealpha/postgres:POSTGRES_PASSWORD"


def _schema_field(default: Any, source: str, group: str, **kw: Any) -> Any:
    meta_keys = ("injected", "empty_ok", "sensitive", "scope", "composed_from", "provided_by")
    extra: dict[str, Any] = {"source": source, "group": group, **{k: kw.pop(k) for k in meta_keys if k in kw}}
    return Field(default=default, json_schema_extra=extra, **kw)


def database_url_field(*, composed_from: str | None = None) -> Any:
    kw = {"composed_from": composed_from} if composed_from is not None else {}
    return _schema_field(DEFAULT_DATABASE_URL, "runtime", "postgres", provided_by=POSTGRES_PROVIDED_BY, **kw)


def resolve_environment_tier(app_env: str, *, github_actions: bool = False) -> EnvironmentTier:
    try:
        return _resolve_tier(app_env, github_actions=github_actions)
    except ValueError:
        raise ValueError(f"unknown APP_ENV: {app_env!r}") from None


class CoreRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
        populate_by_name=True,
    )

    app_env: str = _schema_field(
        "dev", "code", "runtime", injected=True, validation_alias=AliasChoices(RuntimeEnvKey.ENVIRONMENT.value, "APP_ENV")
    )
    git_commit_sha: str = _schema_field("unknown", "release", "runtime")
    database_url: str = database_url_field()
    database_connect_timeout_seconds: int = _schema_field(5, "code", "postgres", ge=1, le=60)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        return value

    @cached_property
    def environment_tier(self) -> EnvironmentTier:
        return resolve_environment_tier(self.app_env, github_actions=os.getenv("GITHUB_ACTIONS") == "true")

    @property
    def is_deployed(self) -> bool:
        return self.environment_tier in {
            EnvironmentTier.PREVIEW,
            EnvironmentTier.STAGING,
            EnvironmentTier.PRODUCTION,
        }


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
        if not 3 <= len(value) <= 63 or value.lower() != value or "_" in value:
            raise ValueError("S3_BUCKET must be a lowercase S3-compatible bucket name")
        return value

    @property
    def may_create_bucket(self) -> bool:
        return self.environment_tier in {
            EnvironmentTier.LOCAL_DEV,
            EnvironmentTier.LOCAL_TEST,
            EnvironmentTier.GITHUB_CI,
        }

    def to_s3_settings(self):
        from infra2_sdk.runtime.s3 import S3Settings

        return S3Settings(
            bucket=self.s3_bucket,
            endpoint_url=(self.s3_endpoint or "").strip() or None,
            access_key_id=self.s3_access_key or None,
            secret_access_key=self.s3_secret_key.get_secret_value() or None,
            region_name=self.s3_region,
            addressing_style="path",
            connect_timeout_seconds=float(self.s3_connect_timeout_seconds),
            read_timeout_seconds=float(self.s3_connect_timeout_seconds),
        )
