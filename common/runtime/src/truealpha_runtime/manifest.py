from __future__ import annotations

from enum import StrEnum

from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.health import Dependency, DependencyKind, DependencyManifest

from truealpha_runtime.config import CoreRuntimeSettings, RuntimeSettings


class RuntimeBackend(StrEnum):
    POSTGRES = "postgres"
    POSTGRES_KG = "postgres_kg"
    MINIO = "minio"
    S3_COMPATIBLE = "s3_compatible"


_ALL = frozenset(EnvironmentTier)
_IDENTITY_FIELDS = frozenset({"app_env", "git_commit_sha"})
_DATABASE_ENV_VARS = frozenset(
    name.upper() for name in CoreRuntimeSettings.model_fields if name not in _IDENTITY_FIELDS
)
_OBJECT_STORAGE_ENV_VARS = frozenset(
    name.upper() for name in RuntimeSettings.model_fields if name not in CoreRuntimeSettings.model_fields
)

DEPENDENCY_MANIFEST = DependencyManifest(
    (
        Dependency(
            name="database",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=_DATABASE_ENV_VARS,
            local_backend=RuntimeBackend.POSTGRES,
            deployed_backend=RuntimeBackend.POSTGRES,
        ),
        Dependency(
            name="graph_store",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=frozenset({"DATABASE_URL"}),
            local_backend=RuntimeBackend.POSTGRES_KG,
            deployed_backend=RuntimeBackend.POSTGRES_KG,
        ),
        Dependency(
            name="object_storage",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=_OBJECT_STORAGE_ENV_VARS,
            local_backend=RuntimeBackend.MINIO,
            deployed_backend=RuntimeBackend.S3_COMPATIBLE,
        ),
    )
)

__all__ = [
    "DEPENDENCY_MANIFEST",
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
    "RuntimeBackend",
]
