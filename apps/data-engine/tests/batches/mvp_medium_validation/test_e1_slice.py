from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e0_slice import (
    BATCH_MANIFEST_PATH,
    CORPUS_PATH,
    FrozenPriceAdapter,
    PriceComponentCatalog,
    PriceReconciliationEvidence,
    build_price_registry,
)
from data_engine.batches.mvp_medium_validation.e1_slice import (
    D2_E1_ASSET_NAME,
    D2E1Activation,
    D2E1Evidence,
    D2E1EvidenceRepository,
    _strict_decimal,
    build_d2_e1_definitions,
    load_e1_corpus,
    route_typed_payload,
    run_d2_e1,
    validate_schema_contract,
)
from data_engine.config import settings
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.release import ReleaseManifest

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
GRAPH_PATH = Path("governance/vision-issue-graph.json")


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


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_dagster_e1_is_idempotent_and_all_cases_pass(connection) -> None:
    definitions = build_d2_e1_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=D2E1Activation(environment="ci"),
    )
    dg.Definitions.validate_loadable(definitions)

    first = definitions.get_implicit_global_asset_job_def().execute_in_process()
    repeated = definitions.get_implicit_global_asset_job_def().execute_in_process()
    evidence = first.output_for_node(D2_E1_ASSET_NAME)
    repeated_evidence = repeated.output_for_node(D2_E1_ASSET_NAME)

    assert first.success and repeated.success
    assert evidence == repeated_evidence
    assert evidence.stable_handoff is False
    assert evidence.accepted_rung == "E1"
    assert len(evidence.cases) == 7
    assert all(case.passed for case in evidence.cases)
    assert D2E1EvidenceRepository(connection).get(evidence.evidence_id) == evidence


def test_e1_evidence_covers_exact_cross_domain_timing_matrix(connection) -> None:
    evidence = run_d2_e1(REPOSITORY_ROOT, connection, MemoryRawObjectStore(), environment="ci")
    cases = {case.case_id: case for case in evidence.cases}

    assert set(cases) == {
        "d0-cross-domain-regression",
        "e0-price-changed-vintage",
        "jpm-dividend-lifecycle",
        "nvda-split-lifecycle",
        "plug-financial-filing-restatement",
        "qqq-membership-vintages",
        "schema-registry-provenance",
    }
    assert "raw-row-reparsed" in cases["e0-price-changed-vintage"].assertion_ids
    assert "interrupted-transaction-rolled-back" in cases["e0-price-changed-vintage"].assertion_ids
    assert "retry-and-repeat-idempotent" in cases["e0-price-changed-vintage"].assertion_ids
    assert "reordered-and-repeated-idempotency" in cases["d0-cross-domain-regression"].assertion_ids
    assert "at-amendment-restated" in cases["plug-financial-filing-restatement"].assertion_ids
    assert "each-phase-exactly-once" in cases["nvda-split-lifecycle"].assertion_ids
    assert "missing-pay-rejected" in cases["jpm-dividend-lifecycle"].assertion_ids
    assert "addition-and-removal-replayed" in cases["qqq-membership-vintages"].assertion_ids
    assert "unknown-payload-type-rejected" in cases["schema-registry-provenance"].assertion_ids
    assert cases["schema-registry-provenance"].blocker_codes == ("e2.shared-multidomain-registry-path-required",)
    assert len(evidence.factor_projection_sha256s) == 5
    assert len(set(evidence.factor_projection_sha256s)) == 5


def test_price_normalizer_rejects_payload_not_parsed_from_changed_bytes() -> None:
    case = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci")
    original = case.artifact
    changed_row = original.source_row.replace("174.39999389648438", "174.50000000000000")
    body = original.body.replace(original.source_row.encode(), changed_row.encode())
    sha256 = hashlib.sha256(body).hexdigest()
    invalid = replace(
        original,
        artifact_id="invalid-unparsed-vintage",
        source_record_id="fixture:yahoo:NVDA:invalid-unparsed-vintage",
        body=body,
        sha256=sha256,
        source_row=changed_row,
        reconciliation=PriceReconciliationEvidence(
            **original.reconciliation.model_dump(
                mode="python",
                exclude={"raw_object_id", "raw_object_sha256"},
            ),
            raw_object_id=f"raw-object:{sha256}",
            raw_object_sha256=sha256,
        ),
    )

    registry = build_price_registry()
    _adapter, normalizer, source, semantic_type = PriceComponentCatalog.e0().resolve(registry)
    with pytest.raises(ValueError, match="payload values do not match"):
        normalizer.normalize(case, invalid, 1, sha256, source, semantic_type)


