from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_capture_tiny.e0_slice import CUTOFF, run_e0_slice
from data_engine.batches.mvp_capture_tiny.e1_slice import (
    E1_ASSET_NAME,
    E1CaseResult,
    E1ProvisionalActivation,
    EphemeralPostgresEvidenceRepository,
    EphemeralPostgresRecordRepository,
    FindingClass,
    MvpCaptureTinyEvidence,
    _base_probe,
    _restatement_pair,
    build_e1_definitions,
    evaluate_evidence_variant,
    run_e1_suite,
    validate_boundary_probe,
)
from data_engine.config import settings
from pydantic import ValidationError
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import SnapshotCellSelection
from truealpha_contracts.release import ArtifactRole, ReleaseArtifact, ReleaseManifest
from truealpha_contracts.universe import UniverseRef

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)


@pytest.fixture
def connection():
    try:
        active_connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    with active_connection:
        yield active_connection


def test_dagster_executes_all_terminal_e1_cases(connection):
    definitions = build_e1_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        activation=E1ProvisionalActivation(),
    )
    dg.Definitions.validate_loadable(definitions)

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    evidence = result.output_for_node(E1_ASSET_NAME)
    repeated = definitions.get_implicit_global_asset_job_def().execute_in_process()
    repeated_evidence = repeated.output_for_node(E1_ASSET_NAME)

    assert result.success
    assert evidence.fixture_snapshot_id == evidence.postgres_snapshot_id
    assert evidence.fixture_runner_selection_id == evidence.postgres_runner_selection_id
    assert {case.case_id for case in evidence.cases} == {
        "applicable-success",
        "missing-raw-evidence",
        "missing-normalized-evidence",
        "append-only-restatement",
        "wrong-listing-identity",
        "publication-boundary",
        "look-ahead-sentinel",
        "reordered-and-repeated-execution",
    }
    assert all(case.passed for case in evidence.cases)
    assert evidence.stable_handoff is False
    assert repeated.success
    assert repeated_evidence.evidence_id == evidence.evidence_id
    assert EphemeralPostgresEvidenceRepository(connection).get(evidence.evidence_id) == evidence


def test_restatement_pit_excludes_superseded_record(connection):
    subject, original, amended, _ledger = _restatement_pair(REPOSITORY_ROOT)
    repository = EphemeralPostgresRecordRepository(connection)
    repository.put(amended)
    repository.put(original)

    before = repository.select_pit(
        subject=subject,
        semantic_type_id=original.draft.semantic_type_id,
        as_of=amended.draft.knowable_at - timedelta(microseconds=1),
        valid_on=date(2020, 12, 31),
    )
    after = repository.select_pit(
        subject=subject,
        semantic_type_id=original.draft.semantic_type_id,
        as_of=amended.draft.knowable_at,
        valid_on=date(2020, 12, 31),
    )

    assert before == (original,)
    assert after == (amended,)


def test_look_ahead_sentinel_rejects_a_future_known_vintage():
    base = run_e0_slice(REPOSITORY_ROOT)
    _subject, _original, amended, _ledger = _restatement_pair(REPOSITORY_ROOT)

    evaluation = evaluate_evidence_variant(
        base,
        manifest_as_of=amended.draft.knowable_at - timedelta(microseconds=1),
        knowable_at=amended.draft.knowable_at,
    )

    assert not evaluation.ready
    assert any(reason.startswith("evidence.future_knowledge:") for reason in evaluation.blocking_reason_codes)


def test_raw_fetch_precedes_normalization_and_recording():
    _subject, original, amended, ledger = _restatement_pair(REPOSITORY_ROOT)
    by_sha256 = {entry.envelope.object.sha256: entry for entry in ledger.entries}

    for record in (original, amended):
        raw = by_sha256[record.raw_object_sha256]
        assert raw.envelope.source_published_at <= raw.envelope.fetched_at
        assert raw.envelope.fetched_at <= record.draft.produced_at
        assert record.draft.produced_at <= record.recorded_at


def test_failed_case_evidence_is_content_addressed_and_append_only(connection):
    accepted = run_e1_suite(REPOSITORY_ROOT, connection)
    failed_case = E1CaseResult(
        **accepted.cases[0].model_dump(exclude={"passed", "classification"}),
        passed=False,
        classification=FindingClass.LOCAL_CAPTURE_BUG,
    )
    values = accepted.model_dump(mode="python", exclude={"evidence_id", "content_sha256", "cases"})
    failed = MvpCaptureTinyEvidence(**values, cases=(failed_case, *accepted.cases[1:]))
    repository = EphemeralPostgresEvidenceRepository(connection)

    assert repository.put(failed)
    assert repository.put(failed) is False
    assert repository.get(failed.evidence_id) == failed
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(
            "update mvp_capture_tiny_evidence set content_sha256 = %s where evidence_id = %s",
            ("0" * 64, failed.evidence_id),
        )


def test_frozen_case_outputs_bind_the_declared_artifacts(connection):
    evidence = run_e1_suite(REPOSITORY_ROOT, connection)
    by_case = {case.case_id: case for case in evidence.cases}

    assert len(by_case["applicable-success"].observed_ids) == 4
    assert by_case["publication-boundary"].observed_ids[0] not in {
        identifier for identifier in by_case["applicable-success"].observed_ids if identifier.startswith("snapshot:")
    }
    assert by_case["wrong-listing-identity"].blocker_codes == ("wrong-listing-at-cutoff",)
    assert by_case["look-ahead-sentinel"].blocker_codes == ("future-known-vintage",)


