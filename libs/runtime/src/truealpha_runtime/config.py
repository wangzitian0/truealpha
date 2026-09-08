from __future__ import annotations

import os
from functools import cached_property
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from truealpha_runtime.tiers import EnvironmentTier, resolve_environment_tier


class RuntimeSettings(BaseSettings):
    """Runtime/CICD settings shared by every Python application."""

    # env_ignore_empty: an empty environment variable must not shadow a default or the next
    # alias (`DATABASE_URL=""` from an unrendered template line reached the URL validator as
    # "" and refused the service as not-PostgreSQL instead of falling through).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, env_ignore_empty=True)

    app_env: str = Field(default="dev", json_schema_extra={"source": "code", "injected": True, "group": "runtime"})
    git_commit_sha: str = Field(default="unknown", json_schema_extra={"source": "release", "group": "runtime"})

    # Composed by each service from the postgres service's password: the host and port
    # depend on the service's network topology, so the subclasses declare `composed_from`.
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/truealpha",
        json_schema_extra={
            "source": "runtime",
            "group": "postgres",
            "provided_by": "truealpha/postgres:POSTGRES_PASSWORD",
        },
    )
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60, json_schema_extra={"source": "code"})

    # Object-store coordinates and credentials come from the environment ONLY. No
    # defaults, for two reasons that agree:
    #
    # 1. `s3_access_key` / `s3_secret_key` were credentials living in tracked source. The
    #    values are local-dev placeholders, but the repository's own red line admits no
    #    "harmless" credential in code, and a placeholder that is also the FALLBACK is
    #    what gets used in an environment nobody configured.
    # 2. `http://localhost:9000` cannot be correct anywhere this actually deploys: inside
    #    a container it is that container's own loopback. Production injects no `S3_*`
    #    variables at all (verified 2026-08-31), so it has been falling back to exactly
    #    this triple -- which is #531, where object-storage writes failed and three runs
    #    died. The default turned "nobody configured object storage" into "connection
    #    refused", and a connection error sends you looking at the network.
    #
    # Empty means unconfigured. `storage.py` refuses rather than dialling a guess, so the
    # failure names the missing configuration instead of impersonating an outage.
    s3_endpoint: str | None = Field(default=None, json_schema_extra={"source": "code", "injected": True, "group": "s3"})
    s3_access_key: str = Field(
        default="", json_schema_extra={"source": "runtime", "empty_ok": True, "sensitive": True, "group": "s3"}
    )
    s3_secret_key: SecretStr = Field(
        default=SecretStr(""), json_schema_extra={"source": "runtime", "empty_ok": True, "group": "s3"}
    )
    s3_bucket: str = Field(default="truealpha-raw", json_schema_extra={"source": "code", "group": "s3"})
    s3_region: str = Field(default="us-east-1", json_schema_extra={"source": "code", "group": "s3"})
    s3_raw_prefix: str = Field(default="raw", json_schema_extra={"source": "code", "group": "s3"})
    s3_connect_timeout_seconds: int = Field(default=5, ge=1, le=60, json_schema_extra={"source": "code", "group": "s3"})

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

    @property
    def is_deployed(self) -> bool:
        """Preview, staging, production: a platform-provisioned stack, where every value the
        deployment injects must be present at boot (#759). Local and CI tiers run without them."""
        return self.environment_tier in {
            EnvironmentTier.PREVIEW,
            EnvironmentTier.STAGING,
            EnvironmentTier.PRODUCTION,
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