def test_schema_and_decimal_guards_fail_closed() -> None:
    contract = load_e1_corpus(REPOSITORY_ROOT).schema_contract
    drifted = {**contract, "corporate-action": "0" * 64}
    price = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci").artifact.payload

    with pytest.raises(ValueError, match="schema contract drifted"):
        validate_schema_contract(drifted)
    assert route_typed_payload("market-price", price.model_dump(mode="json")) == price
    with pytest.raises(ValueError, match="unknown D2 E1 semantic type"):
        route_typed_payload("unknown-semantic-type", {})
    with pytest.raises(ValueError, match="binary float"):
        _strict_decimal(1.4, label="cash amount")
    assert _strict_decimal("1.40", label="cash amount") == Decimal("1.40")


def test_e1_evidence_is_content_addressed_and_append_only(connection) -> None:
    evidence = run_d2_e1(REPOSITORY_ROOT, connection, MemoryRawObjectStore(), environment="ci")
    repository = D2E1EvidenceRepository(connection)

    assert repository.put(evidence) is False
    with pytest.raises(ValidationError, match="content_sha256"):
        D2E1Evidence.model_validate({**evidence.model_dump(mode="json"), "content_sha256": "0" * 64})
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute(
            "update d2_mvp_medium_validation_e1_evidence set content_sha256 = %s where evidence_id = %s",
            ("0" * 64, evidence.evidence_id),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
        connection.execute("delete from d2_mvp_medium_validation_e1_evidence")


def test_release_and_staging_activation_are_rejected(connection) -> None:
    with pytest.raises(ValidationError):
        D2E1Activation(environment="staging")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="batch-private"):
        build_d2_e1_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(Any, ReleaseManifest.model_construct()),
        )


def test_e1_manifest_corpus_and_graph_transition_are_exact() -> None:
    manifest_bytes = (REPOSITORY_ROOT / BATCH_MANIFEST_PATH).read_bytes()
    manifest = json.loads(manifest_bytes)
    corpus_bytes = (REPOSITORY_ROOT / CORPUS_PATH).read_bytes()
    corpus = json.loads(corpus_bytes)
    graph = json.loads((REPOSITORY_ROOT / GRAPH_PATH).read_bytes())
    graph_entry = graph["batches"]["D2-mvp-medium-validation"]
    lease_path = REPOSITORY_ROOT / "governance/leases/D2-mvp-medium-validation.v1.json"
    lease_bytes = lease_path.read_bytes()
    lease = json.loads(lease_bytes)

    assert manifest["revision"] == 8
    assert manifest["activation"]["base_sha"] == "8aa3fb3218fec2fb60095c248b8de2a364b0efd7"
    assert manifest["last_accepted_rung"] == "E3"
    assert manifest["target_rung"] == "E3"
    assert manifest["terminal_rung"] == "E3"
    assert manifest["corpus"]["sha256"] == hashlib.sha256(corpus_bytes).hexdigest()
    assert corpus["rung_scope"]["frozen_target_rung"] == "E1"
    assert corpus["e1_evidence"]["stable_handoff"] is False
    assert graph_entry["status"] == "done"
    assert graph_entry["target_rung"] == "E3"
    assert graph_entry["sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert manifest["paths"]["lease_manifest"] == {
        "path": "governance/leases/D2-mvp-medium-validation.v1.json",
        "sha256": hashlib.sha256(lease_bytes).hexdigest(),
    }
    assert lease["lease_id"] == ("integration-lease:bc129504d02f61b4e48f23dc069742b42ec6831fc50eae494031a1e8bd86e4fb")
    assert lease["base_sha"] == "45ff014e6881198b6d078e33f080d2a09adb18e0"
