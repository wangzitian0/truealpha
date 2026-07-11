from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from truealpha_runtime.tiers import EnvironmentTier


class DependencyKind(StrEnum):
    CODE_DOMINANT = "code_dominant"


class RuntimeBackend(StrEnum):
    POSTGRES = "postgres"
    POSTGRES_KG = "postgres_kg"
    MINIO = "minio"
    S3_COMPATIBLE = "s3_compatible"


@dataclass(frozen=True)
class Dependency:
    name: str
    kind: DependencyKind
    required_in: frozenset[EnvironmentTier]
    env_vars: frozenset[str]
    local_backend: RuntimeBackend
    deployed_backend: RuntimeBackend


class DependencyManifest:
    def __init__(self, dependencies: tuple[Dependency, ...]) -> None:
        names = [dependency.name for dependency in dependencies]
        if len(names) != len(set(names)):
            raise ValueError("duplicate runtime dependency name")
        self._by_name = {dependency.name: dependency for dependency in dependencies}

    def __iter__(self):
        return iter(self._by_name.values())

    def get(self, name: str) -> Dependency:
        return self._by_name[name]

    def required_for(self, tier: EnvironmentTier) -> frozenset[str]:
        return frozenset(dependency.name for dependency in self if tier in dependency.required_in)


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
