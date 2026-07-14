from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import cast

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation import e3_slice
from data_engine.batches.mvp_medium_validation.e3_slice import (
    D2_E2_RUNTIME_HANDOFF_ID,
    D2_E3_ASSET_NAME,
    D2E3Activation,
    D2E3CapturePlan,
    D2E3Evidence,
    D2E3RowCompleteManifest,
    FrozenToptDenominator,
    FrozenToptMarketFixture,
    build_d2_e3_definitions,
    load_topt_denominator,
    load_topt_market_fixture,
    run_d2_e3,
)
from data_engine.config import settings
from data_engine.contract_repository import (
    PostgresCaptureEvaluationRepository,
    PostgresCaptureManifestRepository,
    PostgresCaptureScopeRepository,
)
from data_engine.mvp_medium_pipeline import LandedMediumCapture
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import ValidationError
from truealpha_contracts import (
    CaptureManifest,
    RawIngestionEnvelope,
    RawObjectRef,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.release import ReleaseManifest

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d2-fixtures",
            key=sha256,
            sha256=sha256,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        existing = self.objects.setdefault(ref.uri, capture.body)
        if existing != capture.body:
            raise ValueError("content-addressed raw object collision")
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        body = self.objects[ref.uri]
        if hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("raw object checksum mismatch")
        return body


@pytest.fixture(scope="module")
def e3_database_url():
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_d2_e3_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin_url = make_conninfo(**(parameters | {"dbname": "postgres"}))
    target_url = make_conninfo(**(parameters | {"dbname": database_name}))
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        for migration in (*sorted((REPOSITORY_ROOT / "db/migrations").glob("*.sql")), REPOSITORY_ROOT / "db/roles.sql"):
            completed = subprocess.run(
                ["psql", target_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                pytest.fail(completed.stdout + completed.stderr, pytrace=False)
        yield target_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))


@pytest.fixture
def connection(e3_database_url):
    active = psycopg.connect(e3_database_url, connect_timeout=3, autocommit=False)
    active.execute("begin")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _count(connection, table: str) -> int:
    if table not in {
        "raw.fetches",
        "staging.normalized_records",
        "staging.mvp_issuer_security_links",
        "staging.mvp_market_prices",
        "staging.mvp_security_listing_links",
        "staging.mvp_universe_memberships",
    }:
        raise ValueError(table)
    row = connection.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return cast(int, row[0])


def _evaluate_e3_manifest(evidence: D2E3Evidence, manifest: CaptureManifest):
    scope = evidence.capture_plan.scope
    return evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=evidence.capture_plan.applicability_mapping(),
        source_coverage=evidence.capture_plan.source_coverage_mapping(),
        evaluated_at=manifest.created_at,
    )


def _manifest_with_first_evidence_update(
    manifest: CaptureManifest,
    updates: dict[str, object],
) -> CaptureManifest:
    payload = manifest.model_dump(mode="python")
    payload["capture_manifest_id"] = ""
    payload["content_sha256"] = ""
    cells = list(payload["cells"])
    first_cell = dict(cells[0])
    first_cell["capture_cell_id"] = ""
    first_cell["content_sha256"] = ""
    evidence_rows = list(first_cell["evidence"])
    first_evidence = dict(evidence_rows[0])
    first_evidence["evidence_id"] = ""
    first_evidence["content_sha256"] = ""
    first_evidence.update(updates)
    evidence_rows[0] = first_evidence
    first_cell["evidence"] = evidence_rows
    cells[0] = first_cell
    payload["cells"] = cells
    return CaptureManifest.model_validate(payload)


