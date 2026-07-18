from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.mvp_assets import (
    D1_E0_ASSET_NAME,
    D1_E2_ASSET_NAME,
    D1_E2_CONSUMERS,
    D1HandoffActivation,
    D1ProvisionalActivation,
    build_d1_e0_definitions,
    build_d1_e2_definitions,
    run_d1_e0,
    run_d1_e2,
)
from data_engine.mvp_models import FilingDocumentPayload
from data_engine.mvp_pipeline import FilingComponentCatalog, run_filing_pipeline
from data_engine.mvp_registry import (
    FILING_SEMANTIC_TYPE_ID,
    FILING_VERSION,
    build_filing_registry,
)
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.mvp_snapshot import build_filing_snapshot
from data_engine.mvp_sources import FilingDocumentNormalizer, FilingFixtureAdapter
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import NormalizedRecordRef, SemanticDraft
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.release import ArtifactRole, ReleaseArtifact, ReleaseManifest
from truealpha_contracts.universe import UniverseRef

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
CORPUS_PATH = Path("apps/data-engine/tests/fixtures/mvp_capture_tiny/corpus.v1.json")


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d1-fixtures",
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
    # autocommit=False + rollback keeps each test hermetic: rows never commit,
    # so sibling tests cannot leak content-identical raw.fetches vintages that
    # the content-addressed dedup would collapse onto (see #367). The two
    # execute_in_process idempotency runs below share this one connection, so
    # the first run's uncommitted writes are still visible to the second.
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


def test_dagster_e0_is_idempotent_and_matches_postgres_snapshot(connection) -> None:
    store = MemoryRawObjectStore()
    definitions = build_d1_e0_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        activation=D1ProvisionalActivation(),
    )
    dg.Definitions.validate_loadable(definitions)

    first = definitions.get_implicit_global_asset_job_def().execute_in_process()
    second = definitions.get_implicit_global_asset_job_def().execute_in_process()
    evidence = first.output_for_node(D1_E0_ASSET_NAME)
    repeated = second.output_for_node(D1_E0_ASSET_NAME)

    assert first.success and second.success
    assert evidence.evidence_id == repeated.evidence_id
    assert evidence.fixture_snapshot_id == evidence.postgres_snapshot_id
    assert evidence.fixture_runner_selection_id == evidence.postgres_runner_selection_id
    assert evidence.row_counts == {"filing_documents": 2, "normalized_records": 2, "raw_fetches": 2}
    assert evidence.stable_handoff is False


