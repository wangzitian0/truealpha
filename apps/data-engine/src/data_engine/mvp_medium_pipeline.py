"""Generic registry-dispatched capture and normalization for D2 medium domains."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field
from truealpha_contracts import DataSource, RawCapture, RawObjectStore
from truealpha_contracts.execution import NormalizedRecordRef
from truealpha_contracts.registries import RegistrySnapshot, SourceRegistryEntry

from data_engine import raw_store
from data_engine.mvp_medium_models import MvpNormalizationDraft
from data_engine.mvp_medium_repository import PostgresMediumSemanticRepository, attach_normalized_lineage

CaptureAdapter = Callable[[BaseModel], RawCapture]
SourceNormalizer = Callable[["LandedMediumCapture"], tuple[MvpNormalizationDraft, ...]]


@dataclass(frozen=True)
class MediumAdapterRegistration:
    source_id: str
    source_version: str
    adapter_id: str
    adapter_version: str
    adapter_implementation_sha256: str
    configuration_type: type[BaseModel]
    raw_source: DataSource
    capture: CaptureAdapter

    @property
    def key(self) -> tuple[str, str]:
        return self.source_id, self.source_version


@dataclass(frozen=True)
class MediumNormalizerRegistration:
    source_id: str
    source_version: str
    semantic_type_id: str
    semantic_type_version: str
    normalizer_id: str
    normalizer_version: str
    normalizer_implementation_sha256: str
    normalize: SourceNormalizer

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.source_id,
            self.source_version,
            self.semantic_type_id,
            self.semantic_type_version,
        )


@dataclass(frozen=True)
class MediumCaptureWorkItem:
    source_id: str
    source_version: str
    semantic_type_ids: tuple[str, ...]
    semantic_type_version: str
    configuration: BaseModel
    recorded_at: datetime


class LandedMediumCapture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fetch_id: int = Field(gt=0)
    raw_ref: str = Field(pattern=r"^raw\.fetches:[1-9][0-9]*$")
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: str
    source_version: str
    source_registry_entry_id: str
    source_registry_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_type_ids: tuple[str, ...] = Field(min_length=1)
    semantic_type_versions: dict[str, str]
    source_record_id: str
    body: bytes
    content_type: str
    source_published_at: datetime | None = None
    fetched_at: datetime
    recorded_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediumCaptureBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    captures: tuple[LandedMediumCapture, ...] = Field(min_length=1)


class MediumNormalizationBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_records: tuple[NormalizedRecordRef, ...] = Field(min_length=1)
    inserted_record_ids: tuple[str, ...]


class MediumComponentCatalog:
    """Static process-start dispatch for the additive D2 source routes."""

    def __init__(
        self,
        *,
        registry: RegistrySnapshot,
        adapters: Sequence[MediumAdapterRegistration],
        normalizers: Sequence[MediumNormalizerRegistration],
        disabled_source_ids: frozenset[str] = frozenset(),
        disabled_type_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.registry = registry
        self._sources = {source.key: source for source in registry.sources}
        self._types = {(entry.semantic_type_id, entry.version): entry for entry in registry.semantic_types}
        self._adapters = {registration.key: registration for registration in adapters}
        self._normalizers = {registration.key: registration for registration in normalizers}
        self._disabled_source_ids = disabled_source_ids
        self._disabled_type_ids = disabled_type_ids
        if len(self._adapters) != len(adapters) or len(self._normalizers) != len(normalizers):
            raise ValueError("duplicate medium component registration")
        self._validate()

    def _validate(self) -> None:
        registered_source_ids = {source_id for source_id, _version in self._adapters}
        known_source_ids = {source.source_id for source in self.registry.sources}
        known_type_ids = {entry.semantic_type_id for entry in self.registry.semantic_types}
        if not registered_source_ids or not registered_source_ids <= known_source_ids:
            raise ValueError("medium adapters must bind known registry sources")
        if not self._disabled_source_ids <= registered_source_ids:
            raise ValueError("disabled medium sources must be registered")
        if not self._disabled_type_ids <= known_type_ids:
            raise ValueError("disabled medium semantic types must exist")
        expected_normalizers = {
            (source.source_id, source.version, semantic_type_id, semantic_type.version)
            for source in self.registry.sources
            if source.source_id in registered_source_ids
            for semantic_type_id in source.supported_type_ids
            for semantic_type in self.registry.semantic_types
            if semantic_type.semantic_type_id == semantic_type_id
        }
        if set(self._normalizers) != expected_normalizers:
            raise ValueError("medium normalizers do not cover registered source/type routes exactly")
        for source_key, adapter in self._adapters.items():
            source = self._sources[source_key]
            if (
                adapter.adapter_id != source.adapter_id
                or adapter.adapter_version != source.adapter_version
                or adapter.adapter_implementation_sha256 != source.adapter_implementation_sha256
            ):
                raise ValueError(f"adapter implementation drift for {source.source_id}@{source.version}")
        for route_key, normalizer in self._normalizers.items():
            source = self._sources[(route_key[0], route_key[1])]
            if (
                normalizer.normalizer_id != source.normalizer_id
                or normalizer.normalizer_version != source.normalizer_version
                or normalizer.normalizer_implementation_sha256 != source.normalizer_implementation_sha256
            ):
                raise ValueError(f"normalizer implementation drift for {route_key}")

    @property
    def adapter_registrations(self) -> tuple[MediumAdapterRegistration, ...]:
        return tuple(self._adapters[key] for key in sorted(self._adapters))

    @property
    def normalizer_registrations(self) -> tuple[MediumNormalizerRegistration, ...]:
        return tuple(self._normalizers[key] for key in sorted(self._normalizers))

    def source(self, source_id: str, source_version: str) -> SourceRegistryEntry:
        try:
            return self._sources[(source_id, source_version)]
        except KeyError as error:
            raise ValueError(f"unknown source coordinate: {source_id}@{source_version}") from error

    def capture(self, item: MediumCaptureWorkItem) -> RawCapture:
        source = self.source(item.source_id, item.source_version)
        if source.source_id in self._disabled_source_ids:
            raise ValueError(f"source is disabled for new capture: {source.source_id}")
        requested = set(item.semantic_type_ids)
        if not requested or requested - set(source.supported_type_ids):
            raise ValueError("capture work item requests an unsupported semantic type")
        if requested & self._disabled_type_ids:
            raise ValueError("capture work item requests a disabled semantic type")
        for semantic_type_id in requested:
            if (semantic_type_id, item.semantic_type_version) not in self._types:
                raise ValueError("capture work item uses an unknown semantic coordinate")
        adapter = self._adapters[source.key]
        if not isinstance(item.configuration, adapter.configuration_type):
            raise TypeError(f"{source.adapter_id} received the wrong configuration type")
        capture = adapter.capture(item.configuration)
        if capture.source is not adapter.raw_source:
            raise ValueError("adapter returned bytes under a different raw source")
        return capture

    def normalize(
        self,
        capture: LandedMediumCapture,
        semantic_type_id: str,
    ) -> tuple[MvpNormalizationDraft, ...]:
        if semantic_type_id not in capture.semantic_type_ids:
            raise ValueError("normalizer route is outside the landed capture plan")
        version = capture.semantic_type_versions[semantic_type_id]
        registration = self._normalizers[(capture.source_id, capture.source_version, semantic_type_id, version)]
        drafts = registration.normalize(capture)
        if not drafts:
            raise ValueError(f"normalizer emitted no {semantic_type_id} records")
        for draft in drafts:
            if draft.semantic_type_id != semantic_type_id or draft.raw_ref != capture.raw_ref:
                raise ValueError("normalizer draft escaped its registered route or raw lineage")
        return drafts


def land_medium_capture_plan(
    connection: Connection[Any],
    *,
    object_store: RawObjectStore,
    catalog: MediumComponentCatalog,
    work_items: Sequence[MediumCaptureWorkItem],
) -> MediumCaptureBatch:
    captures: list[LandedMediumCapture] = []
    for item in work_items:
        source = catalog.source(item.source_id, item.source_version)
        capture = catalog.capture(item)
        fetch_id = raw_store.insert_fetch(
            connection,
            source=capture.source,
            source_record_id=capture.source_record_id,
            body=capture.body,
            content_type=capture.content_type,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
            store=object_store,
            recorded_at=item.recorded_at,
        )
        row = connection.execute(
            """
            select payload_sha256, content_type, source_published_at,
                   fetched_at, recorded_at, metadata
            from raw.fetches where id = %s
            """,
            (fetch_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"raw.fetches:{fetch_id} disappeared after insertion")
        raw_sha256, content_type, published_at, fetched_at, recorded_at, metadata = row
        captures.append(
            LandedMediumCapture(
                fetch_id=fetch_id,
                raw_ref=raw_store.raw_ref(fetch_id),
                raw_object_sha256=raw_sha256,
                source_id=source.source_id,
                source_version=source.version,
                source_registry_entry_id=source.source_registry_entry_id,
                source_registry_entry_sha256=source.content_sha256,
                semantic_type_ids=tuple(sorted(set(item.semantic_type_ids))),
                semantic_type_versions={
                    semantic_type_id: item.semantic_type_version for semantic_type_id in item.semantic_type_ids
                },
                source_record_id=capture.source_record_id,
                body=capture.body,
                content_type=content_type,
                source_published_at=published_at,
                fetched_at=fetched_at,
                recorded_at=recorded_at,
                metadata=metadata,
            )
        )
    if not captures:
        raise ValueError("medium capture plan cannot be empty")
    return MediumCaptureBatch(captures=tuple(captures))


def normalize_medium_capture_batch(
    *,
    batch: MediumCaptureBatch,
    catalog: MediumComponentCatalog,
    repository: PostgresMediumSemanticRepository,
) -> MediumNormalizationBatch:
    records: list[NormalizedRecordRef] = []
    inserted_ids: list[str] = []
    records_by_document: dict[str, NormalizedRecordRef] = {}
    semantic_types = {(entry.semantic_type_id, entry.version): entry for entry in catalog.registry.semantic_types}
    for capture in batch.captures:
        source = catalog.source(capture.source_id, capture.source_version)
        if (
            capture.source_registry_entry_id != source.source_registry_entry_id
            or capture.source_registry_entry_sha256 != source.content_sha256
        ):
            raise ValueError("landed capture registry binding drifted before normalization")
        for semantic_type_id in capture.semantic_type_ids:
            semantic_type = semantic_types[(semantic_type_id, capture.semantic_type_versions[semantic_type_id])]
            for source_draft in catalog.normalize(capture, semantic_type_id):
                draft = source_draft
                if source_draft.supersedes_document_id is not None:
                    predecessor = records_by_document.get(source_draft.supersedes_document_id)
                    if predecessor is None:
                        raise ValueError("restatement predecessor was not normalized first")
                    draft = MvpNormalizationDraft.model_validate(
                        {
                            **source_draft.model_dump(mode="python"),
                            "payload": source_draft.payload,
                            "supersedes_document_id": None,
                            "supersedes_record_id": predecessor.normalized_record_id,
                        }
                    )
                record = attach_normalized_lineage(
                    draft=draft,
                    semantic_type=semantic_type,
                    source=source,
                    raw_object_sha256=capture.raw_object_sha256,
                )
                inserted = repository.put(record, draft.payload, raw_ref=capture.raw_ref)
                previous = records_by_document.setdefault(draft.document_id, record)
                if previous != record:
                    raise ValueError("one document ID emitted conflicting normalized records")
                records.append(record)
                if inserted:
                    inserted_ids.append(record.normalized_record_id)
    return MediumNormalizationBatch(
        normalized_records=tuple(sorted(records, key=lambda item: item.normalized_record_id)),
        inserted_record_ids=tuple(sorted(inserted_ids)),
    )


__all__ = [
    "LandedMediumCapture",
    "MediumAdapterRegistration",
    "MediumCaptureBatch",
    "MediumCaptureWorkItem",
    "MediumComponentCatalog",
    "MediumNormalizationBatch",
    "MediumNormalizerRegistration",
    "land_medium_capture_plan",
    "normalize_medium_capture_batch",
]