def test_e3_dagster_run_is_exact_row_complete_and_idempotent(connection) -> None:
    connection.execute("set local time zone 'Asia/Singapore'")
    assert _count(connection, "staging.normalized_records") == 0
    definitions = build_d2_e3_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=D2E3Activation(environment="ci"),
    )
    dg.Definitions.validate_loadable(definitions)

    first_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    second_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    first = first_result.output_for_node(D2_E3_ASSET_NAME)
    second = second_result.output_for_node(D2_E3_ASSET_NAME)

    assert first_result.success and second_result.success
    assert first == second
    assert first.evidence_id == "d2-e3-evidence:d812369f2808942c8040a3d5f15e71ec7c147d7d547f988e312f428f15bf6139"
    assert first.accepted_e2_handoff_id == D2_E2_RUNTIME_HANDOFF_ID
    assert first.denominator.universe_id == "universe:topt-us-2026-03-31"
    assert first.denominator.accession == "000207169126012475"
    assert len({item.issuer_lei for item in first.denominator.instruments}) == 20
    assert len(first.denominator.instruments) == 21
    alphabet = {item.ticker: item for item in first.denominator.instruments if item.ticker in {"GOOG", "GOOGL"}}
    assert alphabet["GOOG"].issuer_lei == alphabet["GOOGL"].issuer_lei
    assert alphabet["GOOG"].cusip != alphabet["GOOGL"].cusip
    assert first.pre_knowable_rejected
    assert first.fixture_postgres_parity
    assert not first.release_allowed
    assert first.market_original_raw_sha256 == ("7a160dbd1a5816d0c31e20bd1f0e1ab8d2738e1fc744fc7bf96fa2903d19e038")
    assert first.market_changed_raw_sha256 != first.market_original_raw_sha256
    assert len(first.capture_plan.scope.requirements) == 4
    assert len(first.capture_plan.data_requirements) == 4
    assert len(first.capture_plan.cells) == 84
    assert len(first.capture_plan.applicability_mapping()) == 84
    assert len(first.capture_plan.source_coverage_mapping()) == 168
    assert {cell.instrument.id for cell in first.capture_plan.cells} == {
        item.security_id for item in first.denominator.instruments
    }
    assert all(
        cell.capture_demand.subject == e3_slice._capture_instrument_ref(cell.instrument)
        for cell in first.capture_plan.cells
    )
    assert len(first.capture_manifests) == 2
    assert len(first.capture_evaluations) == 2
    assert all(len(manifest.cells) == 84 for manifest in first.capture_manifests)
    assert all(report.ready and not report.blocking_reason_codes for report in first.capture_evaluations)
    requirement_by_id = first.capture_plan.scope.requirement_map()
    for manifest in first.capture_manifests:
        assert manifest.capture_scope_id == first.capture_plan.scope.capture_scope_id
        assert manifest.capture_scope_sha256 == first.capture_plan.scope.content_sha256
        assert {cell.key for cell in manifest.cells} == set(first.capture_plan.applicability_mapping())
        for cell in manifest.cells:
            assert cell.status == "complete"
            assert len(cell.evidence) == 1
            evidence = cell.evidence[0]
            requirement = requirement_by_id[cell.capture_requirement_id]
            assert evidence.raw_id is not None and evidence.raw_id.startswith("raw.fetches:")
            assert evidence.raw_sha256 is not None
            assert evidence.normalized_id is not None
            assert evidence.confidence is not None
            assert evidence.mapping_version is not None
            assert evidence.lineage_sha256 is not None
            assert evidence.quality_status is not None
            assert set(requirement.required_fields).issubset(evidence.populated_fields)
    assert len(first.normalized_record_ids) == 168
    assert first.universe_manifest.ref.content_sha256 == (
        "0320f5d4d6284eb6c72d476e47dca87911c61c79527d14824f839d94c621b129"
    )
    assert len(first.universe_manifest.membership_ids) == 21
    assert len(first.snapshots) == 2
    assert {snapshot.request.registry_snapshot_id for snapshot in first.snapshots} == {
        "registry-snapshot:7cda61357952971bf892aa9871c61ae6d39c08989b75247e33e50e911c212ef5"
    }
    assert all(len(snapshot.universe_memberships) == 21 for snapshot in first.snapshots)
    assert all(len(snapshot.normalized_records) == 84 for snapshot in first.snapshots)
    assert all(len(snapshot.selections) == 84 for snapshot in first.snapshots)
    assert all(len(row.cells) == 84 for row in first.row_manifests)
    market_fixture = load_topt_market_fixture(REPOSITORY_ROOT, first.denominator)
    source_retrieved_at = max(
        market_fixture.listing_source.retrieved_at,
        market_fixture.price_source.retrieved_at,
    )
    assert all(
        record.draft.knowable_at >= source_retrieved_at
        for snapshot in first.snapshots
        for record in snapshot.normalized_records
        if record.draft.semantic_type_id in {"semantic.market-price", "semantic.security-listing-link"}
    )
    expected_type_counts = {
        "semantic.issuer-security-link": 21,
        "semantic.market-price": 21,
        "semantic.security-listing-link": 21,
        "semantic.universe-membership": 21,
    }
    assert all(
        Counter(cell.demand.semantic_type_id for cell in row.cells) == expected_type_counts
        for row in first.row_manifests
    )
    implementation_sha256 = e3_slice.TOPT_SOURCE_IMPLEMENTATION_SHA256
    assert implementation_sha256 == "e0509b3bb93982bc0ed29776518612f1c470efd0095b2373eba0358032d8eac1"
    assert e3_slice._topt_source_code_sha256() == e3_slice.TOPT_SOURCE_CODE_SHA256
    expected_topt_sources = {
        "semantic.issuer-security-link": (
            "source-registry-entry:86641ce50d78bfc87d03cc0a4ab17230d04b1c3a7db964d10f39555e89d569ba"
        ),
        "semantic.market-price": "source-registry-entry:bf81b8356f25d7cda19b9037ecfe8617056cf9c84ec594ca054b604636f7a391",
        "semantic.security-listing-link": (
            "source-registry-entry:9a811571884d5434a14ec9f5a5ea24050a6c140e106871ffd9c057bac37777b3"
        ),
        "semantic.universe-membership": (
            "source-registry-entry:6d58ccbf2eec140b47126d11643bce8e8a8e4310ff26bc1bc2e6b858392f85d1"
        ),
    }
    records_by_type = {
        semantic_type_id: tuple(
            record
            for snapshot in first.snapshots
            for record in snapshot.normalized_records
            if record.draft.semantic_type_id == semantic_type_id
        )
        for semantic_type_id in expected_topt_sources
    }
    assert {
        semantic_type_id: tuple(sorted({record.source_registry_entry_id for record in records}))
        for semantic_type_id, records in records_by_type.items()
    } == {
        semantic_type_id: (source_registry_entry_id,)
        for semantic_type_id, source_registry_entry_id in expected_topt_sources.items()
    }
    for records in records_by_type.values():
        assert len(records) == 42
        assert {record.draft.producer_implementation_sha256 for record in records} == {implementation_sha256}
        assert {record.mapping_implementation_sha256 for record in records} == {implementation_sha256}
    persisted = connection.execute(
        """
        select semantic_type_id, count(*), count(raw_ref), count(confidence)
        from staging.normalized_records
        where normalized_record_id = any(%s)
        group by semantic_type_id
        order by semantic_type_id
        """,
        (list(first.normalized_record_ids),),
    ).fetchall()
    assert {
        semantic_type_id: (row_count, raw_lineage_count, confidence_count)
        for semantic_type_id, row_count, raw_lineage_count, confidence_count in persisted
    } == {semantic_type_id: (42, 42, 42) for semantic_type_id in expected_type_counts}
    assert connection.execute(
        """
        select count(distinct source_registry_entry_id)
        from staging.normalized_records
        where normalized_record_id = any(%s)
          and semantic_type_id = 'semantic.market-price'
        """,
        (list(first.normalized_record_ids),),
    ).fetchone() == (1,)
    assert first.row_manifests[0].expected_cell_ids == first.row_manifests[1].expected_cell_ids
    selected = [{cell.normalized_record_id for cell in row.cells} for row in first.row_manifests]
    assert selected[0].isdisjoint(selected[1])

    rebuilt_plan = e3_slice._build_e3_capture_plan(
        denominator=first.denominator,
        market_fixture=market_fixture,
        registry=first.snapshots[0].registry_snapshot,
        universe_manifest=first.universe_manifest,
        effective_at=first.capture_plan.scope.effective_at,
    )
    assert rebuilt_plan == first.capture_plan
    scope_repository = PostgresCaptureScopeRepository(connection)
    manifest_repository = PostgresCaptureManifestRepository(connection)
    evaluation_repository = PostgresCaptureEvaluationRepository(connection)
    assert scope_repository.get(first.capture_plan.scope.capture_scope_id) == first.capture_plan.scope
    for manifest in first.capture_manifests:
        assert manifest_repository.get(manifest.capture_manifest_id) == manifest
    for report in first.capture_evaluations:
        assert evaluation_repository.get(report.capture_evaluation_report_id) == report
    report_by_manifest = {report.capture_manifest_id: report for report in first.capture_evaluations}
    for manifest in first.capture_manifests:
        expected_report = report_by_manifest[manifest.capture_manifest_id]
        replayed_report = evaluate_capture_manifest(
            first.capture_plan.scope,
            manifest,
            applicability_catalog_id=first.capture_plan.scope.applicability_catalog_id,
            applicability_catalog_sha256=first.capture_plan.scope.applicability_catalog_sha256,
            applicability=rebuilt_plan.applicability_mapping(),
            source_coverage=rebuilt_plan.source_coverage_mapping(),
            evaluated_at=expected_report.evaluated_at,
        )
        assert replayed_report == expected_report

    assert _count(connection, "staging.normalized_records") == 381
    assert _count(connection, "staging.mvp_issuer_security_links") == 43
    assert _count(connection, "staging.mvp_market_prices") == 44
    assert _count(connection, "staging.mvp_security_listing_links") == 43
    assert _count(connection, "staging.mvp_universe_memberships") == 245
    assert connection.execute("show timezone").fetchone() == ("UTC",)

    row_payload = first.row_manifests[0].model_dump(mode="python")
    missing = deepcopy(row_payload)
    missing["cells"] = missing["cells"][:-1]
    with pytest.raises(ValidationError, match="at least 84 items"):
        D2E3RowCompleteManifest.model_validate(missing)
    duplicate = deepcopy(row_payload)
    duplicate["cells"] = list(duplicate["cells"])
    duplicate["cells"][-1] = duplicate["cells"][0]
    with pytest.raises(ValidationError, match="missing or duplicate required cells"):
        D2E3RowCompleteManifest.model_validate(duplicate)


