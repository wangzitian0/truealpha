import hashlib
import json
import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts.execution import ExtractionTemplate
from truealpha_contracts.release import (
    ArtifactRole,
    ModelRevisionRef,
    ReleaseArtifact,
    ReleaseManifest,
    live_release_manifest_id,
    live_release_payload,
    resolve_accepted_release,
)
from truealpha_contracts.universe import UniverseRef

SHA = "a" * 64
DIGEST = "sha256:" + SHA
GIT_SHA = "b" * 40


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _universe() -> UniverseRef:
    return UniverseRef(universe_id="universe:test", universe_version="2026-07-12", content_sha256="c" * 64)


def _artifact(role: ArtifactRole, *, image: str | None = None, digest: str = DIGEST) -> ReleaseArtifact:
    return ReleaseArtifact(
        role=role,
        image_or_bundle=image or f"ghcr.io/example/{role.value}",
        digest=digest,
        git_sha=GIT_SHA,
        sbom_sha256="d" * 64,
        signature_ref=f"sigstore:{role.value}",
    )


def _model_revision(*, revision: str = "2026-07-12", seed: str = "a") -> ModelRevisionRef:
    return ModelRevisionRef(
        provider="provider.example",
        model_id="extractor.financial-fact",
        immutable_revision=revision,
        endpoint_or_artifact_sha256=seed * 64,
        decoding_parameters_sha256="b" * 64,
    )


def _extraction_template(
    model_revision: ModelRevisionRef,
    *,
    version: str = "1.0.0",
) -> ExtractionTemplate:
    return ExtractionTemplate(
        template_name="financial-fact",
        template_version=version,
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        payload_model_key="contracts:FinancialFact",
        output_schema_sha256="c" * 64,
        instructions_sha256="d" * 64,
        extractor_implementation_sha256="e" * 64,
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
    )


def _manifest(**overrides) -> ReleaseManifest:
    migration_ids = ("0001.sql", "0002.sql")
    values = {
        "contract_version": "contracts:v1",
        "mart_schema_version": "mart:v1",
        "research_catalog_id": "research-catalog:" + "e" * 64,
        "research_catalog_sha256": "e" * 64,
        "universe": _universe(),
        "capture_scope_id": "capture-scope:" + "f" * 64,
        "capture_scope_sha256": "f" * 64,
        "applicability_catalog_id": "applicability:" + "1" * 64,
        "applicability_catalog_sha256": "1" * 64,
        "source_coverage_catalog_id": "source-coverage:" + "2" * 64,
        "source_coverage_catalog_sha256": "2" * 64,
        "source_readiness_report_id": "source-readiness:" + "3" * 64,
        "source_readiness_report_sha256": "3" * 64,
        "slo_catalog_id": "module-slo:" + "4" * 64,
        "slo_catalog_sha256": "4" * 64,
        "consumer_slo_catalog_id": "consumer-slo:" + "5" * 64,
        "consumer_slo_catalog_sha256": "5" * 64,
        "usage_telemetry_slo_catalog_id": "usage-telemetry-slo:" + "6" * 64,
        "usage_telemetry_slo_catalog_sha256": "6" * 64,
        "registry_snapshot_id": "registry-snapshot:" + "0" * 64,
        "registry_snapshot_sha256": "0" * 64,
        "source_registry_id": "source-registry:" + "5" * 64,
        "source_registry_sha256": "5" * 64,
        "semantic_type_registry_id": "semantic-type-registry:" + "6" * 64,
        "semantic_type_registry_sha256": "6" * 64,
        "identifier_type_registry_id": "identifier-type-registry:" + "a" * 64,
        "identifier_type_registry_sha256": "a" * 64,
        "configuration_sha256": {"data-engine": "7" * 64, "dagster": "8" * 64},
        "migration_ids": migration_ids,
        "migration_set_sha256": _hash(migration_ids),
        "artifacts": tuple(_artifact(role) for role in reversed(tuple(ArtifactRole))),
        "natural_refresh_requirement_ids": ("natural-refresh:" + "7" * 64,),
        "approved_model_revisions": (),
        "approved_extraction_templates": (),
        "created_at": datetime(2026, 7, 12, tzinfo=UTC),
        "manifest_signature_ref": "sigstore:release-test",
    }
    values.update(overrides)
    return ReleaseManifest(**values)


def test_release_manifest_is_complete_content_addressed_and_order_independent():
    manifest = _manifest()
    reordered = _manifest(
        migration_ids=tuple(reversed(manifest.migration_ids)),
        migration_set_sha256=_hash(tuple(sorted(manifest.migration_ids))),
        artifacts=tuple(reversed(manifest.artifacts)),
    )
    assert manifest.release_manifest_id == "release-manifest:" + manifest.manifest_sha256
    assert manifest.release_manifest_id == reordered.release_manifest_id
    assert {artifact.role for artifact in manifest.artifacts} == set(ArtifactRole)


