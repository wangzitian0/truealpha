# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; from functools import cached_property; from typing import Any; from infra2_sdk.runtime.environment import EnvironmentTier; from infra2_sdk.runtime.environment import resolve_environment_tier as _resolve_tier; from pydantic import Field, SecretStr, field_validator; from pydantic_settings import BaseSettings, SettingsConfigDict; DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"; POSTGRES_PROVIDED_BY = "truealpha/postgres:POSTGRES_PASSWORD"
def database_url_field(*, composed_from: str | None = None) -> Any:
    extra: dict[str, Any] = {"source": "runtime", "group": "postgres", "provided_by": POSTGRES_PROVIDED_BY}
    if composed_from is not None: extra["composed_from"] = composed_from
    return Field(default=DEFAULT_DATABASE_URL, json_schema_extra=extra)
def resolve_environment_tier(app_env: str, *, github_actions: bool = False) -> EnvironmentTier:
    try: return _resolve_tier(app_env, github_actions=github_actions)
    except ValueError: raise ValueError(f"unknown APP_ENV: {app_env!r}") from None
class CoreRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, env_ignore_empty=True, populate_by_name=True); app_env: str = Field(default="dev", json_schema_extra={"source": "code", "injected": True, "group": "runtime"}); git_commit_sha: str = Field(default="unknown", json_schema_extra={"source": "release", "group": "runtime"}); database_url: str = database_url_field(); database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60, json_schema_extra={"source": "code", "group": "postgres"})
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgres://")): raise ValueError("DATABASE_URL must use PostgreSQL")
        return value
    @cached_property
    def environment_tier(self) -> EnvironmentTier: return resolve_environment_tier(self.app_env, github_actions=os.getenv("GITHUB_ACTIONS") == "true")
    @property
    def is_deployed(self) -> bool: return self.environment_tier in {EnvironmentTier.PREVIEW, EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}
class RuntimeSettings(CoreRuntimeSettings):
    s3_endpoint: str | None = Field(default=None, json_schema_extra={"source": "code", "injected": True, "group": "s3"}); s3_access_key: str = Field(default="", json_schema_extra={"source": "runtime", "empty_ok": True, "sensitive": True, "group": "s3"}); s3_secret_key: SecretStr = Field(default=SecretStr(""), json_schema_extra={"source": "runtime", "empty_ok": True, "group": "s3"}); s3_bucket: str = Field(default="truealpha-raw", json_schema_extra={"source": "code", "group": "s3"}); s3_region: str = Field(default="us-east-1", json_schema_extra={"source": "code", "group": "s3"}); s3_raw_prefix: str = Field(default="raw", json_schema_extra={"source": "code", "group": "s3"}); s3_connect_timeout_seconds: int = Field(default=5, ge=1, le=60, json_schema_extra={"source": "code", "group": "s3"})
    @field_validator("s3_bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        if not 3 <= len(value) <= 63 or value.lower() != value or "_" in value: raise ValueError("S3_BUCKET must be a lowercase S3-compatible bucket name")
        return value
    @property
    def may_create_bucket(self) -> bool: return self.environment_tier in {EnvironmentTier.LOCAL_DEV, EnvironmentTier.LOCAL_TEST, EnvironmentTier.GITHUB_CI}
    def to_s3_settings(self): from infra2_sdk.runtime.s3 import S3Settings; return S3Settings( bucket=self.s3_bucket, endpoint_url=(self.s3_endpoint or "").strip() or None, access_key_id=self.s3_access_key or None, secret_access_key=self.s3_secret_key.get_secret_value() or None, region_name=self.s3_region, addressing_style="path", connect_timeout_seconds=float(self.s3_connect_timeout_seconds), read_timeout_seconds=float(self.s3_connect_timeout_seconds), )