def test_e3_standard_capture_contract_fails_closed_on_cell_drift(connection) -> None:
    evidence = run_d2_e3(
        REPOSITORY_ROOT,
        connection,
        MemoryRawObjectStore(),
        environment="ci",
    )
    manifest = evidence.capture_manifests[0]
    payload = manifest.model_dump(mode="python")

    missing = deepcopy(payload)
    missing["capture_manifest_id"] = ""
    missing["content_sha256"] = ""
    missing["cells"] = missing["cells"][:-1]
    missing_report = _evaluate_e3_manifest(evidence, CaptureManifest.model_validate(missing))
    assert not missing_report.ready
    assert any(reason.startswith("cell.missing:") for reason in missing_report.blocking_reason_codes)

    duplicate = deepcopy(payload)
    duplicate["capture_manifest_id"] = ""
    duplicate["content_sha256"] = ""
    duplicate["cells"] = list(duplicate["cells"])
    duplicate["cells"][-1] = duplicate["cells"][0]
    duplicate_report = _evaluate_e3_manifest(evidence, CaptureManifest.model_validate(duplicate))
    assert not duplicate_report.ready
    assert any(reason.startswith("cell.duplicate:") for reason in duplicate_report.blocking_reason_codes)

    extra = deepcopy(payload)
    extra["capture_manifest_id"] = ""
    extra["content_sha256"] = ""
    extra["cells"] = list(extra["cells"])
    extra_cell = deepcopy(extra["cells"][0])
    extra_cell["capture_cell_id"] = ""
    extra_cell["content_sha256"] = ""
    extra_cell["subject"] = {"kind": "security", "id": "security:cusip:extra"}
    extra["cells"].append(extra_cell)
    extra_report = _evaluate_e3_manifest(evidence, CaptureManifest.model_validate(extra))
    assert not extra_report.ready
    assert any(reason.startswith("cell.extra:") for reason in extra_report.blocking_reason_codes)

    shrunk_plan = evidence.capture_plan.model_dump(mode="python")
    shrunk_plan["capture_plan_id"] = ""
    shrunk_plan["content_sha256"] = ""
    shrunk_plan["cells"] = shrunk_plan["cells"][:-1]
    with pytest.raises(ValidationError, match="at least 84 items"):
        D2E3CapturePlan.model_validate(shrunk_plan)

    replacement_instrument = evidence.capture_plan.cells[0].instrument.model_copy(
        update={"id": "security:cusip:000000000"}
    )
    replacement_cell = evidence.capture_plan.cells[0].model_copy(update={"instrument": replacement_instrument})
    substituted_plan = evidence.capture_plan.model_copy(
        update={"cells": (replacement_cell, *evidence.capture_plan.cells[1:])}
    )
    substituted_evidence = evidence.model_dump(mode="python", exclude_computed_fields=True)
    substituted_evidence["evidence_id"] = ""
    substituted_evidence["content_sha256"] = ""
    substituted_evidence["capture_plan"] = substituted_plan
    with pytest.raises(ValidationError, match="retain 21 security instruments"):
        D2E3Evidence.model_validate(substituted_evidence)