def test_dagster_e2_publishes_a_stable_named_consumer_handoff(connection) -> None:
    expected = run_d1_e2(REPOSITORY_ROOT, connection, MemoryRawObjectStore())
    definitions = build_d1_e2_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=D1HandoffActivation(
            consumer="H0-core-headcount-extraction",
            environment="local",
            expected_handoff_id=expected.handoff_id,
            expected_handoff_sha256=expected.content_sha256,
        ),
    )
    dg.Definitions.validate_loadable(definitions)

    first = definitions.get_implicit_global_asset_job_def().execute_in_process()
    repeated = definitions.get_implicit_global_asset_job_def().execute_in_process()
    handoff = first.output_for_node(D1_E2_ASSET_NAME)
    repeated_handoff = repeated.output_for_node(D1_E2_ASSET_NAME)

    assert first.success and repeated.success
    assert handoff == repeated_handoff
    assert handoff == expected
    assert handoff.stable_handoff is True
    assert handoff.schema_epoch == "staging.filing-document.v1+0019"
    assert (
        handoff.migration_sha256
        == hashlib.sha256((REPOSITORY_ROOT / "db/migrations/0019_mvp_filing_document.sql").read_bytes()).hexdigest()
    )
    assert handoff.allowed_consumers == D1_E2_CONSUMERS
    assert handoff.allowed_environments == ("ci", "local")
    assert len(handoff.normalized_record_ids) == 2
    assert handoff.evaluation.ready
    assert handoff.evaluation.capture_manifest_id == handoff.capture_manifest.capture_manifest_id
    assert handoff.capture_manifest.as_of == handoff.snapshot.request.as_of
    assert handoff.snapshot.snapshot_id.endswith(handoff.snapshot.content_sha256)
    assert handoff.runner_selection.snapshot_id == handoff.snapshot.snapshot_id
    assert handoff.selected_record_id == handoff.runner_selection.bindings[0].input_id
    selected_record = {record.normalized_record_id: record for record in handoff.snapshot.normalized_records}[
        handoff.selected_record_id
    ]
    assert handoff.runner_selection.factor_inputs[0].observation.payload_sha256 == selected_record.draft.payload_sha256
    assert type(handoff).model_validate_json(handoff.model_dump_json()) == handoff

    manifest_values = handoff.capture_manifest.model_dump(
        mode="python",
        exclude={"capture_manifest_id", "content_sha256", "created_at"},
    )
    mismatched_manifest = type(handoff.capture_manifest)(
        **manifest_values,
        created_at=handoff.capture_manifest.created_at + timedelta(seconds=1),
    )
    handoff_values = {
        name: getattr(handoff, name)
        for name in type(handoff).model_fields
        if name not in {"handoff_id", "content_sha256", "capture_manifest"}
    }
    with pytest.raises(ValidationError, match="does not bind the handoff manifest"):
        type(handoff)(**handoff_values, capture_manifest=mismatched_manifest)

    evaluation = handoff.evaluation
    mismatched_evaluation = type(evaluation)(
        **{
            name: getattr(evaluation, name)
            for name in type(evaluation).model_fields
            if name not in {"capture_evaluation_report_id", "content_sha256", "environment"}
        },
        environment="staging",
    )
    evaluation_values = {
        name: getattr(handoff, name)
        for name in type(handoff).model_fields
        if name not in {"handoff_id", "content_sha256", "evaluation"}
    }
    with pytest.raises(ValidationError, match="does not match the handoff manifest context"):
        type(handoff)(**evaluation_values, evaluation=mismatched_evaluation)

    pair_values = {
        name: getattr(handoff, name)
        for name in type(handoff).model_fields
        if name not in {"handoff_id", "content_sha256", "normalized_record_ids"}
    }
    with pytest.raises(ValidationError, match="original/amendment chain"):
        type(handoff)(
            **pair_values,
            normalized_record_ids=(handoff.selected_record_id, "normalized-record:" + "0" * 64),
        )

    binding = handoff.runner_selection.bindings[0]
    tampered_observation = binding.observation.model_copy(update={"payload_sha256": "0" * 64})
    tampered_binding = type(binding)(
        **{name: getattr(binding, name) for name in type(binding).model_fields if name != "observation"},
        observation=tampered_observation,
    )
    selection = handoff.runner_selection
    tampered_selection = type(selection)(
        **{
            name: getattr(selection, name)
            for name in type(selection).model_fields
            if name not in {"selection_id", "content_sha256", "bindings"}
        },
        bindings=(tampered_binding,),
    )
    runner_values = {
        name: getattr(handoff, name)
        for name in type(handoff).model_fields
        if name not in {"handoff_id", "content_sha256", "runner_selection"}
    }
    with pytest.raises(ValidationError, match="does not match the selected snapshot record"):
        type(handoff)(**runner_values, runner_selection=tampered_selection)


def test_e2_registry_drift_fails_with_a_targeted_error(connection, monkeypatch) -> None:
    pipeline = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    drifted_semantic_type = pipeline.registry.semantic_types[0].model_copy(
        update={"semantic_type_id": "semantic.unrelated-filing-document"}
    )
    drifted_source = pipeline.registry.sources[0].model_copy(
        update={"supported_type_ids": (drifted_semantic_type.semantic_type_id,)}
    )
    drifted_registry = RegistrySnapshot(
        sources=(drifted_source,),
        semantic_types=(drifted_semantic_type,),
        required_type_ids=(drifted_semantic_type.semantic_type_id,),
    )
    monkeypatch.setattr(
        "data_engine.mvp_assets.run_filing_pipeline",
        lambda **_kwargs: replace(pipeline, registry=drifted_registry),
    )

    with pytest.raises(ValueError, match="semantic type/version is absent from the registry snapshot"):
        run_d1_e2(REPOSITORY_ROOT, connection, MemoryRawObjectStore())


