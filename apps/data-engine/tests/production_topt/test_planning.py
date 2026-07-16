from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import (
    PRODUCTION_CONFIRMATION,
    ManualProductionToptRequest,
    ProductionReleaseBinding,
    persist_manual_production_plan,
    plan_manual_production_topt,
)
from truealpha_contracts import canonical_sha256
from truealpha_contracts.release import ArtifactRole, ReleaseArtifact, ReleaseManifest
from truealpha_contracts.universe import UniverseRef

CORPUS = Path(__file__).parents[1] / "fixtures" / "capture_control" / "corpus.v1.json"
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)


def _sha(character: str) -> str:
    return character * 64


def _release(universe: UniverseRef) -> ReleaseManifest:
    migration_ids = ("0023_capture_control.sql", "0025_manual_topt_capture_reads.sql")
    artifacts = tuple(
        ReleaseArtifact(
            role=role,
            image_or_bundle=f"ghcr.io/truealpha/{role.value}@sha256:{_sha('1')}",
            digest=f"sha256:{_sha('1')}",
            git_sha=_sha("2")[:40],
            sbom_sha256=_sha("3"),
            signature_ref=f"sigstore:{role.value}:v1",
        )
        for role in ArtifactRole
    )
    return ReleaseManifest(
        contract_version="contracts:v1",
        mart_schema_version="mart:topt-v1",
        research_catalog_id=f"research-catalog:{_sha('4')}",
        research_catalog_sha256=_sha("4"),
        universe=universe,
        capture_scope_id=f"capture-scope:{_sha('5')}",
        capture_scope_sha256=_sha("5"),
        applicability_catalog_id=f"applicability:{_sha('6')}",
        applicability_catalog_sha256=_sha("6"),
        source_coverage_catalog_id=f"source-coverage:{_sha('7')}",
        source_coverage_catalog_sha256=_sha("7"),
        source_readiness_report_id=f"source-readiness:{_sha('8')}",
        source_readiness_report_sha256=_sha("8"),
        slo_catalog_id=f"module-slo:{_sha('9')}",
        slo_catalog_sha256=_sha("9"),
        consumer_slo_catalog_id=f"consumer-slo:{_sha('a')}",
        consumer_slo_catalog_sha256=_sha("a"),
        usage_telemetry_slo_catalog_id=f"usage-telemetry-slo:{_sha('b')}",
        usage_telemetry_slo_catalog_sha256=_sha("b"),
        registry_snapshot_id=f"registry-snapshot:{_sha('c')}",
        registry_snapshot_sha256=_sha("c"),
        source_registry_id=f"source-registry:{_sha('d')}",
        source_registry_sha256=_sha("d"),
        semantic_type_registry_id=f"semantic-type-registry:{_sha('e')}",
        semantic_type_registry_sha256=_sha("e"),
        identifier_type_registry_id=f"identifier-type-registry:{_sha('f')}",
        identifier_type_registry_sha256=_sha("f"),
        configuration_sha256={"production-topt": _sha("0")},
        migration_ids=migration_ids,
        migration_set_sha256=canonical_sha256(migration_ids),
        artifacts=artifacts,
        natural_refresh_requirement_ids=(f"natural-refresh:{_sha('1')}",),
        created_at=CUTOFF - timedelta(days=1),
        manifest_signature_ref="sigstore:release-manifest:v1",
    )


def _corpus() -> dict[str, object]:
    return json.loads(CORPUS.read_text())


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _request(corpus: dict[str, object]) -> ManualProductionToptRequest:
    denominator = corpus["topt_denominator"]
    assert isinstance(denominator, dict)
    release = _release(
        UniverseRef(
            universe_id=str(denominator["universe_id"]),
            universe_version="topt-candidate-2026-03-31-v1",
            content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
        )
    )
    return ManualProductionToptRequest(
        release_manifest_id=release.release_manifest_id,
        cutoff=CUTOFF,
        run_sequence=1,
        confirmation=PRODUCTION_CONFIRMATION,
    )


class MemoryReleaseRepository:
    def __init__(self, release: ReleaseManifest) -> None:
        self.release = release

    def get(self, release_manifest_id: str) -> ReleaseManifest | None:
        return self.release if release_manifest_id == self.release.release_manifest_id else None


class StubSignatureVerifier:
    def __init__(self, verified: bool = True) -> None:
        self.verified = verified

    def verify(self, manifest: ReleaseManifest) -> bool:
        return self.verified


def _release_for_corpus(corpus: dict[str, object]) -> ReleaseManifest:
    denominator = corpus["topt_denominator"]
    assert isinstance(denominator, dict)
    return _release(
        UniverseRef(
            universe_id=str(denominator["universe_id"]),
            universe_version="topt-candidate-2026-03-31-v1",
            content_sha256="8b2f885e6161c01603b9d78882d411c7984ff6a3dbf35d636cb11e8c2ecfcf8f",
        )
    )