def test_e3_local_and_ci_share_one_environment_neutral_capture_scope(connection) -> None:
    store = MemoryRawObjectStore()
    ci = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    local = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="local")

    assert local.capture_plan == ci.capture_plan
    assert local.capture_plan.scope.capture_scope_id == ci.capture_plan.scope.capture_scope_id
    assert {manifest.environment.value for manifest in ci.capture_manifests} == {"github_ci"}
    assert {manifest.environment.value for manifest in local.capture_manifests} == {"local_test"}
    assert {manifest.capture_manifest_id for manifest in ci.capture_manifests}.isdisjoint(
        manifest.capture_manifest_id for manifest in local.capture_manifests
    )
    assert all(report.ready for report in (*ci.capture_evaluations, *local.capture_evaluations))


def test_e3_standard_capture_contract_rejects_incomplete_or_tampered_evidence(connection) -> None:
    evidence = run_d2_e3(
        REPOSITORY_ROOT,
        connection,
        MemoryRawObjectStore(),
        environment="ci",
    )
    manifest = evidence.capture_manifests[0]
    wrong_environment = evidence.model_dump(mode="python", exclude_computed_fields=True)
    wrong_environment["evidence_id"] = ""
    wrong_environment["content_sha256"] = ""
    wrong_environment["environment"] = "local"
    with pytest.raises(ValidationError, match="capture manifest drifted from its predeclared scope"):
        D2E3Evidence.model_validate(wrong_environment)

    for applicability, status in (
        ("required", "missing"),
        ("optional", "optional"),
        ("not_applicable", "not_applicable"),
    ):
        status_payload = manifest.model_dump(mode="python")
        status_payload["capture_manifest_id"] = ""
        status_payload["content_sha256"] = ""
        status_payload["cells"] = list(status_payload["cells"])
        status_cell = dict(status_payload["cells"][0])
        status_cell["capture_cell_id"] = ""
        status_cell["content_sha256"] = ""
        status_cell["applicability"] = applicability
        status_cell["status"] = status
        status_cell["evidence"] = ()
        status_cell["reason_codes"] = ("producer-self-report",)
        status_payload["cells"][0] = status_cell
        status_report = _evaluate_e3_manifest(evidence, CaptureManifest.model_validate(status_payload))
        assert not status_report.ready
        assert any(reason.startswith("cell.required_not_complete:") for reason in status_report.blocking_reason_codes)

    scope = evidence.capture_plan.scope
    applicability = dict(evidence.capture_plan.applicability_mapping())
    first_key = next(iter(applicability))
    applicability[first_key] = ("required", manifest.started_at + timedelta(microseconds=1))
    postdated_report = evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=applicability,
        source_coverage=evidence.capture_plan.source_coverage_mapping(),
        evaluated_at=manifest.created_at,
    )
    assert not postdated_report.ready
    assert any(reason.startswith("applicability.postdated:") for reason in postdated_report.blocking_reason_codes)

    source_coverage = dict(evidence.capture_plan.source_coverage_mapping())
    source_coverage.pop((manifest.environment, *first_key))
    coverage_report = evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=evidence.capture_plan.applicability_mapping(),
        source_coverage=source_coverage,
        evaluated_at=manifest.created_at,
    )
    assert not coverage_report.ready
    assert any(reason.startswith("source_coverage.missing:") for reason in coverage_report.blocking_reason_codes)

    binding_payload = manifest.model_dump(mode="python")
    binding_payload["capture_manifest_id"] = ""
    binding_payload["content_sha256"] = ""
    binding_payload["research_catalog_id"] = "research-catalog:" + "0" * 64
    binding_payload["research_catalog_sha256"] = "0" * 64
    binding_report = _evaluate_e3_manifest(evidence, CaptureManifest.model_validate(binding_payload))
    assert not binding_report.ready
    assert "binding.research_catalog_id_mismatch" in binding_report.blocking_reason_codes
    assert "binding.research_catalog_sha256_mismatch" in binding_report.blocking_reason_codes

    cases = (
        ({"raw_id": None}, "evidence.missing_raw_id:"),
        ({"raw_sha256": None}, "evidence.missing_raw_sha256:"),
        ({"normalized_id": None}, "evidence.missing_normalized_id:"),
        ({"confidence": None}, "evidence.missing_confidence:"),
        ({"mapping_version": None}, "evidence.missing_mapping_version:"),
        ({"policy_versions": {}}, "evidence.missing_policy:"),
        ({"quality_check_ids": ()}, "evidence.missing_quality:"),
        ({"quality_status": None}, "evidence.missing_quality_status:"),
        ({"lineage_sha256": None}, "evidence.missing_lineage_sha256:"),
        ({"knowable_at": None}, "evidence.missing_knowable_at:"),
        ({"recorded_at": None}, "evidence.missing_recorded_at:"),
        ({"valid_from": None}, "evidence.missing_valid_from:"),
        ({"semantic_type_id": "semantic.financial-fact"}, "evidence.semantic_type_mismatch:"),
        ({"populated_fields": ()}, "evidence.required_fields_missing:"),
        (
            {"source_coverage_entry_id": "source-coverage-entry:" + "0" * 64},
            "evidence.unapproved_source_coverage_entry:",
        ),
        (
            {"knowable_at": manifest.as_of + timedelta(microseconds=1)},
            "evidence.future_knowledge:",
        ),
        ({"knowable_at": manifest.as_of - timedelta(days=3)}, "evidence.stale:"),
    )
    for updates, expected_reason in cases:
        tampered = _manifest_with_first_evidence_update(manifest, updates)
        report = _evaluate_e3_manifest(evidence, tampered)
        assert not report.ready
        assert any(reason.startswith(expected_reason) for reason in report.blocking_reason_codes)

    duplicate_lineage = manifest.model_dump(mode="python")
    duplicate_lineage["capture_manifest_id"] = ""
    duplicate_lineage["content_sha256"] = ""
    duplicate_lineage["cells"] = list(duplicate_lineage["cells"])
    duplicate_cell = dict(duplicate_lineage["cells"][0])
    duplicate_cell["capture_cell_id"] = ""
    duplicate_cell["content_sha256"] = ""
    duplicate_cell["evidence"] = [duplicate_cell["evidence"][0], duplicate_cell["evidence"][0]]
    duplicate_lineage["cells"][0] = duplicate_cell
    duplicate_report = _evaluate_e3_manifest(
        evidence,
        CaptureManifest.model_validate(duplicate_lineage),
    )
    assert not duplicate_report.ready
    assert any(
        reason.startswith("evidence.duplicate_lineage_edge:") for reason in duplicate_report.blocking_reason_codes
    )

    raw_conflict = manifest.model_dump(mode="python")
    raw_conflict["capture_manifest_id"] = ""
    raw_conflict["content_sha256"] = ""
    raw_conflict["cells"] = list(raw_conflict["cells"])
    conflict_cell = dict(raw_conflict["cells"][0])
    conflict_cell["capture_cell_id"] = ""
    conflict_cell["content_sha256"] = ""
    conflict_evidence = deepcopy(conflict_cell["evidence"][0])
    conflict_evidence["evidence_id"] = ""
    conflict_evidence["content_sha256"] = ""
    conflict_evidence["raw_sha256"] = "0" * 64
    conflict_cell["evidence"] = [conflict_cell["evidence"][0], conflict_evidence]
    raw_conflict["cells"][0] = conflict_cell
    conflict_report = _evaluate_e3_manifest(evidence, CaptureManifest.model_validate(raw_conflict))
    assert not conflict_report.ready
    assert any(reason.startswith("evidence.raw_checksum_conflict:") for reason in conflict_report.blocking_reason_codes)


