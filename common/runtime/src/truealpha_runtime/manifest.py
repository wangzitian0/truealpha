from __future__ import annotations

from infra2_sdk.config import EnvironmentTier
from infra2_sdk.service import Dependency, DependencyKind, DependencyManifest

from truealpha_runtime.config import CoreRuntimeSettings, RuntimeSettings

_ALL = frozenset(EnvironmentTier)
_IDENTITY_FIELDS = frozenset({"environment", "git_commit_sha"})
_DATABASE_ENV_VARS = frozenset(
    name.upper() for name in CoreRuntimeSettings.model_fields if name not in _IDENTITY_FIELDS
)
_OBJECT_STORAGE_ENV_VARS = frozenset(
    name.upper() for name in RuntimeSettings.model_fields if name not in CoreRuntimeSettings.model_fields
)

DEPENDENCY_MANIFEST = DependencyManifest(
    dependencies=(
        Dependency(
            name="database",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=_DATABASE_ENV_VARS,
        ),
        Dependency(
            name="graph_store",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=frozenset({"DATABASE_URL"}),
        ),
        Dependency(
            name="object_storage",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=_ALL,
            env_vars=_OBJECT_STORAGE_ENV_VARS,
        ),
    )
)

__all__ = [
    "DEPENDENCY_MANIFEST",
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
]
