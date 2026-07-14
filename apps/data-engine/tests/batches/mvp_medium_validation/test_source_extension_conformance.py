from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.mvp_pipeline import FilingComponentCatalog, run_filing_pipeline
from data_engine.mvp_registry import (
    FILING_SEMANTIC_TYPE_ID,
    FILING_VERSION,
    build_filing_registry,
)
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.mvp_snapshot import build_filing_snapshot
from data_engine.mvp_sources import (
    FilingDocumentNormalizer,
    FilingFixtureAdapter,
    FrozenFilingArtifact,
)
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from truealpha_contracts import RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import NormalizedRecordRef
from truealpha_contracts.registries import RegistrySnapshot, SourceRegistryEntry

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
EXTENSION_SOURCE_ID = "source.test-filing-extension"
EXTENSION_ADAPTER_ID = "d2_source_extension:SourceOwnedFilingAdapter"
EXTENSION_NORMALIZER_ID = "d2_source_extension:SourceOwnedFilingNormalizer"


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d2-source-extension",
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


class _SourceOwnedFilingRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    accession: str = Field(min_length=1)
    form: str = Field(min_length=1)
    report_period: date
    accepted_at: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _SourceOwnedFilingAdapter(FilingFixtureAdapter):
    """Test-source adapter registered without changing generic dispatch."""

    def __init__(self, *, inject_source_drift: bool = False) -> None:
        self._inject_source_drift = inject_source_drift

    def load(self, root: Path, corpus_path: Path) -> tuple[FrozenFilingArtifact, ...]:
        artifacts = super().load(root, corpus_path)
        for artifact in artifacts:
            source_row: dict[str, object] = {
                "artifact_id": artifact.artifact_id,
                "source_record_id": artifact.source_record_id,
                "accession": artifact.accession,
                "form": artifact.form,
                "report_period": artifact.report_period,
                "accepted_at": artifact.accepted_at,
                "content_sha256": artifact.sha256,
            }
            if self._inject_source_drift:
                source_row["unexpected_source_field"] = "schema drift"
            _SourceOwnedFilingRow.model_validate(source_row)
        return artifacts

    def capture(self, artifact: FrozenFilingArtifact) -> RawCapture:
        capture = super().capture(artifact)
        return capture.model_copy(
            update={
                "source_record_id": f"extension:{capture.source_record_id}",
                "metadata": capture.metadata
                | {
                    "registry_source_id": EXTENSION_SOURCE_ID,
                    "source_schema_version": 1,
                },
            }
        )


class _SourceOwnedFilingNormalizer(FilingDocumentNormalizer):
    """Test-source normalizer with an independently registered identity."""


def _implementation_sha256(component: type[object]) -> str:
    return hashlib.sha256(inspect.getsource(component).encode()).hexdigest()


def _extension_registry_and_catalog(
    *, inject_source_drift: bool = False
) -> tuple[RegistrySnapshot, SourceRegistryEntry, FilingComponentCatalog]:
    base_registry = build_filing_registry()
    base_source = base_registry.sources[0]
    extension = base_source.model_copy(
        update={
            "source_id": EXTENSION_SOURCE_ID,
            "adapter_id": EXTENSION_ADAPTER_ID,
            "normalizer_id": EXTENSION_NORMALIZER_ID,
            "configuration_schema_sha256": canonical_sha256(_SourceOwnedFilingRow.model_json_schema()),
            "adapter_implementation_sha256": _implementation_sha256(_SourceOwnedFilingAdapter),
            "normalizer_implementation_sha256": _implementation_sha256(_SourceOwnedFilingNormalizer),
        }
    )
    registry = RegistrySnapshot(
        parent_snapshot_id=base_registry.registry_snapshot_id,
        sources=(*base_registry.sources, extension),
        semantic_types=base_registry.semantic_types,
        required_type_ids=base_registry.required_type_ids,
    )
    base_catalog = FilingComponentCatalog.e0()
    catalog = FilingComponentCatalog(
        adapters=base_catalog.adapters
        | {EXTENSION_ADAPTER_ID: _SourceOwnedFilingAdapter(inject_source_drift=inject_source_drift)},
        normalizers=base_catalog.normalizers | {EXTENSION_NORMALIZER_ID: _SourceOwnedFilingNormalizer()},
    )
    return registry, extension, catalog