def test_e3_denominator_rejects_shrink_duplicate_drift_and_float() -> None:
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    payload = denominator.model_dump(mode="python")

    shrink = deepcopy(payload)
    shrink["instruments"] = shrink["instruments"][:-1]
    shrink["selected_instrument_cusips"] = shrink["selected_instrument_cusips"][:-1]
    with pytest.raises(ValidationError, match="at least 21 items"):
        FrozenToptDenominator.model_validate(shrink)

    duplicate = deepcopy(payload)
    duplicate["instruments"] = list(duplicate["instruments"])
    duplicate["instruments"][-1] = duplicate["instruments"][0]
    with pytest.raises(ValidationError, match="21 distinct instruments"):
        FrozenToptDenominator.model_validate(duplicate)

    wrong_accession = deepcopy(payload)
    wrong_accession["accession"] = "000000000000000000"
    with pytest.raises(ValidationError, match="Input should be '000207169126012475'"):
        FrozenToptDenominator.model_validate(wrong_accession)

    float_weight = deepcopy(payload)
    float_weight["instruments"] = list(float_weight["instruments"])
    float_weight["instruments"][0]["filing_weight_percent"] = 1.5
    with pytest.raises(ValidationError, match="exact Decimal literals"):
        FrozenToptDenominator.model_validate(float_weight)

    schema_drift = deepcopy(payload)
    schema_drift["instruments"] = list(schema_drift["instruments"])
    schema_drift["instruments"][0]["vendor_symbol"] = "AAPL"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FrozenToptDenominator.model_validate(schema_drift)


