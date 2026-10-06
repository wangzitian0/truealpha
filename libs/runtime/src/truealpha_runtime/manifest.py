from __future__ import annotations

from enum import StrEnum

from infra2_sdk.runtime.dependencies import Dependency, DependencyKind, DependencyManifest

from truealpha_runtime.tiers import EnvironmentTier


class RuntimeBackend(StrEnum):
    POSTGRES = "postgres"
    POSTGRES_KG = "postgres_kg"
    MINIO = "minio"
    S3_COMPATIBLE = "s3_compatible"


_ALL = frozenset(EnvironmentTier)

DEPENDENCY_MANIFEST = DependencyManifest(
    (
        Dependency(
            name="database",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=frozenset({"DATABASE_URL", "DATABASE_CONNECT_TIMEOUT_SECONDS"}),
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
            env_vars=frozenset(
                {
                    "S3_ENDPOINT",
                    "S3_ACCESS_KEY",
                    "S3_SECRET_KEY",
                    "S3_BUCKET",
                    "S3_REGION",
                    "S3_RAW_PREFIX",
                    "S3_CONNECT_TIMEOUT_SECONDS",
                }
            ),
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