def _binding(release: ReleaseManifest) -> ProductionReleaseBinding:
    return ProductionReleaseBinding(
        artifact_digest=release.artifact(ArtifactRole.DATA_ENGINE_DAGSTER).digest,
        capture_scope_id=release.capture_scope_id,
        capture_scope_sha256=release.capture_scope_sha256,
        research_catalog_id=release.research_catalog_id,
        research_catalog_sha256=release.research_catalog_sha256,
        registry_snapshot_id=release.registry_snapshot_id,
        registry_snapshot_sha256=release.registry_snapshot_sha256,
        source_readiness_report_id=release.source_readiness_report_id,
        source_readiness_report_sha256=release.source_readiness_report_sha256,
        configuration_sha256=release.configuration_sha256,
    )


def _plan(
    corpus: dict[str, object],
    request: ManualProductionToptRequest | None = None,
    release: ReleaseManifest | None = None,
    *,
    verified: bool = True,
):
    accepted = release or _release_for_corpus(corpus)
    return plan_manual_production_topt(
        corpus,
        request or _request(corpus),
        release_repository=MemoryReleaseRepository(accepted),
        signature_verifier=StubSignatureVerifier(verified),
        release_binding=_binding(accepted),
    )


def test_plan_is_exact_deterministic_and_manual_only() -> None:
    corpus = _corpus()
    request = _request(corpus)

    release = _release_for_corpus(corpus)
    first = _plan(corpus, request, release)
    repeated = _plan(corpus, request, release)

    assert first == repeated
    assert (first.issuer_count, first.instrument_count, first.obligation_count) == (20, 21, 84)
    assert first.campaign.environment.value == "production"
    assert first.run.capture_scope_id == release.capture_scope_id
    assert len(first.source_requests) == len(first.work_items) == len(first.bindings) == 84
    assert len({item.source_request_id for item in first.source_requests}) == 84
    assert len({item.work_item_id for item in first.work_items}) == 84
    assert len({item.binding_id for item in first.bindings}) == 84
    assert {
        requirement: sum(item.capture_requirement_id == requirement for item in first.obligations)
        for requirement in {item.capture_requirement_id for item in first.obligations}
    } == {
        "financial-fact:v1": 21,
        "listing-identity:v1": 21,
        "market-price:v1": 21,
        "universe-membership:v1": 21,
    }


def test_plan_persistence_is_atomic_and_idempotent(connection) -> None:
    corpus = _corpus()
    plan = _plan(corpus)
    recorded_at = CUTOFF + timedelta(seconds=1)

    first = persist_manual_production_plan(connection, plan, recorded_at=recorded_at)
    repeated = persist_manual_production_plan(connection, plan, recorded_at=recorded_at)

    assert first == repeated
    assert first.environment == "production"
    assert (first.obligation_count, first.terminal_count, first.success_count) == (84, 0, 0)
    assert first.complete is False
    assert connection.execute(
        "select count(*) from raw.capture_checkpoints where run_id = %s",
        (plan.run.run_id,),
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda request: {**request.__dict__, "confirmation": "yes"}, "confirmation must be exactly"),
        (
            lambda request: {**request.__dict__, "release_manifest_id": "release-manifest:" + "0" * 64},
            "does not exist",
        ),
        (lambda request: {**request.__dict__, "cutoff": datetime(2026, 4, 2)}, "timezone-aware"),
        (
            lambda request: {**request.__dict__, "cutoff": datetime(2026, 3, 1, tzinfo=UTC)},
            "precedes the frozen TOPT list",
        ),
        (lambda request: {**request.__dict__, "run_sequence": 0}, "run_sequence must be positive"),
    ),
)
def test_plan_rejects_unsafe_operator_input(mutation, message: str) -> None:
    corpus = _corpus()
    request = ManualProductionToptRequest(**mutation(_request(corpus)))

    with pytest.raises((ValueError, LookupError), match=message):
        _plan(corpus, request)


def test_plan_rejects_release_universe_drift() -> None:
    corpus = _corpus()
    release = _release_for_corpus(corpus)
    drifted_release = _release(
        UniverseRef(
            universe_id="universe:topt-replaced",
            universe_version=release.universe.universe_version,
            content_sha256=release.universe.content_sha256,
        )
    )
    drifted_request = ManualProductionToptRequest(
        release_manifest_id=drifted_release.release_manifest_id,
        cutoff=CUTOFF,
        run_sequence=1,
        confirmation=PRODUCTION_CONFIRMATION,
    )

    with pytest.raises(ValueError, match="UniverseRef does not match"):
        _plan(corpus, drifted_request, drifted_release)


def test_plan_rejects_unverified_release() -> None:
    corpus = _corpus()
    with pytest.raises(ValueError, match="signature verification failed"):
        _plan(corpus, verified=False)