def test_e3_market_fixture_rejects_shrink_duplicate_float_and_successor_leak() -> None:
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    fixture = load_topt_market_fixture(REPOSITORY_ROOT, denominator)
    payload = fixture.model_dump(mode="python")

    shrink = deepcopy(payload)
    shrink["rows"] = shrink["rows"][:-1]
    with pytest.raises(ValidationError, match="at least 21 items"):
        FrozenToptMarketFixture.model_validate(shrink)

    duplicate = deepcopy(payload)
    duplicate["rows"] = list(duplicate["rows"])
    duplicate["rows"][-1] = duplicate["rows"][0]
    with pytest.raises(ValidationError, match="incomplete or duplicated"):
        FrozenToptMarketFixture.model_validate(duplicate)

    binary_float = deepcopy(payload)
    binary_float["rows"] = list(binary_float["rows"])
    binary_float["rows"][0]["close"] = 1.5
    with pytest.raises(ValidationError, match="exact Decimal literals"):
        FrozenToptMarketFixture.model_validate(binary_float)

    successor_leak = deepcopy(payload)
    successor_leak["rows"] = list(successor_leak["rows"])
    xom = next(row for row in successor_leak["rows"] if row["ticker"] == "XOM")
    xom["issuer_cik_at_report_date"] = "0002115436"
    with pytest.raises(ValidationError, match="post-report XOM successor"):
        FrozenToptMarketFixture.model_validate(successor_leak)


def test_e3_market_routes_reject_corrupted_landed_bytes_and_bind_implementations(connection) -> None:
    store = MemoryRawObjectStore()
    e2_handoff = e3_slice.run_d2_e2(REPOSITORY_ROOT, connection, store, environment="ci")
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    fixture = load_topt_market_fixture(REPOSITORY_ROOT, denominator)
    registry = e3_slice._e3_registry(e2_handoff.registry_snapshot)
    catalog = e3_slice._e3_catalog(registry, denominator, fixture)
    implementation_sha256 = e3_slice.TOPT_SOURCE_IMPLEMENTATION_SHA256
    corrupt_body = b'{"schema_version": 1}'
    routes = (
        (e3_slice.TOPT_ISSUER_SOURCE_ID, "semantic.issuer-security-link"),
        (e3_slice.TOPT_LISTING_SOURCE_ID, "semantic.security-listing-link"),
        (e3_slice.TOPT_MEMBERSHIP_SOURCE_ID, "semantic.universe-membership"),
        (e3_slice.TOPT_PRICE_SOURCE_ID, "semantic.market-price"),
    )

    for fetch_id, (source_id, semantic_type_id) in enumerate(routes, start=1):
        source = catalog.source(source_id, "1.0.0")
        assert source.adapter_implementation_sha256 == implementation_sha256
        assert source.normalizer_implementation_sha256 == implementation_sha256
        capture = LandedMediumCapture(
            fetch_id=fetch_id,
            raw_ref=f"raw.fetches:{fetch_id}",
            raw_object_sha256=hashlib.sha256(corrupt_body).hexdigest(),
            source_id=source.source_id,
            source_version=source.version,
            source_registry_entry_id=source.source_registry_entry_id,
            source_registry_entry_sha256=source.content_sha256,
            semantic_type_ids=(semantic_type_id,),
            semantic_type_versions={semantic_type_id: "1.0.0"},
            source_record_id=f"corrupt:{source_id}",
            body=corrupt_body,
            content_type="application/json",
            source_published_at=fixture.price_source.retrieved_at,
            fetched_at=fixture.price_source.retrieved_at,
            recorded_at=fixture.price_source.retrieved_at,
        )
        with pytest.raises(ValueError, match="missing|Field required"):
            catalog.normalize(capture, semantic_type_id)


