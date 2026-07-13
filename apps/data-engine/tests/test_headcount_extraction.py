from __future__ import annotations

import hashlib
import inspect
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.headcount_assets import (
    H0_E1_ASSET_NAME,
    H0_E2_ASSET_NAME,
    CoreHeadcountExtractionHandoff,
    H0E1Activation,
    H0E2RunnerResource,
    H0HandoffActivation,
    build_h0_e1_definitions,
    build_h0_e2_definitions,
    run_h0_e2,
)
from data_engine.headcount_models import (
    D1_RUNTIME_HANDOFF_ID,
    D1_RUNTIME_HANDOFF_SHA256,
    HEADCOUNT_CORPUS_SHA256,
    EvidenceSpan,
    HeadcountAvailability,
    HeadcountCandidate,
    HeadcountPayload,
    HeadcountReviewStatus,
    HeadcountScope,
    build_fixture_extraction_identity,
    build_fixture_headcount_extraction,
    visible_document_text,
)
from data_engine.headcount_pipeline import (
    FrozenHeadcountArtifact,
    FrozenHeadcountCase,
    HeadcountFixtureExtractor,
    build_e1_fixture_extraction_identity,
    load_headcount_corpus,
    replay_headcount_e1,
    run_headcount_e1,
    run_headcount_variant,
)
from data_engine.headcount_registry import (
    HEADCOUNT_CORPUS_SOURCE_ID,
    build_headcount_registry,
)
from data_engine.headcount_repository import PostgresHeadcountRepository
from data_engine.mvp_assets import D1HandoffActivation, run_d1_e2
from data_engine.mvp_registry import build_filing_registry
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.raw_store import get_payload
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import ExtractionTemplate, ModelRevisionRef

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
CORPUS_PATH = REPOSITORY_ROOT / "apps/data-engine/tests/fixtures/headcount_extraction/corpus.v1.json"


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        content_sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d1-fixtures",
            key=content_sha256,
            sha256=content_sha256,
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


class ExplodingHeadcountExtractor:
    def extract(
        self,
        case: FrozenHeadcountCase,
        artifact: FrozenHeadcountArtifact,
        document_record,
        raw_body: bytes,
    ) -> HeadcountPayload:
        del case, artifact, document_record, raw_body
        raise AssertionError("stored replay called the fixture extractor")


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    with active:
        yield active


def _corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _d1_input(connection):
    store = MemoryRawObjectStore()
    handoff = run_d1_e2(REPOSITORY_ROOT, connection, store)
    activation = D1HandoffActivation(
        consumer="H0-core-headcount-extraction",
        environment="local",
        expected_handoff_id=D1_RUNTIME_HANDOFF_ID,
        expected_handoff_sha256=D1_RUNTIME_HANDOFF_SHA256,
    )
    record = handoff.snapshot.normalized_records[0]
    payload = PostgresFilingDocumentRepository(connection).payload_for(record.normalized_record_id)
    row = connection.execute(
        "select raw_ref from staging.normalized_records where normalized_record_id = %s",
        (record.normalized_record_id,),
    ).fetchone()
    assert row is not None
    raw_ref = row[0]
    body = get_payload(connection, int(raw_ref.split(":", 1)[1]), store=store)
    return handoff, activation, record, payload, body, raw_ref


def _plug_payload(document_record, body: bytes) -> HeadcountPayload:
    case = next(item for item in _corpus()["cases"] if item["case_id"] == "d1-selected-plug-total")
    expected = case["expected"]
    document = visible_document_text(body)
    span = EvidenceSpan.locate(
        document_id=document_record.document_id,
        document=document,
        text=expected["evidence_spans"][0]["text"],
    )
    selected = HeadcountCandidate(
        value=int(expected["selected_value"]),
        scope=HeadcountScope.TOTAL,
        valid_period_end=expected["valid_period_end"],
        evidence_spans=(span,),
    )
    partial = tuple(
        HeadcountCandidate(
            value=int(candidate["value"]),
            scope=HeadcountScope(candidate["scope"]),
            valid_period_end=expected["valid_period_end"],
            evidence_spans=(span,),
        )
        for candidate in expected["rejected_candidates"]
    )
    return HeadcountPayload(
        availability=HeadcountAvailability.AVAILABLE,
        valid_period_end=expected["valid_period_end"],
        selected=selected,
        candidates=(selected, *partial),
        confidence=expected["confidence"],
        review_status=HeadcountReviewStatus.REVIEWED_FIXTURE,
    )