def test_publication_boundary_and_factor_input_are_point_in_time_safe(connection) -> None:
    pipeline = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    original, amended = pipeline.records
    repository = PostgresFilingDocumentRepository(connection)
    query = {
        "subject": original.draft.subject,
        "semantic_type_id": original.draft.semantic_type_id,
        "semantic_type_version": original.draft.semantic_type_version,
        "source_registry_entry_id": original.source_registry_entry_id,
        "valid_on": original.draft.valid_to,
    }

    assert repository.select_pit(
        **query,
        as_of=amended.draft.knowable_at - timedelta(microseconds=1),
    ) == (original,)
    assert repository.select_pit(**query, as_of=amended.draft.knowable_at) == (amended,)
    assert repository.select_pit(
        **query,
        as_of=amended.draft.knowable_at + timedelta(microseconds=1),
    ) == (amended,)

    bundle = build_filing_snapshot(
        records=pipeline.records,
        selected_record=amended,
        registry=pipeline.registry,
        as_of=amended.draft.knowable_at,
    )
    assert bundle.evaluation.ready
    assert bundle.snapshot.request.as_of == amended.draft.knowable_at
    factor_input = bundle.runner_selection.factor_inputs[0].observation.model_dump()
    assert set(factor_input) == {
        "subject",
        "payload_model_key",
        "payload_sha256",
        "valid_from",
        "valid_to",
        "confidence",
        "as_of",
    }
    assert not ({"source", "raw_ref", "lineage", "provenance"} & factor_input.keys())

    future_draft_values = amended.draft.model_dump(
        mode="python",
        exclude={"semantic_draft_id", "content_sha256", "knowable_at"},
    )
    future_draft = SemanticDraft(
        **future_draft_values,
        knowable_at=amended.draft.knowable_at + timedelta(seconds=30),
    )
    future_record_values = amended.model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256", "draft"},
    )
    future_record = NormalizedRecordRef(**future_record_values, draft=future_draft)
    with pytest.raises(ValueError):
        build_filing_snapshot(
            records=(future_record,),
            selected_record=future_record,
            registry=pipeline.registry,
            as_of=amended.draft.knowable_at,
        )


def test_reversed_fixture_order_and_repeated_pipeline_are_stable(connection) -> None:
    adapter = FilingFixtureAdapter()
    artifacts = adapter.load(REPOSITORY_ROOT, CORPUS_PATH)
    store = MemoryRawObjectStore()
    reversed_run = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        artifacts=tuple(reversed(artifacts)),
    )
    repeated = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        artifacts=tuple(reversed(artifacts)),
    )

    assert tuple(item.artifact_id for item in reversed_run.artifacts) == (
        "plug-original-filing",
        "plug-amended-filing",
    )
    assert reversed_run.records == repeated.records
    assert repeated.inserted == (False, False)
    assert reversed_run.records[1].supersedes_record_id == reversed_run.records[0].normalized_record_id