def test_e3_capture_scope_is_persisted_before_any_topt_landing(connection) -> None:
    class PlanObserved(RuntimeError):
        pass

    observed = False

    def stop_after_plan(point: e3_slice.FailurePoint) -> None:
        nonlocal observed
        if point != "after-capture-plan":
            return
        observed = True
        assert connection.execute(
            """
            select count(*)
            from staging.contract_objects
            where contract_kind = 'capture_scope'
              and payload ->> 'owner' = 'D2-mvp-medium-validation:E3'
            """
        ).fetchone() == (1,)
        assert connection.execute(
            "select count(*) from raw.fetches where source_record_id like 'd2-e3:%'"
        ).fetchone() == (0,)
        raise PlanObserved("capture plan observed before TOPT landing")

    with pytest.raises(PlanObserved, match="before TOPT landing"):
        run_d2_e3(
            REPOSITORY_ROOT,
            connection,
            MemoryRawObjectStore(),
            environment="ci",
            failure_injector=stop_after_plan,
        )
    assert observed
    assert _count(connection, "raw.fetches") == 0
    assert _count(connection, "staging.normalized_records") == 0


def test_e3_capture_manifest_rejects_corrupted_persisted_raw_bytes(connection) -> None:
    store = MemoryRawObjectStore()

    def corrupt_original_raw(point: e3_slice.FailurePoint) -> None:
        if point != "after-original-normalization":
            return
        uri = next(uri for uri in store.objects if uri.endswith(e3_slice.TOPT_MARKET_FIXTURE_SHA256))
        store.objects[uri] += b"\ncorrupt"

    with pytest.raises(ValueError, match="raw object checksum mismatch"):
        run_d2_e3(
            REPOSITORY_ROOT,
            connection,
            store,
            environment="ci",
            failure_injector=corrupt_original_raw,
        )
    assert _count(connection, "raw.fetches") == 0
    assert _count(connection, "staging.normalized_records") == 0


def test_e3_failure_rolls_back_and_retry_recovers_without_duplicates(connection) -> None:
    class InjectedFailure(RuntimeError):
        pass

    def fail_after_original(point: e3_slice.FailurePoint) -> None:
        if point == "after-original-vintage":
            raise InjectedFailure("simulated terminal interruption")

    store = MemoryRawObjectStore()
    with pytest.raises(InjectedFailure, match="terminal interruption"):
        run_d2_e3(
            REPOSITORY_ROOT,
            connection,
            store,
            environment="ci",
            failure_injector=fail_after_original,
        )
    assert _count(connection, "raw.fetches") == 0
    assert _count(connection, "staging.normalized_records") == 0

    recovered = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    repeated = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    assert recovered == repeated
    assert _count(connection, "staging.normalized_records") == 381