def _bundle(connection):
    handoff, activation, record, document_payload, body, raw_ref = _d1_input(connection)
    started_at = record.recorded_at + timedelta(seconds=1)
    return build_fixture_headcount_extraction(
        handoff=handoff,
        activation=activation,
        document_record=record,
        document_payload=document_payload,
        raw_body=body,
        raw_ref=raw_ref,
        payload=_plug_payload(record, body),
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=1),
    )


def test_frozen_corpus_and_exact_visible_span_match_checked_in_bytes() -> None:
    corpus_bytes = CORPUS_PATH.read_bytes()
    assert hashlib.sha256(corpus_bytes).hexdigest() == HEADCOUNT_CORPUS_SHA256
    corpus = json.loads(corpus_bytes)
    artifacts = {item["artifact_id"]: item for item in corpus["artifacts"]}
    artifact = artifacts["plug-amended-filing"]
    body = (REPOSITORY_ROOT / artifact["path"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == artifact["sha256"]
    document = visible_document_text(body)
    evidence_text = next(item for item in corpus["cases"] if item["case_id"] == "d1-selected-plug-total")["expected"][
        "evidence_spans"
    ][0]["text"]
    span = EvidenceSpan.locate(
        document_id=f"document:{artifact['accession']}",
        document=document,
        text=evidence_text,
    )
    assert document.text[span.start_char : span.end_char] == evidence_text
    assert span.document_sha256 == artifact["sha256"]


def test_selection_contract_rejects_partial_missing_evidence_and_confidence() -> None:
    document = visible_document_text(b"<p>As of 2025, we had 12 employees.</p>")
    span = EvidenceSpan.locate(document_id="document:test", document=document, text="we had 12 employees.")
    partial = HeadcountCandidate(
        value=12,
        scope=HeadcountScope.DEPARTMENTAL,
        valid_period_end="2025-12-31",
        evidence_spans=(span,),
    )
    with pytest.raises(ValidationError, match="total-employee"):
        HeadcountPayload(
            availability="available",
            valid_period_end="2025-12-31",
            selected=partial,
            candidates=(partial,),
            confidence="0.9",
            review_status="reviewed-fixture",
        )
    with pytest.raises(ValidationError, match="at least 1 item"):
        HeadcountCandidate(
            value=12,
            scope="total",
            valid_period_end="2025-12-31",
            evidence_spans=(),
        )
    values = {
        "availability": "available",
        "valid_period_end": "2025-12-31",
        "selected": partial.model_copy(update={"scope": HeadcountScope.TOTAL}).model_dump(mode="json"),
        "candidates": [partial.model_copy(update={"scope": HeadcountScope.TOTAL}).model_dump(mode="json")],
        "review_status": "reviewed-fixture",
    }
    with pytest.raises(ValidationError, match="confidence"):
        HeadcountPayload.model_validate(values)


def test_extraction_identity_rejects_mutable_or_unbound_model_state() -> None:
    with pytest.raises(ValidationError, match="must be immutable"):
        ModelRevisionRef(
            provider="fixture",
            model_id="headcount-golden",
            immutable_revision="latest",
            endpoint_or_artifact_sha256="a" * 64,
            decoding_parameters_sha256="b" * 64,
        )
    model_revision, template = build_fixture_extraction_identity()
    with pytest.raises(ValidationError, match="model-revision ID and hash do not match"):
        ExtractionTemplate(
            **template.model_dump(
                mode="python",
                exclude={"extraction_template_id", "content_sha256", "model_revision_sha256"},
            ),
            model_revision_sha256="0" * 64,
        )
    assert model_revision.decoding_parameters_sha256 == canonical_sha256(
        {"temperature": "0", "top_p": "1", "response_mode": "frozen-fixture"}
    )


def test_e0_consumes_exact_d1_handoff_and_replays_stored_result(connection) -> None:
    bundle = _bundle(connection)
    assert bundle.record.draft.extraction_invocation_id == bundle.invocation.extraction_invocation_id
    assert bundle.record.document_id.startswith("document:")
    repository = PostgresHeadcountRepository(connection)
    repository.put(bundle)
    assert repository.put(bundle) is False

    replayed = repository.load(
        bundle.invocation.extraction_invocation_id,
        model_revision=bundle.model_revision,
        template=bundle.template,
    )
    assert replayed == bundle
    assert "extractor" not in inspect.signature(repository.load).parameters
    assert connection.execute(
        "select count(*) from staging.headcount_facts where normalized_record_id = %s",
        (bundle.record.normalized_record_id,),
    ).fetchone() == (1,)

    factor_input = replayed.factor_input(as_of=bundle.record.recorded_at)
    assert factor_input.value == 1285
    assert factor_input.fiscal_period == "2020-12-31"
    assert set(factor_input.model_dump()) == {
        "entity_id",
        "metric",
        "value",
        "confidence",
        "as_of",
        "fiscal_period",
    }
    assert not set(factor_input.model_dump()) & {
        "source",
        "raw_ref",
        "document_id",
        "model_revision_id",
        "extraction_invocation_id",
        "evidence_spans",
        "review_status",
    }


def test_wrong_handoff_future_execution_and_raw_tamper_fail(connection) -> None:
    handoff, activation, record, document_payload, body, raw_ref = _d1_input(connection)
    payload = _plug_payload(record, body)
    wrong_activation = D1HandoffActivation(
        consumer="H0-core-headcount-extraction",
        environment="local",
        expected_handoff_id="mvp-normalization-handoff:" + "0" * 64,
        expected_handoff_sha256="0" * 64,
    )
    started_at = record.recorded_at + timedelta(seconds=1)
    with pytest.raises(ValueError, match="does not authorize H0"):
        build_fixture_headcount_extraction(
            handoff=handoff,
            activation=wrong_activation,
            document_record=record,
            document_payload=document_payload,
            raw_body=body,
            raw_ref=raw_ref,
            payload=payload,
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="cannot start before"):
        build_fixture_headcount_extraction(
            handoff=handoff,
            activation=activation,
            document_record=record,
            document_payload=document_payload,
            raw_body=body,
            raw_ref=raw_ref,
            payload=payload,
            started_at=record.recorded_at - timedelta(microseconds=1),
            completed_at=record.recorded_at,
        )
    with pytest.raises(ValueError, match="raw bytes do not match"):
        build_fixture_headcount_extraction(
            handoff=handoff,
            activation=activation,
            document_record=record,
            document_payload=document_payload,
            raw_body=body + b"tamper",
            raw_ref=raw_ref,
            payload=payload,
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
        )


def test_span_tamper_and_wrong_replay_chain_fail(connection) -> None:
    handoff, activation, record, document_payload, body, raw_ref = _d1_input(connection)
    payload = _plug_payload(record, body)
    selected = payload.selected
    assert selected is not None
    span = selected.evidence_spans[0]
    with pytest.raises(ValidationError, match="offsets do not match"):
        EvidenceSpan(
            **span.model_dump(mode="python", exclude={"evidence_span_id", "content_sha256", "end_char"}),
            end_char=span.end_char - 1,
        )

    bundle = _bundle(connection)
    repository = PostgresHeadcountRepository(connection)
    repository.put(bundle)
    wrong_model = ModelRevisionRef(
        provider="fixture",
        model_id="headcount-golden",
        immutable_revision="2026-07-13.v2",
        endpoint_or_artifact_sha256=HEADCOUNT_CORPUS_SHA256,
        decoding_parameters_sha256=bundle.model_revision.decoding_parameters_sha256,
    )
    with pytest.raises(ValidationError, match="does not bind"):
        repository.load(
            bundle.invocation.extraction_invocation_id,
            model_revision=wrong_model,
            template=bundle.template,
        )


def test_headcount_tables_and_normalized_vintages_are_append_only(connection) -> None:
    bundle = _bundle(connection)
    PostgresHeadcountRepository(connection).put(bundle)
    statements = (
        (
            "update staging.headcount_extraction_invocations set recorded_at = recorded_at "
            "where extraction_invocation_id = %s",
            bundle.invocation.extraction_invocation_id,
        ),
        (
            "delete from staging.headcount_extraction_invocations where extraction_invocation_id = %s",
            bundle.invocation.extraction_invocation_id,
        ),
        (
            "update staging.headcount_facts set confidence = confidence where normalized_record_id = %s",
            bundle.record.normalized_record_id,
        ),
        (
            "delete from staging.headcount_facts where normalized_record_id = %s",
            bundle.record.normalized_record_id,
        ),
        (
            "update staging.normalized_records set confidence = confidence where normalized_record_id = %s",
            bundle.record.normalized_record_id,
        ),
        (
            "delete from staging.normalized_records where normalized_record_id = %s",
            bundle.record.normalized_record_id,
        ),
    )
    for statement, identity in statements:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(statement, (identity,))


def test_headcount_migration_exposes_required_constraints(connection) -> None:
    assert connection.execute(
        "select to_regclass('staging.headcount_extraction_invocations'), to_regclass('staging.headcount_facts')"
    ).fetchone() == ("staging.headcount_extraction_invocations", "staging.headcount_facts")
    assert connection.execute(
        """
        select count(*)
        from pg_trigger
        where not tgisinternal
          and tgname in (
              'trg_headcount_invocations_validate',
              'trg_headcount_invocations_append_only',
              'trg_headcount_facts_validate',
              'trg_headcount_facts_append_only'
          )
        """
    ).fetchone() == (4,)
    assert connection.execute(
        "select to_regclass('staging.idx_headcount_invocation_document'), to_regclass('staging.idx_headcount_facts_pit')"
    ).fetchone() == ("staging.idx_headcount_invocation_document", "staging.idx_headcount_facts_pit")


def test_unavailable_payload_cannot_claim_a_selected_value() -> None:
    unavailable = HeadcountPayload(
        availability="unavailable",
        valid_period_end="2024-05-22",
        selected=None,
        candidates=(),
        confidence="0",
        review_status="reviewed-fixture",
        reason="no-total-headcount-disclosure",
    )
    assert unavailable.selected is None
    with pytest.raises(ValidationError, match="requires a reason and no selected value"):
        HeadcountPayload(
            availability="unavailable",
            valid_period_end="2024-05-22",
            selected=HeadcountCandidate(
                value=1,
                scope="total",
                valid_period_end="2024-05-22",
                evidence_spans=(
                    EvidenceSpan.locate(
                        document_id="document:test",
                        document=visible_document_text(b"<p>1 employee</p>"),
                        text="1 employee",
                    ),
                ),
            ),
            candidates=(),
            confidence="0",
            review_status="reviewed-fixture",
            reason="no-total-headcount-disclosure",
        )


def test_e1_corpus_and_registry_extend_the_exact_d1_parent(connection) -> None:
    corpus = load_headcount_corpus(REPOSITORY_ROOT)
    assert corpus.producer_handoff_id.startswith("handoff:d1-mvp-normalization-handoff:")
    assert {artifact.artifact_id for artifact in corpus.artifacts} == {
        "plug-original-filing",
        "plug-amended-filing",
        "ddog-2025-filing",
        "nice-2025-filing",
        "jpm-2025-filing",
        "nvda-guidance-without-headcount",
    }
    assert {case.case_id for case in corpus.cases} == {
        "d1-selected-plug-total",
        "ddog-total-versus-departments",
        "nice-worldwide-total-with-contractors",
        "missing-total-headcount",
        "jpm-financial-issuer-branch-input",
        "d1-pit-restatement-replay",
    }
    assert all(hashlib.sha256(artifact.body).hexdigest() == artifact.sha256 for artifact in corpus.artifacts)

    handoff = run_d1_e2(REPOSITORY_ROOT, connection, MemoryRawObjectStore())
    d1_registry = handoff.snapshot.registry_snapshot
    registry = build_headcount_registry(d1_registry)
    assert registry.parent_snapshot_id == d1_registry.registry_snapshot_id
    assert set(d1_registry.sources) < set(registry.sources)
    assert set(d1_registry.semantic_types) < set(registry.semantic_types)
    assert next(source for source in registry.sources if source.source_id == HEADCOUNT_CORPUS_SOURCE_ID)
    headcount_type = next(
        semantic_type
        for semantic_type in registry.semantic_types
        if semantic_type.semantic_type_id == "semantic.employee-headcount"
    )
    assert headcount_type.domain.value == "filing_extractions"
    assert headcount_type.input_model_key == "factors:Fact"


def test_e1_multi_case_pit_and_stored_replay_without_calls(connection) -> None:
    evidence = run_headcount_e1(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        environment="local",
    )
    results = {result.case_id: result for result in evidence.case_results}
    assert results["d1-selected-plug-total"].selected_value == 1285
    assert results["ddog-total-versus-departments"].selected_value == 8100
    assert results["nice-worldwide-total-with-contractors"].selected_value == 9626
    assert results["missing-total-headcount"].availability is HeadcountAvailability.UNAVAILABLE
    assert results["missing-total-headcount"].selected_value is None
    assert results["jpm-financial-issuer-branch-input"].selected_value == 318512
    assert results["jpm-financial-issuer-branch-input"].branch_input == "financial-issuer"
    assert results["jpm-financial-issuer-branch-input"].factor_policy == "not-decided-by-H0"
    assert evidence.pit_selection_ids[0] != evidence.pit_selection_ids[1]
    assert evidence.pit_selection_ids[1] == evidence.pit_selection_ids[2]
    assert evidence.stable_handoff is False
    assert evidence.live_source_calls is False
    assert evidence.live_model_calls is False

    replayed = replay_headcount_e1(connection, evidence)
    assert {bundle.invocation.extraction_invocation_id for bundle in replayed} == set(evidence.invocation_ids)
    assert {bundle.record.normalized_record_id for bundle in replayed} == {
        *evidence.headcount_vintage_ids,
        *(
            result.normalized_record_id
            for result in evidence.case_results
            if result.case_id != "d1-selected-plug-total"
        ),
    }
    assert set(inspect.signature(replay_headcount_e1).parameters) == {"connection", "evidence"}

    by_subject = {bundle.record.draft.subject.id: bundle for bundle in replayed}
    ddog = by_subject["issuer.ddog"]
    assert {(candidate.value, candidate.scope) for candidate in ddog.payload.candidates} == {
        (8100, HeadcountScope.TOTAL),
        (3600, HeadcountScope.DEPARTMENTAL),
        (3900, HeadcountScope.DEPARTMENTAL),
    }
    nice = by_subject["issuer.nice"]
    assert {(candidate.value, candidate.scope) for candidate in nice.payload.candidates} == {
        (9626, HeadcountScope.TOTAL),
        (84, HeadcountScope.TEMPORARY),
        (2174, HeadcountScope.CONTRACTOR),
    }
    jpm_factor_input = by_subject["issuer.jpm"].factor_input(as_of=by_subject["issuer.jpm"].record.recorded_at)
    assert jpm_factor_input.value == 318512
    assert set(jpm_factor_input.model_dump()) == {
        "entity_id",
        "metric",
        "value",
        "confidence",
        "as_of",
        "fiscal_period",
    }

    original_id, amended_id = evidence.headcount_vintage_ids
    rows = connection.execute(
        """
        select normalized_record_id, is_restatement, supersedes_record_id
        from staging.normalized_records
        where normalized_record_id = any(%s)
        """,
        ([original_id, amended_id],),
    ).fetchall()
    restatement = {row[0]: (row[1], row[2]) for row in rows}
    assert restatement[original_id] == (False, None)
    assert restatement[amended_id] == (True, original_id)

    repeated = run_headcount_e1(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        environment="local",
        extractor=cast(HeadcountFixtureExtractor, ExplodingHeadcountExtractor()),
    )
    assert repeated == evidence


def test_e1_identity_and_document_changes_append_new_vintages(connection) -> None:
    evidence = run_headcount_e1(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        environment="local",
    )
    assert len(set(evidence.document_vintage_ids)) == 2
    assert len(set(evidence.headcount_vintage_ids)) == 2

    identities = (
        build_fixture_extraction_identity(
            immutable_revision="2026-07-13.v2",
            template_version="0.1.1",
        ),
        build_fixture_extraction_identity(
            template_version="0.1.1",
            instructions="Select only the reviewed company-wide employee total; never promote a partial count.",
        ),
        build_fixture_extraction_identity(
            template_version="0.1.1",
            output_schema_sha256=canonical_sha256(
                {"schema": HeadcountPayload.model_json_schema(), "revision": "e1-variant"}
            ),
        ),
        build_fixture_extraction_identity(
            template_version="0.1.1",
            decoding_parameters={"temperature": "0", "top_p": "0.9", "response_mode": "frozen-fixture"},
        ),
    )
    store = MemoryRawObjectStore()
    variants = tuple(
        run_headcount_variant(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=store,
            environment="local",
            case_id="ddog-total-versus-departments",
            model_revision=model_revision,
            template=template,
        )
        for model_revision, template in identities
    )
    assert len({bundle.invocation.extraction_invocation_id for bundle in variants}) == 4
    assert len({bundle.record.normalized_record_id for bundle in variants}) == 4
    assert not {bundle.invocation.extraction_invocation_id for bundle in variants} & set(evidence.invocation_ids)
    persisted = connection.execute(
        "select count(*) from staging.headcount_facts where normalized_record_id = any(%s)",
        ([bundle.record.normalized_record_id for bundle in variants],),
    ).fetchone()
    assert persisted == (4,)

    repeated = run_headcount_variant(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        environment="local",
        case_id="ddog-total-versus-departments",
        model_revision=identities[0][0],
        template=identities[0][1],
    )
    assert repeated == variants[0]


def test_e1_dagster_composition_is_explicit_local_ci_only(connection) -> None:
    import data_engine.headcount_assets as headcount_assets

    definitions = build_h0_e1_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=H0E1Activation(environment="local"),
    )
    dg.Definitions.validate_loadable(definitions)
    assert not definitions.schedules
    assert not definitions.sensors
    assert not hasattr(headcount_assets, "defs")

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    evidence = result.output_for_node(H0_E1_ASSET_NAME)
    assert result.success
    assert evidence.environment == "local"
    assert evidence.stable_handoff is False
    assert H0E1Activation(environment="ci").environment == "ci"
    with pytest.raises(ValidationError, match="local|ci"):
        H0E1Activation(environment="staging")
    with pytest.raises(ValidationError, match="False"):
        H0E1Activation(environment="local", live_model_allowed=True)
    with pytest.raises(ValueError, match="cannot be activated by a release"):
        build_h0_e1_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(Any, object()),
        )


