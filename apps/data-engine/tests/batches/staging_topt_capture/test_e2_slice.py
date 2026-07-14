from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest
from data_engine import raw_store as shared_raw_store
from data_engine.batches.mvp_medium_validation.e0_slice import PostgresMarketPriceRepository
from data_engine.batches.staging_topt_capture.e0_slice import (
    freeze_yahoo_request_plan,
    load_frozen_e0_corpus,
)
from data_engine.batches.staging_topt_capture.e1_slice import (
    D3E1TinyExecution,
    InMemoryRawResponseLedger,
    YahooDailyBarNormalizer,
    YahooRawHttpAdapter,
    execute_e1_tiny_interaction,
)
from data_engine.batches.staging_topt_capture.e2_slice import (
    D3E2CaptureContext,
    build_e2_capture_context,
    persist_e1_execution,
)
from data_engine.config import settings
from data_engine.contract_repository import (
    PostgresCaptureEvaluationRepository,
    PostgresCaptureManifestRepository,
    PostgresCaptureScopeRepository,
    PostgresRegistrySnapshotRepository,
)
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_runtime.testing import skip_or_fail

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
FIRST_FETCHED_AT = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
SECOND_FETCHED_AT = datetime(2026, 4, 2, 0, 0, tzinfo=UTC)


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d3-e2-tests",
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
    required = active.execute(
        "select to_regclass('raw.fetches'), to_regclass('staging.normalized_records'), "
        "to_regclass('staging.contract_objects')"
    ).fetchone()
    if required is None or any(value is None for value in required):
        active.close()
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail("configured Postgres is not fully migrated", pytrace=False)
        pytest.skip("local Postgres is not fully migrated")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _context(environment: str = "ci") -> D3E2CaptureContext:
    request = load_frozen_e0_corpus(REPOSITORY_ROOT).request
    return build_e2_capture_context(
        request,
        environment=environment,  # type: ignore[arg-type]
        repository_root=REPOSITORY_ROOT,
    )


def _execution(
    *,
    body: bytes | None = None,
    fetched_at: datetime = FIRST_FETCHED_AT,
    context: D3E2CaptureContext | None = None,
    retry_once: bool = False,
) -> tuple[D3E2CaptureContext, D3E1TinyExecution]:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    active_context = context or _context()
    plan = freeze_yahoo_request_plan(
        corpus.request,
        capture_scope_id=active_context.scope.capture_scope_id,
        capture_scope_sha256=active_context.scope.content_sha256,
    )
    response_body = body or corpus.raw_body
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if retry_once and calls == 1:
            return httpx.Response(503, headers={"content-type": "text/plain"}, content=b"retry")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=response_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execution = execute_e1_tiny_interaction(
            plan,
            adapter=YahooRawHttpAdapter(client),
            normalizer=YahooDailyBarNormalizer(),
            raw_ledger=InMemoryRawResponseLedger(),
            clock=lambda: fetched_at,
        )
    return active_context, execution


def _counts(connection, call_plan_id: str) -> tuple[int, int]:
    raw_count = connection.execute(
        "select count(*) from raw.fetches where source_record_id like %s",
        (f"{call_plan_id}:%",),
    ).fetchone()[0]
    normalized_count = connection.execute(
        "select count(*) from staging.normalized_records where document_id = %s",
        ("document:yahoo-chart:nvda:2026-03-31",),
    ).fetchone()[0]
    return raw_count, normalized_count


def test_e2_persists_and_replays_identical_input_idempotently(connection) -> None:
    context, execution = _execution()
    _, later_execution = _execution(fetched_at=SECOND_FETCHED_AT, context=context)
    store = MemoryRawObjectStore()

    first = persist_e1_execution(connection, store, execution, context)
    repeated = persist_e1_execution(connection, store, later_execution, context)

    assert first.evaluation.ready
    assert first.raw_fetch_ids == repeated.raw_fetch_ids
    assert first.record == repeated.record
    assert first.manifest == repeated.manifest
    assert first.evaluation == repeated.evaluation
    assert first.normalized_inserted is True and repeated.normalized_inserted is False
    assert (
        first.registry_inserted,
        first.scope_inserted,
        first.manifest_inserted,
        first.evaluation_inserted,
    ) == (True, True, True, True)
    assert (
        repeated.registry_inserted,
        repeated.scope_inserted,
        repeated.manifest_inserted,
        repeated.evaluation_inserted,
    ) == (False, False, False, False)
    assert _counts(connection, execution.result.call_plan_id) == (1, 1)
    assert list(store.objects.values()) == [execution.landed_raw_responses[-1].response.body]
    assert PostgresRegistrySnapshotRepository(connection).get(first.registry.registry_snapshot_id) == first.registry
    assert PostgresCaptureScopeRepository(connection).get(first.scope.capture_scope_id) == first.scope
    assert PostgresCaptureManifestRepository(connection).get(first.manifest.capture_manifest_id) == first.manifest
    assert (
        PostgresCaptureEvaluationRepository(connection).get(first.evaluation.capture_evaluation_report_id)
        == first.evaluation
    )