def test_release_manifest_rejects_partial_or_mutable_artifacts():
    with pytest.raises(ValidationError, match="missing artifact roles"):
        _manifest(artifacts=(_artifact(ArtifactRole.DATA_ENGINE_DAGSTER),))
    with pytest.raises(ValidationError, match="floating tags"):
        _artifact(ArtifactRole.APP_WEB, image="ghcr.io/example/app:latest")
    with pytest.raises(ValidationError, match="model revisions must be immutable"):
        _model_revision(revision="latest")
    with pytest.raises(ValidationError, match="template versions must be immutable"):
        _extraction_template(_model_revision(), version="latest")
    with pytest.raises(ValidationError, match="approved together"):
        _manifest(approved_model_revisions=(_model_revision(),))


def test_release_binds_exact_template_objects_to_approved_model_revisions():
    model_revision = _model_revision()
    template = _extraction_template(model_revision)
    manifest = _manifest(
        approved_model_revisions=(model_revision,),
        approved_extraction_templates=(template,),
    )

    assert manifest.approved_model_revisions == (model_revision,)
    assert manifest.approved_extraction_templates == (template,)
    unapproved_revision = _model_revision(seed="f")
    with pytest.raises(ValidationError, match="model revision is not approved"):
        _manifest(
            approved_model_revisions=(unapproved_revision,),
            approved_extraction_templates=(template,),
        )
    with pytest.raises(ValidationError, match="content_sha256"):
        ModelRevisionRef(
            **model_revision.model_dump(exclude={"model_revision_id", "content_sha256"}),
            content_sha256="f" * 64,
        )
    with pytest.raises(ValidationError, match="content_sha256"):
        ExtractionTemplate(
            **template.model_dump(exclude={"extraction_template_id", "content_sha256"}),
            content_sha256="f" * 64,
        )


def test_release_manifest_rejects_forged_content_hashes():
    with pytest.raises(ValidationError, match="migration_set_sha256"):
        _manifest(migration_set_sha256="0" * 64)
    with pytest.raises(ValidationError, match="manifest_sha256"):
        _manifest(manifest_sha256="0" * 64)
    with pytest.raises(ValidationError, match="release_manifest_id"):
        _manifest(release_manifest_id="release-manifest:" + "0" * 64)


def test_release_manifest_rejects_post_run_evidence_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _manifest(accepted_capture_manifest_ids=("capture-manifest:" + "9" * 64,))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _manifest(extraction_invocations=("extraction-invocation:" + "8" * 64,))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _manifest(approved_extraction_template_ids=("extraction-template:" + "7" * 64,))


class _Repository:
    def __init__(self, manifest: ReleaseManifest | None):
        self.manifest = manifest

    def get(self, release_manifest_id: str) -> ReleaseManifest | None:
        if self.manifest is not None and self.manifest.release_manifest_id == release_manifest_id:
            return self.manifest
        return None


class _Verifier:
    def __init__(self, accepted: bool):
        self.accepted = accepted

    def verify(self, manifest: ReleaseManifest) -> bool:
        return self.accepted and bool(manifest.manifest_signature_ref)


def test_runtime_binding_resolves_signed_manifest_and_rejects_substitution():
    manifest = _manifest()
    resolved = resolve_accepted_release(
        _Repository(manifest),
        _Verifier(True),
        release_manifest_id=manifest.release_manifest_id,
        artifact_role=ArtifactRole.DATA_ENGINE_DAGSTER,
        artifact_digest=manifest.artifact(ArtifactRole.DATA_ENGINE_DAGSTER).digest,
        capture_scope_id=manifest.capture_scope_id,
        capture_scope_sha256=manifest.capture_scope_sha256,
        research_catalog_id=manifest.research_catalog_id,
        research_catalog_sha256=manifest.research_catalog_sha256,
        universe=manifest.universe,
        registry_snapshot_id=manifest.registry_snapshot_id,
        registry_snapshot_sha256=manifest.registry_snapshot_sha256,
        source_readiness_report_id=manifest.source_readiness_report_id,
        source_readiness_report_sha256=manifest.source_readiness_report_sha256,
        configuration_sha256=manifest.configuration_sha256,
    )
    assert resolved is manifest

    with pytest.raises(ValueError, match="signature verification failed"):
        resolve_accepted_release(
            _Repository(manifest),
            _Verifier(False),
            release_manifest_id=manifest.release_manifest_id,
            artifact_role=ArtifactRole.DATA_ENGINE_DAGSTER,
            artifact_digest=manifest.artifact(ArtifactRole.DATA_ENGINE_DAGSTER).digest,
            capture_scope_id=manifest.capture_scope_id,
            capture_scope_sha256=manifest.capture_scope_sha256,
            research_catalog_id=manifest.research_catalog_id,
            research_catalog_sha256=manifest.research_catalog_sha256,
            universe=manifest.universe,
            registry_snapshot_id=manifest.registry_snapshot_id,
            registry_snapshot_sha256=manifest.registry_snapshot_sha256,
            source_readiness_report_id=manifest.source_readiness_report_id,
            source_readiness_report_sha256=manifest.source_readiness_report_sha256,
            configuration_sha256=manifest.configuration_sha256,
        )
    with pytest.raises(ValueError, match="artifact digest"):
        resolve_accepted_release(
            _Repository(manifest),
            _Verifier(True),
            release_manifest_id=manifest.release_manifest_id,
            artifact_role=ArtifactRole.DATA_ENGINE_DAGSTER,
            artifact_digest="sha256:" + "0" * 64,
            capture_scope_id=manifest.capture_scope_id,
            capture_scope_sha256=manifest.capture_scope_sha256,
            research_catalog_id=manifest.research_catalog_id,
            research_catalog_sha256=manifest.research_catalog_sha256,
            universe=manifest.universe,
            registry_snapshot_id=manifest.registry_snapshot_id,
            registry_snapshot_sha256=manifest.registry_snapshot_sha256,
            source_readiness_report_id=manifest.source_readiness_report_id,
            source_readiness_report_sha256=manifest.source_readiness_report_sha256,
            configuration_sha256=manifest.configuration_sha256,
        )