@pytest.fixture(scope="module")
def source_extension_database_url() -> Iterator[str]:
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_d2_source_extension_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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
        for migration in (
            *sorted((REPOSITORY_ROOT / "db/migrations").glob("*.sql")),
            REPOSITORY_ROOT / "db/roles.sql",
        ):
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
def connection(source_extension_database_url: str) -> Iterator[psycopg.Connection[Any]]:
    active = psycopg.connect(source_extension_database_url, connect_timeout=3, autocommit=False)
    active.execute("begin")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _table_count(connection: psycopg.Connection[Any], table: str) -> int:
    if table not in {"raw.fetches", "staging.normalized_records", "staging.filing_documents"}:
        raise ValueError(table)
    row = connection.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_existing_type_source_extension_survives_disable_and_replay(
    connection: psycopg.Connection[Any],
) -> None:
    assert _table_count(connection, "raw.fetches") == 0
    registry, extension, catalog = _extension_registry_and_catalog()
    registry_before = registry.model_dump_json()
    store = MemoryRawObjectStore()

    first = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        registry=registry,
        components=catalog,
        source_id=extension.source_id,
    )
    original, amended = first.artifacts
    changed_body = amended.body + b"\n<!-- D2 source-extension changed vintage -->\n"
    changed = replace(
        amended,
        body=changed_body,
        sha256=hashlib.sha256(changed_body).hexdigest(),
        accepted_at=amended.accepted_at + timedelta(days=1),
        supersedes_artifact_id=amended.artifact_id,
    )
    changed_run = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        registry=registry,
        components=catalog,
        artifacts=(changed, amended, original),
        source_id=extension.source_id,
    )
    repeated = run_filing_pipeline(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        registry=registry,
        components=catalog,
        artifacts=(original, amended, changed),
        source_id=extension.source_id,
    )

    assert first.inserted == (True, True)
    assert changed_run.inserted == (False, False, True)
    assert repeated.inserted == (False, False, False)
    assert first.records == changed_run.records[:2]
    assert changed_run.records == repeated.records
    assert changed_run.raw_fetch_ids == repeated.raw_fetch_ids
    assert len(set(changed_run.raw_fetch_ids)) == 3
    assert len({record.normalized_record_id for record in changed_run.records}) == 3
    assert all(record.source_registry_entry_id == extension.source_registry_entry_id for record in changed_run.records)
    assert all(record.confidence == Decimal("0.98") for record in changed_run.records)

    for fetch_id, record, artifact in zip(
        changed_run.raw_fetch_ids,
        changed_run.records,
        changed_run.artifacts,
        strict=True,
    ):
        raw_row = connection.execute(
            """
            select source_record_id, payload_sha256, object_uri,
                   metadata ->> 'registry_source_id'
            from raw.fetches where id = %s
            """,
            (fetch_id,),
        ).fetchone()
        assert raw_row == (
            f"extension:{artifact.source_record_id}",
            artifact.sha256,
            f"s3://d2-source-extension/{artifact.sha256}",
            EXTENSION_SOURCE_ID,
        )
        normalized_row = connection.execute(
            """
            select raw_ref, raw_object_sha256, confidence
            from staging.normalized_records where normalized_record_id = %s
            """,
            (record.normalized_record_id,),
        ).fetchone()
        assert normalized_row == (f"raw.fetches:{fetch_id}", artifact.sha256, Decimal("0.98"))
        assert record.raw_object_id == f"raw-object:{artifact.sha256}"
        assert record.raw_object_sha256 == artifact.sha256

    repository = PostgresFilingDocumentRepository(connection)
    subject = changed_run.records[0].draft.subject
    selection_arguments = {
        "subject": subject,
        "semantic_type_id": FILING_SEMANTIC_TYPE_ID,
        "semantic_type_version": FILING_VERSION,
        "source_registry_entry_id": extension.source_registry_entry_id,
        "valid_on": changed_run.records[0].draft.valid_to,
    }
    assert (
        repository.select_pit(
            **selection_arguments,
            as_of=original.accepted_at - timedelta(microseconds=1),
        )
        == ()
    )
    assert repository.select_pit(
        **selection_arguments,
        as_of=changed.accepted_at - timedelta(microseconds=1),
    ) == (changed_run.records[1],)
    selected = repository.select_pit(
        **selection_arguments,
        as_of=changed.accepted_at,
    )
    assert selected == (changed_run.records[2],)

    records_before_disable = repository.all_records(subject=subject)
    bundle_before_disable = build_filing_snapshot(
        records=records_before_disable,
        selected_record=selected[0],
        registry=registry,
        as_of=changed.accepted_at,
    )
    counts_before_disable = {
        table: _table_count(connection, table)
        for table in ("raw.fetches", "staging.normalized_records", "staging.filing_documents")
    }
    assert counts_before_disable == {
        "raw.fetches": 3,
        "staging.normalized_records": 3,
        "staging.filing_documents": 3,
    }

    disabled_catalog = FilingComponentCatalog.e0()
    base_source = build_filing_registry().sources[0]
    _, _, resolved_base, _ = disabled_catalog.resolve(
        registry,
        source_id=base_source.source_id,
        source_version=base_source.version,
        semantic_type_id=FILING_SEMANTIC_TYPE_ID,
        semantic_type_version=FILING_VERSION,
    )
    assert resolved_base == base_source
    with pytest.raises(ValueError, match="not activated"):
        run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=store,
            registry=registry,
            components=disabled_catalog,
            source_id=extension.source_id,
        )

    records_after_disable = repository.all_records(subject=subject)
    selected_after_disable = repository.select_pit(
        **selection_arguments,
        as_of=changed.accepted_at,
    )
    bundle_after_disable = build_filing_snapshot(
        records=records_after_disable,
        selected_record=selected_after_disable[0],
        registry=registry,
        as_of=changed.accepted_at,
    )
    assert registry.model_dump_json() == registry_before
    assert records_after_disable == records_before_disable
    assert selected_after_disable == selected
    assert bundle_after_disable == bundle_before_disable
    assert {
        table: _table_count(connection, table)
        for table in ("raw.fetches", "staging.normalized_records", "staging.filing_documents")
    } == counts_before_disable

    record_values = selected[0].model_dump(
        mode="python",
        exclude={"normalized_record_id", "content_sha256", "confidence"},
    )
    with pytest.raises(ValidationError, match="confidence"):
        NormalizedRecordRef(**record_values)


def test_source_extension_rejects_unexpected_fields_before_landing(
    connection: psycopg.Connection[Any],
) -> None:
    registry, extension, drifted_catalog = _extension_registry_and_catalog(inject_source_drift=True)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        run_filing_pipeline(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            registry=registry,
            components=drifted_catalog,
            source_id=extension.source_id,
        )

    assert _table_count(connection, "raw.fetches") == 0
    assert _table_count(connection, "staging.normalized_records") == 0
    assert _table_count(connection, "staging.filing_documents") == 0