def _accepted_release_manifest() -> ReleaseManifest:
    migration_ids = ("0001.sql",)
    return ReleaseManifest(
        contract_version="contracts:v1",
        mart_schema_version="mart:v1",
        research_catalog_id="research-catalog:" + "1" * 64,
        research_catalog_sha256="1" * 64,
        universe=UniverseRef(
            universe_id="universe:topt-test",
            universe_version="2026-07-12",
            content_sha256="2" * 64,
        ),
        capture_scope_id="capture-scope:" + "3" * 64,
        capture_scope_sha256="3" * 64,
        applicability_catalog_id="applicability:" + "4" * 64,
        applicability_catalog_sha256="4" * 64,
        source_coverage_catalog_id="source-coverage:" + "5" * 64,
        source_coverage_catalog_sha256="5" * 64,
        source_readiness_report_id="source-readiness:" + "6" * 64,
        source_readiness_report_sha256="6" * 64,
        slo_catalog_id="module-slo:" + "7" * 64,
        slo_catalog_sha256="7" * 64,
        consumer_slo_catalog_id="consumer-slo:" + "8" * 64,
        consumer_slo_catalog_sha256="8" * 64,
        usage_telemetry_slo_catalog_id="usage-telemetry-slo:" + "9" * 64,
        usage_telemetry_slo_catalog_sha256="9" * 64,
        registry_snapshot_id="registry-snapshot:" + "a" * 64,
        registry_snapshot_sha256="a" * 64,
        source_registry_id="source-registry:" + "b" * 64,
        source_registry_sha256="b" * 64,
        semantic_type_registry_id="semantic-type-registry:" + "c" * 64,
        semantic_type_registry_sha256="c" * 64,
        identifier_type_registry_id="identifier-type-registry:" + "d" * 64,
        identifier_type_registry_sha256="d" * 64,
        configuration_sha256={"data-engine": "e" * 64},
        migration_ids=migration_ids,
        migration_set_sha256=canonical_sha256(migration_ids),
        artifacts=tuple(
            ReleaseArtifact(
                role=role,
                image_or_bundle=f"ghcr.io/example/{role.value}",
                digest="sha256:" + "f" * 64,
                git_sha="0" * 40,
                sbom_sha256="1" * 64,
                signature_ref=f"sigstore:{role.value}",
            )
            for role in ArtifactRole
        ),
        natural_refresh_requirement_ids=("natural-refresh:" + "2" * 64,),
        created_at=CUTOFF,
        manifest_signature_ref="sigstore:accepted-release",
    )


def test_accepted_release_cannot_activate_the_provisional_batch(connection):
    accepted_release = _accepted_release_manifest()

    with pytest.raises(ValueError, match="not release-activated"):
        build_e1_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            activation=accepted_release,
        )


def test_ephemeral_repository_rejects_update_and_delete(connection):
    base = run_e0_slice(REPOSITORY_ROOT)
    repository = EphemeralPostgresRecordRepository(connection)
    assert repository.put(base.record)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(
            "update mvp_capture_tiny_normalized_records set subject_id = %s",
            ("issuer.changed",),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute("delete from mvp_capture_tiny_normalized_records")


def test_partial_write_rolls_back_and_retry_succeeds():
    base = run_e0_slice(REPOSITORY_ROOT)
    try:
        active_connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    with active_connection as connection:
        repository = EphemeralPostgresRecordRepository(connection)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            with connection.transaction():
                assert repository.put(base.record)
                raise RuntimeError("simulated interruption")

        assert repository.count() == 0
        assert repository.put(base.record)
        assert repository.put(base.record) is False


@pytest.mark.parametrize(
    ("updates", "blocker"),
    [
        ({"confidence": None}, "evidence.missing_confidence"),
        ({"policy_versions": {}}, "evidence.missing_policy"),
        ({"knowable_at": None}, "evidence.missing_knowable_at"),
    ],
)
def test_capture_evidence_fails_closed(updates, blocker):
    evaluation = evaluate_evidence_variant(run_e0_slice(REPOSITORY_ROOT), **updates)

    assert not evaluation.ready
    assert any(code.startswith(blocker) for code in evaluation.blocking_reason_codes)


def test_stale_evidence_fails_closed():
    base = run_e0_slice(REPOSITORY_ROOT)
    evaluation = evaluate_evidence_variant(
        base,
        knowable_at=base.capture_manifest.as_of - base.capture_requirement.maximum_age - timedelta(seconds=1),
    )

    assert not evaluation.ready
    assert any("stale" in code for code in evaluation.blocking_reason_codes)


def test_required_snapshot_cell_cannot_omit_records():
    base = run_e0_slice(REPOSITORY_ROOT)

    with pytest.raises(ValidationError):
        SnapshotCellSelection(demand=base.snapshot.selections[0].demand)


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"observed_subject": {"kind": "issuer", "id": "issuer.changed"}}, "identity mismatch"),
        ({"observed_listing_id": "listing.nyse.nvda"}, "wrong listing"),
        ({"observed_share_class": "preferred"}, "wrong share class"),
        ({"price_basis": "adjusted"}, "double count"),
        ({"fx_knowable_at": None}, "future-known"),
        ({"maximum_fx_age": timedelta(seconds=1)}, "stale"),
        ({"observed_source_entry_id": "source.changed:1"}, "source registry binding drift"),
        ({"observed_semantic_type_id": "semantic.changed"}, "semantic type binding drift"),
    ],
)
def test_boundary_controls_reject_unsafe_bindings(updates, error):
    probe = _base_probe(run_e0_slice(REPOSITORY_ROOT))
    if updates.get("fx_knowable_at") is None and "fx_knowable_at" in updates:
        updates = {"fx_knowable_at": probe.as_of + timedelta(seconds=1)}

    with pytest.raises(ValueError, match=error):
        validate_boundary_probe(probe.model_copy(update=updates))
