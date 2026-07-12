"""Immutable, signed release bindings used by capture and promotion gates."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import ExtractionTemplate, ModelRevisionRef
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import UniverseRef

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTENT_ID = re.compile(r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$")


class ArtifactRole(StrEnum):
    DATA_ENGINE_DAGSTER = "data_engine_dagster"
    LLM_SERVICE = "llm_service"
    APP_WEB = "app_web"
    DB_MIGRATIONS = "db_migrations"


class ReleaseArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ArtifactRole
    image_or_bundle: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    git_sha: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_ref: str = Field(min_length=1)

    @field_validator("image_or_bundle")
    @classmethod
    def reject_floating_artifacts(cls, value: str) -> str:
        if value.endswith(":latest") or value.endswith(":main"):
            raise ValueError("release artifacts cannot use floating tags")
        return value


class ReleaseManifest(BaseModel):
    """The complete signed artifact set accepted for one exact product scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_manifest_id: str = ""
    contract_version: str = Field(min_length=1)
    mart_schema_version: str = Field(min_length=1)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    universe: UniverseRef
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_coverage_catalog_id: str = Field(pattern=r"^source-coverage:[0-9a-f]{64}$")
    source_coverage_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_readiness_report_id: str = Field(pattern=r"^source-readiness:[0-9a-f]{64}$")
    source_readiness_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slo_catalog_id: str = Field(pattern=r"^module-slo:[0-9a-f]{64}$")
    slo_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consumer_slo_catalog_id: str = Field(pattern=r"^consumer-slo:[0-9a-f]{64}$")
    consumer_slo_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    usage_telemetry_slo_catalog_id: str = Field(pattern=r"^usage-telemetry-slo:[0-9a-f]{64}$")
    usage_telemetry_slo_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_id: str = Field(pattern=r"^source-registry:[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_type_registry_id: str = Field(pattern=r"^semantic-type-registry:[0-9a-f]{64}$")
    semantic_type_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identifier_type_registry_id: str = Field(pattern=r"^identifier-type-registry:[0-9a-f]{64}$")
    identifier_type_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_sha256: dict[str, str] = Field(min_length=1)
    migration_ids: tuple[str, ...] = Field(min_length=1)
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[ReleaseArtifact, ...] = Field(min_length=1)
    natural_refresh_requirement_ids: tuple[str, ...] = Field(min_length=1)
    approved_model_revisions: tuple[ModelRevisionRef, ...] = ()
    approved_extraction_templates: tuple[ExtractionTemplate, ...] = ()
    created_at: datetime
    manifest_sha256: str = ""
    manifest_signature_ref: str = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")

    @field_validator("configuration_sha256")
    @classmethod
    def validate_configuration_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or not _SHA256.fullmatch(value) for key, value in values.items()):
            raise ValueError("configuration entries require names and lowercase sha256 values")
        return dict(sorted(values.items()))

    @field_validator("natural_refresh_requirement_ids")
    @classmethod
    def validate_content_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.startswith("natural-refresh:") or _CONTENT_ID.fullmatch(value) is None for value in values):
            raise ValueError("natural refresh requirements must be content-addressed IDs")
        if len(values) != len(set(values)):
            raise ValueError("natural refresh requirements must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ReleaseManifest:
        for reference_id, content_hash, label in (
            (self.research_catalog_id, self.research_catalog_sha256, "research catalog"),
            (self.capture_scope_id, self.capture_scope_sha256, "capture scope"),
            (self.applicability_catalog_id, self.applicability_catalog_sha256, "applicability catalog"),
            (self.source_coverage_catalog_id, self.source_coverage_catalog_sha256, "source coverage catalog"),
            (self.source_readiness_report_id, self.source_readiness_report_sha256, "source readiness report"),
            (self.slo_catalog_id, self.slo_catalog_sha256, "module SLO catalog"),
            (self.consumer_slo_catalog_id, self.consumer_slo_catalog_sha256, "consumer SLO catalog"),
            (
                self.usage_telemetry_slo_catalog_id,
                self.usage_telemetry_slo_catalog_sha256,
                "usage telemetry SLO catalog",
            ),
            (self.registry_snapshot_id, self.registry_snapshot_sha256, "registry snapshot"),
            (self.source_registry_id, self.source_registry_sha256, "source registry"),
            (
                self.semantic_type_registry_id,
                self.semantic_type_registry_sha256,
                "semantic type registry",
            ),
            (
                self.identifier_type_registry_id,
                self.identifier_type_registry_sha256,
                "identifier type registry",
            ),
        ):
            if not reference_id.endswith(f":{content_hash}"):
                raise ValueError(f"{label} ID and hash do not match")
        migration_ids = tuple(sorted(set(self.migration_ids)))
        if len(migration_ids) != len(self.migration_ids):
            raise ValueError("migration IDs must be unique")
        object.__setattr__(self, "migration_ids", migration_ids)
        expected_migration_hash = canonical_sha256(migration_ids)
        if self.migration_set_sha256 != expected_migration_hash:
            raise ValueError("migration_set_sha256 does not match migration IDs")

        artifacts = tuple(sorted(self.artifacts, key=lambda item: item.role.value))
        roles = [artifact.role for artifact in artifacts]
        if len(roles) != len(set(roles)):
            raise ValueError("release artifact roles must be unique")
        missing_roles = set(ArtifactRole) - set(roles)
        if missing_roles:
            raise ValueError(
                f"release manifest is missing artifact roles: {sorted(role.value for role in missing_roles)}"
            )
        object.__setattr__(self, "artifacts", artifacts)

        revisions = tuple(sorted(self.approved_model_revisions, key=lambda item: item.model_revision_id))
        if len({item.model_revision_id for item in revisions}) != len(revisions):
            raise ValueError("approved model revisions must be unique")
        templates = tuple(sorted(self.approved_extraction_templates, key=lambda item: item.extraction_template_id))
        if len({item.extraction_template_id for item in templates}) != len(templates):
            raise ValueError("approved extraction templates must be unique")
        object.__setattr__(self, "approved_model_revisions", revisions)
        object.__setattr__(self, "approved_extraction_templates", templates)
        if bool(revisions) != bool(templates):
            raise ValueError("model revisions and extraction templates must be approved together")
        approved_revisions = {item.model_revision_id: item.content_sha256 for item in revisions}
        for template in templates:
            if approved_revisions.get(template.model_revision_id) != template.model_revision_sha256:
                raise ValueError("extraction template model revision is not approved by this release")

        payload = self.model_dump(
            mode="json",
            exclude={"release_manifest_id", "manifest_sha256", "manifest_signature_ref"},
        )
        expected_hash = canonical_sha256(payload)
        expected_id = f"release-manifest:{expected_hash}"
        if self.manifest_sha256 and self.manifest_sha256 != expected_hash:
            raise ValueError("manifest_sha256 does not match release content")
        if self.release_manifest_id and self.release_manifest_id != expected_id:
            raise ValueError("release_manifest_id does not match release content")
        object.__setattr__(self, "manifest_sha256", expected_hash)
        object.__setattr__(self, "release_manifest_id", expected_id)
        return self

    def artifact(self, role: ArtifactRole) -> ReleaseArtifact:
        return next(artifact for artifact in self.artifacts if artifact.role is role)


class ReleaseManifestRepository(Protocol):
    def get(self, release_manifest_id: str) -> ReleaseManifest | None: ...


class ReleaseSignatureVerifier(Protocol):
    def verify(self, manifest: ReleaseManifest) -> bool: ...


def resolve_accepted_release(
    repository: ReleaseManifestRepository,
    verifier: ReleaseSignatureVerifier,
    *,
    release_manifest_id: str,
    artifact_role: ArtifactRole,
    artifact_digest: str,
    capture_scope_id: str,
    capture_scope_sha256: str,
    research_catalog_id: str,
    research_catalog_sha256: str,
    universe: UniverseRef,
    registry_snapshot_id: str,
    registry_snapshot_sha256: str,
    source_readiness_report_id: str,
    source_readiness_report_sha256: str,
    configuration_sha256: dict[str, str],
) -> ReleaseManifest:
    """Resolve and verify a real manifest instead of accepting hash-shaped input."""

    if not _DIGEST.fullmatch(artifact_digest):
        raise ValueError("artifact_digest must be an immutable sha256 digest")
    manifest = repository.get(release_manifest_id)
    if manifest is None:
        raise LookupError(f"release manifest {release_manifest_id} does not exist")
    if not verifier.verify(manifest):
        raise ValueError("release manifest signature verification failed")
    if manifest.capture_scope_id != capture_scope_id or manifest.capture_scope_sha256 != capture_scope_sha256:
        raise ValueError("release manifest capture scope does not match the requested run")
    if (
        manifest.research_catalog_id != research_catalog_id
        or manifest.research_catalog_sha256 != research_catalog_sha256
    ):
        raise ValueError("release manifest Research Catalog does not match the requested run")
    if manifest.universe != universe:
        raise ValueError("release manifest UniverseRef does not match the requested run")
    if (
        manifest.registry_snapshot_id != registry_snapshot_id
        or manifest.registry_snapshot_sha256 != registry_snapshot_sha256
    ):
        raise ValueError("release manifest registry snapshot does not match the requested run")
    if (
        manifest.source_readiness_report_id != source_readiness_report_id
        or manifest.source_readiness_report_sha256 != source_readiness_report_sha256
    ):
        raise ValueError("release manifest source readiness does not match the requested run")
    if manifest.configuration_sha256 != dict(sorted(configuration_sha256.items())):
        raise ValueError("release manifest configuration does not match the requested run")
    if manifest.artifact(artifact_role).digest != artifact_digest:
        raise ValueError("release manifest artifact digest does not match the requested run")
    return manifest
