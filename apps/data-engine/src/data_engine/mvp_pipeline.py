"""Registry-routed raw-to-normalized pipeline for the D1 filing slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from psycopg import Connection
from truealpha_contracts import RawObjectStore
from truealpha_contracts.execution import NormalizedRecordRef
from truealpha_contracts.registries import RegistrySnapshot

from data_engine.mvp_models import FilingDocumentPayload
from data_engine.mvp_registry import build_filing_registry
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.mvp_sources import (
    FilingDocumentNormalizer,
    FilingFixtureAdapter,
    FrozenFilingArtifact,
)
from data_engine.raw_store import get_payload, insert_fetch, raw_ref

DEFAULT_CORPUS_PATH = Path("apps/data-engine/tests/fixtures/mvp_capture_tiny/corpus.v1.json")


@dataclass(frozen=True)
class FilingPipelineRun:
    registry: RegistrySnapshot
    artifacts: tuple[FrozenFilingArtifact, ...]
    raw_fetch_ids: tuple[int, ...]
    records: tuple[NormalizedRecordRef, ...]
    payloads: tuple[FilingDocumentPayload, ...]
    inserted: tuple[bool, ...]


@dataclass(frozen=True)
class FilingComponentCatalog:
    """Resolve reviewed components by registry key, without source/type branches."""

    adapters: dict[str, FilingFixtureAdapter]
    normalizers: dict[str, FilingDocumentNormalizer]

    @classmethod
    def e0(cls) -> FilingComponentCatalog:
        return cls(
            adapters={"data_engine:FilingFixtureAdapter": FilingFixtureAdapter()},
            normalizers={"data_engine:FilingDocumentNormalizer": FilingDocumentNormalizer()},
        )

    def resolve(
        self,
        registry: RegistrySnapshot,
    ) -> tuple[FilingFixtureAdapter, FilingDocumentNormalizer]:
        if len(registry.sources) != 1 or len(registry.semantic_types) != 1:
            raise ValueError("D1 E0 requires exactly one reviewed filing route")
        source = registry.sources[0]
        semantic_type = registry.semantic_types[0]
        if semantic_type.semantic_type_id not in source.supported_type_ids:
            raise ValueError("registry source/type route is disconnected")
        try:
            return self.adapters[source.adapter_id], self.normalizers[source.normalizer_id]
        except KeyError as error:
            raise ValueError(f"registry component is not activated: {error.args[0]}") from error


def run_filing_pipeline(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    registry: RegistrySnapshot | None = None,
    components: FilingComponentCatalog | None = None,
    artifacts: tuple[FrozenFilingArtifact, ...] | None = None,
) -> FilingPipelineRun:
    active_registry = registry or build_filing_registry()
    adapter, normalizer = (components or FilingComponentCatalog.e0()).resolve(active_registry)
    loaded = adapter.load(repository_root, corpus_path) if artifacts is None else artifacts
    ordered = tuple(sorted(loaded, key=lambda item: (item.accepted_at, item.artifact_id)))
    if not ordered:
        raise ValueError("filing pipeline requires at least one artifact")

    repository = PostgresFilingDocumentRepository(connection)
    records_by_artifact: dict[str, NormalizedRecordRef] = {}
    raw_fetch_ids: list[int] = []
    records: list[NormalizedRecordRef] = []
    payloads: list[FilingDocumentPayload] = []
    inserted: list[bool] = []
    for artifact in ordered:
        capture = adapter.capture(artifact)
        fetch_id = insert_fetch(
            connection,
            source=capture.source,
            source_record_id=capture.source_record_id,
            body=capture.body,
            content_type=capture.content_type,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
            store=raw_store,
            recorded_at=artifact.accepted_at + timedelta(minutes=1),
        )
        landed_body = get_payload(connection, fetch_id, store=raw_store)
        if landed_body != artifact.body:
            raise ValueError("raw readback differs from the captured filing bytes")
        predecessor = None
        if artifact.supersedes_artifact_id is not None:
            try:
                predecessor = records_by_artifact[artifact.supersedes_artifact_id]
            except KeyError as error:
                raise ValueError("filing amendment predecessor was not normalized first") from error
        record, payload = normalizer.normalize(
            artifact,
            fetch_id,
            artifact.sha256,
            active_registry,
            predecessor,
        )
        was_inserted = repository.put(record, payload, raw_ref=raw_ref(fetch_id))
        records_by_artifact[artifact.artifact_id] = record
        raw_fetch_ids.append(fetch_id)
        records.append(record)
        payloads.append(payload)
        inserted.append(was_inserted)
    return FilingPipelineRun(
        registry=active_registry,
        artifacts=ordered,
        raw_fetch_ids=tuple(raw_fetch_ids),
        records=tuple(records),
        payloads=tuple(payloads),
        inserted=tuple(inserted),
    )


__all__ = [
    "DEFAULT_CORPUS_PATH",
    "FilingComponentCatalog",
    "FilingPipelineRun",
    "run_filing_pipeline",
]