def test_new_normalizer_identity_replays_same_raw_without_mixing_registry_snapshots(connection) -> None:
    class RollBackProbe(Exception):
        pass

    with pytest.raises(RollBackProbe), connection.transaction():
        baseline = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
        )
        revised_source = baseline.registry.sources[0].model_copy(update={"normalizer_implementation_sha256": "a" * 64})
        revised_registry = RegistrySnapshot(
            sources=(revised_source,),
            semantic_types=baseline.registry.semantic_types,
            required_type_ids=baseline.registry.required_type_ids,
        )
        revised = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            registry=revised_registry,
        )

        assert {record.normalized_record_id for record in baseline.records}.isdisjoint(
            record.normalized_record_id for record in revised.records
        )
        selected = PostgresFilingDocumentRepository(connection).select_pit(
            subject=revised.records[0].draft.subject,
            semantic_type_id=revised.records[0].draft.semantic_type_id,
            semantic_type_version=revised.records[0].draft.semantic_type_version,
            source_registry_entry_id=revised_source.source_registry_entry_id,
            as_of=revised.records[1].draft.knowable_at,
            valid_on=revised.records[0].draft.valid_to,
        )
        assert selected == (revised.records[1],)
        raise RollBackProbe


def test_registry_only_source_extension_runs_through_the_existing_factory(connection) -> None:
    registry = build_filing_registry()
    base_source = registry.sources[0]
    extension = base_source.model_copy(update={"source_id": "source.fixture-sec-extension"})
    extended_registry = RegistrySnapshot(
        sources=(base_source, extension),
        semantic_types=registry.semantic_types,
        required_type_ids=registry.required_type_ids,
    )
    assert extension.adapter_id == base_source.adapter_id
    assert extension.normalizer_id == base_source.normalizer_id
    assert {source.key: source for source in extended_registry.sources}[extension.key] == extension

    class RollBackProbe(Exception):
        pass

    with pytest.raises(RollBackProbe), connection.transaction():
        base = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            registry=extended_registry,
        )
        first = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            registry=extended_registry,
            source_id=extension.source_id,
        )
        repeated = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            registry=extended_registry,
            source_id=extension.source_id,
        )
        assert first.records == repeated.records
        assert repeated.inserted == (False, False)
        assert first.raw_fetch_ids == base.raw_fetch_ids
        assert {record.normalized_record_id for record in first.records}.isdisjoint(
            record.normalized_record_id for record in base.records
        )
        assert all(record.source_registry_entry_id == extension.source_registry_entry_id for record in first.records)
        assert base.records[1].supersedes_record_id == base.records[0].normalized_record_id
        assert first.records[1].supersedes_record_id == first.records[0].normalized_record_id
        with pytest.raises(ValueError, match="another registry route"):
            FilingDocumentNormalizer().normalize(
                first.artifacts[1],
                first.raw_fetch_ids[1],
                first.artifacts[1].sha256,
                extension,
                extended_registry.semantic_types[0],
                base.records[0],
            )

        repository = PostgresFilingDocumentRepository(connection)
        base_selected = repository.select_pit(
            subject=base.records[0].draft.subject,
            semantic_type_id=FILING_SEMANTIC_TYPE_ID,
            semantic_type_version=FILING_VERSION,
            source_registry_entry_id=base_source.source_registry_entry_id,
            as_of=base.records[1].draft.knowable_at,
            valid_on=base.records[0].draft.valid_to,
        )
        selected = repository.select_pit(
            subject=first.records[0].draft.subject,
            semantic_type_id=FILING_SEMANTIC_TYPE_ID,
            semantic_type_version=FILING_VERSION,
            source_registry_entry_id=extension.source_registry_entry_id,
            as_of=first.records[1].draft.knowable_at,
            valid_on=first.records[0].draft.valid_to,
        )
        assert base_selected == (base.records[1],)
        assert selected == (first.records[1],)
        bundle = build_filing_snapshot(
            records=selected,
            selected_record=selected[0],
            registry=extended_registry,
            as_of=first.records[1].draft.knowable_at,
        )
        assert bundle.evaluation.ready
        assert len(bundle.runner_selection.factor_inputs) == 1
        raise RollBackProbe


