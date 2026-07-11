from __future__ import annotations

import os
from functools import cached_property
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from truealpha_runtime.tiers import EnvironmentTier, resolve_environment_tier


class RuntimeSettings(BaseSettings):
    """Runtime/CICD settings shared by every Python application."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "dev"
    git_commit_sha: str = "unknown"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/truealpha"
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)

    s3_endpoint: str | None = "http://localhost:9000"
    s3_access_key: str = "minio"
    s3_secret_key: SecretStr = SecretStr("minio_local_secret")
    s3_bucket: str = "truealpha-raw"
    s3_region: str = "us-east-1"
    s3_raw_prefix: str = "raw"
    s3_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        return value

    @field_validator("s3_bucket")
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        if not 3 <= len(value) <= 63 or value.lower() != value or "_" in value:
            raise ValueError("S3_BUCKET must be a lowercase S3-compatible bucket name")
        return value

    @cached_property
    def environment_tier(self) -> EnvironmentTier:
        return resolve_environment_tier(self.app_env, github_actions=os.getenv("GITHUB_ACTIONS") == "true")

    @property
    def may_create_bucket(self) -> bool:
        return self.environment_tier in {
            EnvironmentTier.LOCAL_DEV,
            EnvironmentTier.LOCAL_TEST,
            EnvironmentTier.GITHUB_CI,
        }


runtime_settings = RuntimeSettings()


class DeploymentSettings(BaseSettings):
    """Compose/GitHub/infra2 controls owned by runtime, not application code."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    compose_project_name: str = "truealpha"
    env_suffix: str = ""
    restart_policy: Literal["no", "always", "on-failure", "unless-stopped"] = "unless-stopped"

    postgres_user: str = "postgres"
    postgres_password: SecretStr = SecretStr("postgres")
    postgres_db: str = "truealpha"
    postgres_ports: str = "127.0.0.1:5432:5432"
    minio_api_ports: str = "127.0.0.1:9000:9000"
    minio_console_ports: str = "127.0.0.1:9001:9001"

    registry: str = "ghcr.io"
    image_prefix: str = "wangzitian0/truealpha"
    image_tag: str = "local"
    web_ports: str = "127.0.0.1:3000:3000"
    llm_ports: str = "127.0.0.1:8000:8000"
    docker_log_max_size: str = "10m"
    docker_log_max_file: int = Field(default=3, ge=1, le=10)


deployment_settings = DeploymentSettings()