def test_e2_handoff_is_content_addressed_and_provenance_free(connection) -> None:
    handoff = run_h0_e2(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    repeated = run_h0_e2(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    assert repeated == handoff
    assert handoff.handoff_id == f"core-headcount-extraction-handoff:{handoff.content_sha256}"
    assert handoff.stable_handoff is True
    assert handoff.readiness_ceiling == "local-ci-contract-only"
    assert handoff.semantic_policy_state == "provisional-pending-issue-59"
    assert handoff.model_source_policy_state == "fixture-only-pending-issue-60"
    assert handoff.e1_evidence.environment == "ci"
    assert handoff.live_source_calls is False
    assert handoff.live_model_calls is False
    assert handoff.staging_activation is False
    assert handoff.release_activation is False
    assert len(handoff.normalized_record_ids) == 6
    assert len(handoff.selected_record_ids) == 5
    assert handoff.unavailable_subject_ids == ("issuer.nvda",)

    registry = build_headcount_registry(build_filing_registry())
    assert handoff.registry_snapshot == registry
    model_revision, template = build_e1_fixture_extraction_identity()
    assert handoff.model_revision == model_revision
    assert handoff.extraction_template == template
    migration = REPOSITORY_ROOT / "db/migrations/0020_headcount_extraction.sql"
    assert handoff.migration_sha256 == hashlib.sha256(migration.read_bytes()).hexdigest()

    factor_inputs = {value.entity_id: value for value in handoff.factor_inputs}
    assert set(factor_inputs) == {"issuer.plug", "issuer.ddog", "issuer.nice", "issuer.jpm"}
    assert factor_inputs["issuer.plug"].value == 1285
    assert factor_inputs["issuer.ddog"].value == 8100
    assert factor_inputs["issuer.nice"].value == 9626
    assert factor_inputs["issuer.jpm"].value == 318512
    for factor_input in handoff.factor_inputs:
        assert set(factor_input.model_dump()) == {
            "entity_id",
            "metric",
            "value",
            "confidence",
            "as_of",
            "fiscal_period",
        }
        assert factor_input.as_fact().model_dump() == factor_input.model_dump()
    assert set(inspect.signature(run_h0_e2).parameters) == {
        "repository_root",
        "connection",
        "raw_store",
    }

    wrong_hash = handoff.model_dump(mode="python")
    wrong_hash["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="content_sha256"):
        CoreHeadcountExtractionHandoff.model_validate(wrong_hash)
    provenance_leak = handoff.model_dump(mode="python")
    factor_payloads = list(provenance_leak["factor_inputs"])
    factor_payloads[0]["raw_ref"] = "raw.fetches:1"
    provenance_leak["factor_inputs"] = factor_payloads
    provenance_leak["handoff_id"] = ""
    provenance_leak["content_sha256"] = ""
    with pytest.raises(ValidationError, match="raw_ref|extra"):
        CoreHeadcountExtractionHandoff.model_validate(provenance_leak)


def test_e2_dagster_activation_is_exact_local_ci_only(connection) -> None:
    import data_engine.headcount_assets as headcount_assets

    handoff = run_h0_e2(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    activation = H0HandoffActivation(
        consumer="S0-core-strategy-tiny",
        environment="local",
        expected_handoff_id=handoff.handoff_id,
        expected_handoff_sha256=handoff.content_sha256,
    )
    definitions = build_h0_e2_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=activation,
    )
    dg.Definitions.validate_loadable(definitions)
    assert not definitions.schedules
    assert not definitions.sensors
    assert not hasattr(headcount_assets, "defs")

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    assert result.success
    assert result.output_for_node(H0_E2_ASSET_NAME) == handoff
    assert (
        H0HandoffActivation(
            consumer="H0-core-headcount-extraction",
            environment="ci",
            expected_handoff_id=handoff.handoff_id,
            expected_handoff_sha256=handoff.content_sha256,
        ).environment
        == "ci"
    )

    with pytest.raises(ValidationError, match="local|ci"):
        H0HandoffActivation(
            consumer="S0-core-strategy-tiny",
            environment="staging",
            expected_handoff_id=handoff.handoff_id,
            expected_handoff_sha256=handoff.content_sha256,
        )
    with pytest.raises(ValidationError, match="H0-core|S0-core"):
        H0HandoffActivation(
            consumer="issue-27-staging-canary",
            environment="local",
            expected_handoff_id=handoff.handoff_id,
            expected_handoff_sha256=handoff.content_sha256,
        )
    with pytest.raises(ValidationError, match="False"):
        H0HandoffActivation(
            consumer="S0-core-strategy-tiny",
            environment="local",
            expected_handoff_id=handoff.handoff_id,
            expected_handoff_sha256=handoff.content_sha256,
            live_model_allowed=True,
        )
    with pytest.raises(ValidationError, match="ID and hash"):
        H0HandoffActivation(
            consumer="S0-core-strategy-tiny",
            environment="local",
            expected_handoff_id="core-headcount-extraction-handoff:" + "0" * 64,
            expected_handoff_sha256="1" * 64,
        )

    stale_activation = H0HandoffActivation(
        consumer="S0-core-strategy-tiny",
        environment="local",
        expected_handoff_id="core-headcount-extraction-handoff:" + "0" * 64,
        expected_handoff_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="does not match its activation"):
        H0E2RunnerResource(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=stale_activation,
        ).run()
    with pytest.raises(ValueError, match="cannot be activated by a release"):
        build_h0_e2_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(Any, object()),
        )