def test_e3_rows_remain_append_only(connection) -> None:
    # Keep the successful run inside the fixture-owned transaction so this
    # mutation test cannot contaminate later acceptance runs.
    assert _count(connection, "staging.normalized_records") == 0
    evidence = run_d2_e3(
        REPOSITORY_ROOT,
        connection,
        MemoryRawObjectStore(),
        environment="ci",
    )
    record_id = evidence.normalized_record_ids[0]
    with pytest.raises(psycopg.errors.RaiseException, match="point-in-time records are append-only"):
        with connection.transaction():
            connection.execute(
                "update staging.normalized_records set confidence = 0.5 where normalized_record_id = %s",
                (record_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="point-in-time records are append-only"):
        with connection.transaction():
            connection.execute(
                "delete from staging.normalized_records where normalized_record_id = %s",
                (record_id,),
            )


def test_e3_rejects_staging_release_and_wrong_handoff_activation(connection) -> None:
    with pytest.raises(ValidationError):
        D2E3Activation(environment="staging")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        D2E3Activation(
            environment="ci",
            expected_e2_handoff_id="mvp-medium-validation-handoff:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="explicit Local/CI activation"):
        build_d2_e3_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(ReleaseManifest, object()),
        )


def test_e3_terminal_governance_binds_the_complete_issue_23_matrix() -> None:
    manifest_path = REPOSITORY_ROOT / "governance/batches/D2-mvp-medium-validation.v1.json"
    evidence_path = REPOSITORY_ROOT / "governance/evidence/D2-mvp-medium-validation-E3.v1.json"
    manifest_bytes = manifest_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    evidence = json.loads(evidence_bytes)
    graph = json.loads((REPOSITORY_ROOT / "governance/vision-issue-graph.json").read_bytes())
    output = manifest["acceptance"]["output"]

    assert manifest["revision"] == 14
    assert manifest["status"] == "done"
    assert manifest["last_accepted_rung"] == manifest["target_rung"] == manifest["terminal_rung"] == "E3"
    assert manifest["capability_issues"] == manifest["closes_issues"] == [23]
    assert manifest["activation"]["base_sha"] == "1d6d59bb7708c1de3913dd91b6949d6697d2452a"

    assert output["type"] == "D2E3Evidence"
    assert output["stable_handoff"] is False
    assert output["evidence_id"] == ("d2-e3-evidence:d812369f2808942c8040a3d5f15e71ec7c147d7d547f988e312f428f15bf6139")
    assert output["sha256"] == output["evidence_id"].rsplit(":", 1)[-1]
    assert output["denominator"] == {
        "universe_id": "universe:topt-us-2026-03-31",
        "accession": "000207169126012475",
        "issuer_count": 20,
        "instrument_count": 21,
    }
    assert output["capture_contract"] == {
        "environment_neutral_scope": True,
        "required_cell_count_per_cutoff": 84,
        "source_coverage_count": 168,
        "capture_manifest_count": 2,
        "blocker_free_evaluation_count": 2,
        "normalized_record_count": 168,
        "snapshot_count": 2,
    }
    assert output["accepted_strata"] == [
        {
            "name": "existing-type-source-extension",
            "source_pr": 142,
            "producer_commit": "2ef14df27b6226e86faa775be8358ad30c123031",
        },
        {
            "name": "additive-typed-record",
            "source_pr": 146,
            "producer_commit": "a8b4bbc4f387d269df0dbcadb4d6115a065c2ea5",
        },
        {
            "name": "disabled-extension-output-replay",
            "source_pr": 149,
            "producer_commit": "4102d248aeb451e4d7153b956593952f4c5c4fe9",
        },
        {
            "name": "topt-four-domain-cells",
            "source_pr": 157,
            "producer_commit": "7251a14008397cd3daa7464a9fb0ceaa9d967109",
        },
        {
            "name": "predeclared-capture-contracts",
            "source_pr": 164,
            "producer_commit": "d7fc688dfd58c9c732272afe92c5bfebaa04796f",
        },
    ]

    assert output["rung_evidence"]["sha256"] == hashlib.sha256(evidence_bytes).hexdigest()
    assert evidence["accepted_rung"] == "E3"
    assert evidence["base_sha"] == evidence["producer_head_sha"] == "d7fc688dfd58c9c732272afe92c5bfebaa04796f"
    assert evidence["manifest_sha256"] == "14d1561023d613d4deea37ec72174dac202c8b95df3e45c8e7ad9799924f215a"
    assert [report["command"] for report in evidence["commands"]] == manifest["acceptance"]["commands"]
    assert evidence["negative_controls"] == manifest["acceptance"]["negative_controls"]
    evidence_content = {key: value for key, value in evidence.items() if key != "evidence_id"}
    assert evidence["evidence_id"] == (f"rung-evidence:D2-mvp-medium-validation:{canonical_sha256(evidence_content)}")

    graph_entry = graph["batches"]["D2-mvp-medium-validation"]
    assert graph_entry["status"] == "done"
    assert graph_entry["target_rung"] == "E3"
    assert graph_entry["sha256"] == hashlib.sha256(manifest_bytes).hexdigest()

    accepted_evidence = graph["issues"]["23"]["accepted_evidence"]
    assert accepted_evidence == {
        "path": "governance/evidence/issue-23.v1.json",
        "sha256": "9066cc06367a42ae92f4d69e008cc411ec008705edd52068861763056af98547",
    }
    capability_evidence_bytes = (REPOSITORY_ROOT / accepted_evidence["path"]).read_bytes()
    assert hashlib.sha256(capability_evidence_bytes).hexdigest() == accepted_evidence["sha256"]
    capability_evidence = json.loads(capability_evidence_bytes)
    assert capability_evidence["issue"] == 23
    assert capability_evidence["state"] == "accepted"
    assert capability_evidence["accepted_rung"] == "E3"
    assert capability_evidence["producer_commit"] == "68af3a6507bbb0a43905a1a1a63df3c41b6b996e"
    assert capability_evidence["source_pr"] == 167