# --- the live release identity is a measurement (#784, #712) ---------------------------
#
# It was `canonical_sha256({"kind": "production-topt-live-release"})`: one 64-hex value for
# every tag, every run and both environments. Every assertion below is red against that
# code -- the first two because a constant cannot tell two releases apart, the rest because
# there was nothing to validate.

_MIGRATIONS = ("0001_schemas", "20260908T1055_factors_question_coverage_report")
_CONTRACT = "e" * 64


def _live_id(**overrides) -> str:
    arguments = {
        "git_commit_sha": "v0.0.49",
        "migration_ids": _MIGRATIONS,
        "environment_contract_sha256": _CONTRACT,
    }
    arguments.update(overrides)
    return live_release_manifest_id(**arguments)


def test_two_releases_do_not_share_one_identity() -> None:
    assert _live_id(git_commit_sha="v0.0.49") != _live_id(git_commit_sha="v0.0.50")
    # ... and neither does a release that ships a different schema, or that changes what
    # the deployment has to supply.
    assert _live_id(migration_ids=(*_MIGRATIONS, "20260909T0000_datahub_next")) != _live_id()
    assert _live_id(environment_contract_sha256="f" * 64) != _live_id()


def test_the_identity_is_stable_for_the_same_inputs() -> None:
    assert _live_id() == _live_id()
    # Order is part of the measurement: `db/apply_migrations.sh` applies the files in glob
    # order, so a reordered set is a different schema, not the same one re-sorted.
    assert _live_id(migration_ids=tuple(reversed(_MIGRATIONS))) != _live_id()


def test_the_identity_keeps_the_content_addressed_shape_infra2_asserts() -> None:
    # `^[a-z][a-z0-9-]*:[0-9a-f]{64}$` in truealpha/truealpha/20.data_engine/deploy.py, and
    # `_CONTENT_ID` here.
    assert re.fullmatch(r"^release-manifest:[0-9a-f]{64}$", _live_id())


def test_the_payload_says_what_the_identity_measured() -> None:
    payload = live_release_payload(
        git_commit_sha="v0.0.49", migration_ids=_MIGRATIONS, environment_contract_sha256=_CONTRACT
    )
    assert payload == {
        "kind": "production-topt-live-release",
        "git_commit_sha": "v0.0.49",
        "migration_ids": list(_MIGRATIONS),
        "environment_contract_sha256": _CONTRACT,
    }
    assert live_release_manifest_id(
        git_commit_sha="v0.0.49", migration_ids=_MIGRATIONS, environment_contract_sha256=_CONTRACT
    ) == "release-manifest:" + _hash(payload)


def test_an_unidentified_build_is_named_unknown_rather_than_refused() -> None:
    """Local and CI inject no release ref; `unknown` is honest and is its own identity."""
    assert _live_id(git_commit_sha="unknown") not in {_live_id(), _live_id(git_commit_sha="v0.0.50")}


@pytest.mark.parametrize(
    "overrides",
    [
        {"git_commit_sha": ""},
        {"git_commit_sha": "  "},
        # git reads a leading "-" as an option, and whitespace makes a failure
        # non-deterministic (truealpha_runtime.deployed_release).
        {"git_commit_sha": "-v0.0.49"},
        {"git_commit_sha": "v 0.0.49"},
        {"migration_ids": ()},
        {"migration_ids": ("0001_schemas", "0001_schemas")},
        {"migration_ids": ("0001_schemas", "")},
        {"environment_contract_sha256": "not-a-hash"},
        {"environment_contract_sha256": ("E" * 64)},
    ],
)
def test_an_unmeasurable_input_is_refused_rather_than_hashed(overrides) -> None:
    with pytest.raises(ValueError):
        _live_id(**overrides)


def test_surrounding_whitespace_is_the_same_release_not_a_second_one() -> None:
    """A rendered template line ending in a newline must not mint a second identity."""
    assert _live_id(git_commit_sha=" v0.0.49\n") == _live_id(git_commit_sha="v0.0.49")