def test_restatement_rejects_backdating_cross_registry_and_branching(connection) -> None:
    pipeline = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    original, amended = pipeline.records
    repository = PostgresFilingDocumentRepository(connection)

    original_values = original.model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256", "is_restatement", "supersedes_record_id"},
    )
    backdated = NormalizedRecordRef(
        **original_values,
        is_restatement=True,
        supersedes_record_id=amended.normalized_record_id,
    )
    with pytest.raises(ValueError, match="strictly later"):
        repository.put(backdated, pipeline.payloads[0], raw_ref=f"raw.fetches:{pipeline.raw_fetch_ids[0]}")

    revised_source = pipeline.registry.sources[0].model_copy(update={"normalizer_implementation_sha256": "a" * 64})
    amended_values = amended.model_dump(
        mode="python",
        exclude={
            "normalized_record_id",
            "content_sha256",
            "source_registry_entry_id",
            "source_registry_entry_sha256",
        },
    )
    cross_registry = NormalizedRecordRef(
        **amended_values,
        source_registry_entry_id=revised_source.source_registry_entry_id,
        source_registry_entry_sha256=revised_source.content_sha256,
    )
    with pytest.raises(ValueError, match="registry-bound"):
        repository.put(cross_registry, pipeline.payloads[1], raw_ref=f"raw.fetches:{pipeline.raw_fetch_ids[1]}")

    successor_draft_values = amended.draft.model_dump(
        mode="python",
        exclude={"semantic_draft_id", "content_sha256", "knowable_at", "produced_at"},
    )
    successor_draft = SemanticDraft(
        **successor_draft_values,
        knowable_at=amended.draft.knowable_at + timedelta(days=1),
        produced_at=amended.draft.produced_at + timedelta(days=1),
    )
    successor_values = amended.model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256", "draft", "recorded_at"},
    )
    competing_successor = NormalizedRecordRef(
        **successor_values,
        draft=successor_draft,
        recorded_at=amended.recorded_at + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="multiple successors"):
        repository.put(
            competing_successor,
            pipeline.payloads[1],
            raw_ref=f"raw.fetches:{pipeline.raw_fetch_ids[1]}",
        )


def test_changed_filing_bytes_append_a_new_raw_and_normalized_vintage(connection) -> None:
    original, amended = FilingFixtureAdapter().load(REPOSITORY_ROOT, CORPUS_PATH)
    changed_body = amended.body + b"\n<!-- deterministic changed-vintage sentinel -->\n"
    changed = replace(
        amended,
        body=changed_body,
        sha256=hashlib.sha256(changed_body).hexdigest(),
        accepted_at=amended.accepted_at + timedelta(days=1),
        supersedes_artifact_id=amended.artifact_id,
    )
    store = MemoryRawObjectStore()

    class RollBackProbe(Exception):
        pass

    with pytest.raises(RollBackProbe), connection.transaction():
        first = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=store,
            artifacts=(changed, amended, original),
        )
        repeated = run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=store,
            artifacts=(original, amended, changed),
        )

        assert first.records == repeated.records
        assert len({record.normalized_record_id for record in first.records}) == 3
        assert first.records[2].supersedes_record_id == first.records[1].normalized_record_id
        assert connection.execute(
            """
            select count(*) from raw.fetches
            where source_record_id = %s and payload_sha256 = any(%s)
            """,
            (amended.source_record_id, [amended.sha256, changed.sha256]),
        ).fetchone() == (2,)
        assert connection.execute(
            "select count(*) from staging.normalized_records where normalized_record_id = any(%s)",
            ([record.normalized_record_id for record in first.records],),
        ).fetchone() == (3,)
        raise RollBackProbe