def test_changed_bytes_append_raw_and_normalized_vintages(connection) -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    changed_body = corpus.raw_body.replace(b"174.39999389648438", b"174.40999389648438")
    context, original_execution = _execution()
    changed_context, changed_execution = _execution(
        body=changed_body,
        fetched_at=SECOND_FETCHED_AT,
        context=context,
    )
    store = MemoryRawObjectStore()

    original = persist_e1_execution(connection, store, original_execution, context)
    changed = persist_e1_execution(
        connection,
        store,
        changed_execution,
        changed_context,
        predecessor=original.record,
    )

    assert changed_context.scope == context.scope
    assert changed.raw_fetch_ids != original.raw_fetch_ids
    assert changed.record.normalized_record_id != original.record.normalized_record_id
    assert changed.record.is_restatement is True
    assert changed.record.supersedes_record_id == original.record.normalized_record_id
    assert changed.manifest.capture_manifest_id != original.manifest.capture_manifest_id
    assert changed.evaluation.capture_evaluation_report_id != original.evaluation.capture_evaluation_report_id
    assert _counts(connection, original_execution.result.call_plan_id) == (2, 2)
    assert len(store.objects) == 2

    repository = PostgresMarketPriceRepository(connection)
    before = repository.select_pit(
        subject=original.record.draft.subject,
        semantic_type_id=original.record.draft.semantic_type_id,
        semantic_type_version=original.record.draft.semantic_type_version,
        source_registry_entry_id=original.record.source_registry_entry_id,
        as_of=original.record.recorded_at,
        valid_on=original.payload.trading_date,
    )
    after = repository.select_pit(
        subject=changed.record.draft.subject,
        semantic_type_id=changed.record.draft.semantic_type_id,
        semantic_type_version=changed.record.draft.semantic_type_version,
        source_registry_entry_id=changed.record.source_registry_entry_id,
        as_of=changed.record.recorded_at,
        valid_on=changed.payload.trading_date,
    )
    assert before == (original.record,)
    assert after == (changed.record,)
    assert repository.payload_for(original.record.normalized_record_id) == original.payload


def test_every_retry_response_lands_before_final_normalization(connection) -> None:
    context, execution = _execution(retry_once=True)
    store = MemoryRawObjectStore()

    persisted = persist_e1_execution(connection, store, execution, context)

    assert len(execution.landed_raw_responses) == len(persisted.raw_fetch_ids) == 2
    assert tuple(store.objects.values()) == (
        b"retry",
        execution.landed_raw_responses[-1].response.body,
    )
    raw_reference = connection.execute(
        "select raw_ref from staging.normalized_records where normalized_record_id = %s",
        (persisted.record.normalized_record_id,),
    ).fetchone()[0]
    assert raw_reference == f"raw.fetches:{persisted.raw_fetch_ids[-1]}"


def test_e2_rejects_unbound_scope_before_any_persistence(connection) -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    context = _context()
    unrelated_scope_sha256 = "7" * 64
    plan = freeze_yahoo_request_plan(
        corpus.request,
        capture_scope_id=f"capture-scope:{unrelated_scope_sha256}",
        capture_scope_sha256=unrelated_scope_sha256,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=corpus.raw_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execution = execute_e1_tiny_interaction(
            plan,
            adapter=YahooRawHttpAdapter(client),
            normalizer=YahooDailyBarNormalizer(),
            raw_ledger=InMemoryRawResponseLedger(),
            clock=lambda: FIRST_FETCHED_AT,
        )
    with pytest.raises(ValueError, match="does not bind the frozen E2 scope"):
        persist_e1_execution(connection, MemoryRawObjectStore(), execution, context)
    assert _counts(connection, execution.result.call_plan_id) == (0, 0)


def test_identical_bytes_cannot_be_appended_as_a_restatement(connection) -> None:
    context, execution = _execution()
    store = MemoryRawObjectStore()
    original = persist_e1_execution(connection, store, execution, context)

    with pytest.raises(ValueError, match="identical raw bytes"):
        persist_e1_execution(
            connection,
            store,
            execution,
            context,
            predecessor=original.record,
        )
    assert _counts(connection, execution.result.call_plan_id) == (1, 1)


def test_e2_rejects_staging_activation() -> None:
    with pytest.raises(ValueError, match="only permits local or ci"):
        _context("staging")


@pytest.mark.parametrize(
    "statement",
    [
        "update raw.fetches set metadata = '{}'::jsonb",
        "delete from staging.normalized_records",
        "update staging.contract_objects set payload = '{}'::jsonb",
    ],
)
def test_e2_rows_are_append_only(connection, statement: str) -> None:
    context, execution = _execution()
    persist_e1_execution(connection, MemoryRawObjectStore(), execution, context)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with connection.transaction():
            connection.execute(statement)


def test_e2_round_trips_through_configured_s3_compatible_store(connection) -> None:
    context, execution = _execution()
    store = shared_raw_store.object_store()
    try:
        store.ensure_bucket()
    except Exception:
        skip_or_fail("no reachable S3-compatible object storage")

    persisted = persist_e1_execution(connection, store, execution, context)

    assert shared_raw_store.get_payload(connection, persisted.raw_fetch_ids[-1], store=store) == (
        execution.landed_raw_responses[-1].response.body
    )