def test_missing_confidence_lineage_and_mutation_are_rejected(connection) -> None:
    pipeline = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
    )
    original = pipeline.records[0]
    values = original.model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256", "confidence"},
    )
    with pytest.raises(ValidationError, match="confidence"):
        NormalizedRecordRef(**values)

    drifted_values = original.model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256", "mapping_version"},
    )
    drifted = NormalizedRecordRef(**drifted_values, mapping_version="missing-lineage:1.0.0")
    # savepoints (connection.transaction()) so each expected DB error rolls back
    # to its savepoint instead of aborting the outer non-autocommit transaction.
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="does not exist"), connection.transaction():
        PostgresFilingDocumentRepository(connection).put(
            drifted,
            pipeline.payloads[0],
            raw_ref="raw.fetches:9223372036854775807",
        )

    for statement in (
        "update staging.normalized_records set confidence = 0.5 where normalized_record_id = %s",
        "delete from staging.normalized_records where normalized_record_id = %s",
        "update staging.filing_documents set confidence = 0.5 where normalized_record_id = %s",
        "delete from staging.filing_documents where normalized_record_id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connection.transaction():
            connection.execute(statement, (original.normalized_record_id,))


def test_schema_drift_and_unreviewed_registry_component_are_rejected() -> None:
    payload = {
        "accession": "0001558370-21-007147",
        "form": "10-K",
        "filing_date": "2021-05-14",
        "report_period": "2020-12-31",
        "content_sha256": "a" * 64,
        "content_type": "text/html",
        "unexpected": "schema drift",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FilingDocumentPayload.model_validate(payload)

    registry = build_filing_registry()
    disabled_source = registry.sources[0].model_copy(
        update={
            "source_id": "source.fixture-sec-extension",
            "adapter_id": "data_engine:DisabledFilingAdapter",
        }
    )
    disabled_registry = RegistrySnapshot(
        sources=(disabled_source,),
        semantic_types=registry.semantic_types,
        required_type_ids=registry.required_type_ids,
    )
    with pytest.raises(ValueError, match="not activated"):
        FilingComponentCatalog.e0().resolve(
            disabled_registry,
            source_id=disabled_source.source_id,
            source_version=disabled_source.version,
            semantic_type_id=FILING_SEMANTIC_TYPE_ID,
            semantic_type_version=FILING_VERSION,
        )


def test_registry_route_errors_identify_the_requested_keys() -> None:
    with pytest.raises(ValueError) as error:
        FilingComponentCatalog.e0().resolve(
            build_filing_registry(),
            source_id="source.missing",
            source_version="9.9.9",
            semantic_type_id="semantic.missing",
            semantic_type_version="9.9.9",
        )

    message = str(error.value)
    assert "source=('source.missing', '9.9.9')" in message
    assert "semantic_type=('semantic.missing', '9.9.9')" in message


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
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
        manifest_signature_ref="sigstore:accepted-release",
    )


def test_release_manifest_cannot_activate_d1_e0(connection) -> None:
    release = _accepted_release_manifest()
    with pytest.raises(ValueError, match="not release-activated"):
        build_d1_e0_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=release,
        )
    with pytest.raises(ValueError, match="cannot be release-activated"):
        build_d1_e2_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=release,
        )

    expected = run_d1_e2(REPOSITORY_ROOT, connection, MemoryRawObjectStore())
    with pytest.raises(ValidationError, match="environment"):
        D1HandoffActivation(
            consumer="H0-core-headcount-extraction",
            environment="staging",
            expected_handoff_id=expected.handoff_id,
            expected_handoff_sha256=expected.content_sha256,
        )
    with pytest.raises(ValidationError, match="consumer"):
        D1HandoffActivation(
            consumer="unlisted-consumer",
            environment="local",
            expected_handoff_id=expected.handoff_id,
            expected_handoff_sha256=expected.content_sha256,
        )
    bad_activation = D1HandoffActivation(
        consumer="H0-core-headcount-extraction",
        environment="local",
        expected_handoff_id="mvp-normalization-handoff:" + "0" * 64,
        expected_handoff_sha256="0" * 64,
    )
    bad_definitions = build_d1_e2_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=bad_activation,
    )
    with pytest.raises(ValueError, match="does not match the activated identity"):
        bad_definitions.get_implicit_global_asset_job_def().execute_in_process()


def test_direct_runner_evidence_is_stable(connection) -> None:
    first = run_d1_e0(REPOSITORY_ROOT, connection, MemoryRawObjectStore())
    repeated = run_d1_e0(REPOSITORY_ROOT, connection, MemoryRawObjectStore())
    assert first == repeated
