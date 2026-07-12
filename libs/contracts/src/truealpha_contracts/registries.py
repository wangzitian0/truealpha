"""Static, versioned source, semantic-type, and identifier-type registries.

Registry identifiers are deliberately open strings rather than enums. Adding a
source or a type inside an existing reviewed domain therefore adds a registry
entry without changing generic orchestration code. Registry snapshots remain
self-contained so a historical release never depends on the latest registry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from truealpha_contracts.data_quality import DataDomain

_OPEN_ID_SUFFIX = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
_COMPONENT_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

type SourceId = Annotated[
    str,
    StringConstraints(pattern=rf"^source\.{_OPEN_ID_SUFFIX}$"),
]
type SemanticTypeId = Annotated[
    str,
    StringConstraints(pattern=rf"^semantic\.{_OPEN_ID_SUFFIX}$"),
]
type IdentifierTypeId = Annotated[
    str,
    StringConstraints(
        pattern=rf"^identifier\.(?:entity|security|listing|instrument|document|metric|relationship)\."
        rf"{_OPEN_ID_SUFFIX}$",
    ),
]
type RegistryVersion = Annotated[str, StringConstraints(pattern=_VERSION_PATTERN)]
type Sha256 = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
type ComponentKey = Annotated[str, StringConstraints(pattern=_COMPONENT_KEY_PATTERN)]


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class IdentifierNamespaceKind(StrEnum):
    """Reviewed identity namespaces; identifiers remain open within each kind."""

    ENTITY = "entity"
    SECURITY = "security"
    LISTING = "listing"
    INSTRUMENT = "instrument"
    DOCUMENT = "document"
    METRIC = "metric"
    RELATIONSHIP = "relationship"


class SourceRegistryEntry(BaseModel):
    """One immutable adapter/normalizer implementation for an open source ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: SourceId
    version: RegistryVersion
    adapter_id: ComponentKey
    adapter_version: RegistryVersion
    normalizer_id: ComponentKey
    normalizer_version: RegistryVersion
    supported_domains: tuple[DataDomain, ...] = Field(min_length=1)
    supported_type_ids: tuple[SemanticTypeId, ...] = Field(min_length=1)
    configuration_schema_sha256: Sha256
    mapping_schema_sha256: Sha256
    adapter_implementation_sha256: Sha256
    normalizer_implementation_sha256: Sha256

    @property
    def key(self) -> tuple[str, str]:
        return self.source_id, self.version

    @property
    def content_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))

    @property
    def source_registry_entry_id(self) -> str:
        return f"source-registry-entry:{self.content_sha256}"

    @field_validator("supported_domains")
    @classmethod
    def validate_supported_domains(cls, values: tuple[DataDomain, ...]) -> tuple[DataDomain, ...]:
        if len(values) != len(set(values)):
            raise ValueError("supported_domains must not contain duplicates")
        return tuple(sorted(values, key=lambda value: value.value))

    @field_validator("supported_type_ids")
    @classmethod
    def validate_supported_type_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("supported_type_ids must not contain duplicates")
        return tuple(sorted(values))


class SemanticTypeRegistryEntry(BaseModel):
    """One version of a typed semantic record inside an existing domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_type_id: SemanticTypeId
    version: RegistryVersion
    domain: DataDomain
    schema_version: RegistryVersion
    schema_fingerprint_sha256: Sha256
    normalized_model_key: ComponentKey
    input_model_key: ComponentKey
    repository_key: ComponentKey
    projector_key: ComponentKey
    compatible_schema_versions: tuple[RegistryVersion, ...] = ()
    compatibility_sha256: Sha256
    model_implementation_sha256: Sha256
    repository_implementation_sha256: Sha256
    projector_implementation_sha256: Sha256

    @property
    def key(self) -> tuple[str, str]:
        return self.semantic_type_id, self.version

    @property
    def content_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))

    @property
    def semantic_type_registry_entry_id(self) -> str:
        return f"semantic-type-registry-entry:{self.content_sha256}"

    @field_validator("compatible_schema_versions")
    @classmethod
    def validate_compatible_versions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("compatible_schema_versions must not contain duplicates")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def reject_self_compatibility(self) -> SemanticTypeRegistryEntry:
        if self.schema_version in self.compatible_schema_versions:
            raise ValueError("compatible_schema_versions cannot reference the entry's own schema version")
        return self


class IdentifierTypeRegistryEntry(BaseModel):
    """One immutable implementation version of a stable identifier type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier_type_id: IdentifierTypeId
    version: RegistryVersion
    namespace_kind: IdentifierNamespaceKind
    semantic_definition_sha256: Sha256
    schema_version: RegistryVersion
    schema_fingerprint_sha256: Sha256
    compatible_schema_versions: tuple[RegistryVersion, ...] = ()
    compatibility_sha256: Sha256
    validator_implementation_sha256: Sha256
    canonicalizer_implementation_sha256: Sha256

    @property
    def key(self) -> tuple[str, str]:
        return self.identifier_type_id, self.version

    @property
    def content_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))

    @property
    def identifier_type_registry_entry_id(self) -> str:
        return f"identifier-type-registry-entry:{self.content_sha256}"

    @field_validator("compatible_schema_versions")
    @classmethod
    def validate_compatible_versions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("compatible_schema_versions must not contain duplicates")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def validate_namespace_and_compatibility(self) -> IdentifierTypeRegistryEntry:
        declared_namespace = self.identifier_type_id.split(".", maxsplit=2)[1]
        if declared_namespace != self.namespace_kind.value:
            raise ValueError("identifier_type_id namespace does not match namespace_kind")
        if self.schema_version in self.compatible_schema_versions:
            raise ValueError("compatible_schema_versions cannot reference the entry's own schema version")
        return self


class RegistrySnapshot(BaseModel):
    """Content-addressed, self-contained source, record, and identity registries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_snapshot_id: str = Field(default="", pattern=r"^(?:|registry-snapshot:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    source_registry_snapshot_id: str = Field(default="", pattern=r"^(?:|source-registry:[0-9a-f]{64})$")
    source_registry_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    semantic_type_registry_snapshot_id: str = Field(
        default="",
        pattern=r"^(?:|semantic-type-registry:[0-9a-f]{64})$",
    )
    semantic_type_registry_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    identifier_type_registry_snapshot_id: str = Field(
        default="",
        pattern=r"^(?:|identifier-type-registry:[0-9a-f]{64})$",
    )
    identifier_type_registry_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    parent_snapshot_id: str | None = Field(default=None, pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    sources: tuple[SourceRegistryEntry, ...] = Field(min_length=1)
    semantic_types: tuple[SemanticTypeRegistryEntry, ...] = Field(min_length=1)
    identifier_types: tuple[IdentifierTypeRegistryEntry, ...] = ()
    required_type_ids: tuple[SemanticTypeId, ...] = ()
    required_identifier_type_ids: tuple[IdentifierTypeId, ...] = ()

    @model_validator(mode="after")
    def validate_and_identify(self) -> RegistrySnapshot:
        sources = tuple(sorted(self.sources, key=lambda entry: entry.key))
        semantic_types = tuple(sorted(self.semantic_types, key=lambda entry: entry.key))
        identifier_types = tuple(sorted(self.identifier_types, key=lambda entry: entry.key))
        required_type_ids = tuple(sorted(self.required_type_ids))
        required_identifier_type_ids = tuple(sorted(self.required_identifier_type_ids))

        self._validate_source_keys(sources)
        self._validate_semantic_keys(semantic_types)
        self._validate_identifier_keys(identifier_types)
        if len(required_type_ids) != len(set(required_type_ids)):
            raise ValueError("required_type_ids must not contain duplicates")
        if len(required_identifier_type_ids) != len(set(required_identifier_type_ids)):
            raise ValueError("required_identifier_type_ids must not contain duplicates")

        type_domains = self._validate_semantic_meaning(semantic_types)
        known_identifier_type_ids = self._validate_identifier_meaning(identifier_types)
        self._validate_source_components(sources)
        self._validate_type_references(sources, semantic_types, required_type_ids, type_domains)
        self._validate_identifier_references(
            identifier_types,
            required_identifier_type_ids,
            known_identifier_type_ids,
        )

        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "semantic_types", semantic_types)
        object.__setattr__(self, "identifier_types", identifier_types)
        object.__setattr__(self, "required_type_ids", required_type_ids)
        object.__setattr__(self, "required_identifier_type_ids", required_identifier_type_ids)

        source_hash = _canonical_sha256(
            {"entries": [entry.model_dump(mode="json") for entry in sources]},
        )
        semantic_type_hash = _canonical_sha256(
            {"entries": [entry.model_dump(mode="json") for entry in semantic_types]},
        )
        identifier_type_hash = _canonical_sha256(
            {"entries": [entry.model_dump(mode="json") for entry in identifier_types]},
        )
        self._bind_content_address(
            hash_field="source_registry_sha256",
            id_field="source_registry_snapshot_id",
            id_prefix="source-registry",
            expected_hash=source_hash,
        )
        self._bind_content_address(
            hash_field="semantic_type_registry_sha256",
            id_field="semantic_type_registry_snapshot_id",
            id_prefix="semantic-type-registry",
            expected_hash=semantic_type_hash,
        )
        self._bind_content_address(
            hash_field="identifier_type_registry_sha256",
            id_field="identifier_type_registry_snapshot_id",
            id_prefix="identifier-type-registry",
            expected_hash=identifier_type_hash,
        )

        excluded_payload_fields = {"registry_snapshot_id", "content_sha256"}
        if not identifier_types and not required_identifier_type_ids:
            # Preserve content addresses produced before the identifier registry
            # existed while still exposing its deterministic empty-registry hash.
            excluded_payload_fields.update(
                {
                    "identifier_type_registry_snapshot_id",
                    "identifier_type_registry_sha256",
                    "identifier_types",
                    "required_identifier_type_ids",
                }
            )
        payload = self.model_dump(mode="json", exclude=excluded_payload_fields)
        expected_hash = _canonical_sha256(payload)
        expected_id = f"registry-snapshot:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match canonical registry content")
        if self.registry_snapshot_id and self.registry_snapshot_id != expected_id:
            raise ValueError("registry_snapshot_id does not match canonical registry content")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "registry_snapshot_id", expected_id)
        return self

    def _bind_content_address(
        self,
        *,
        hash_field: str,
        id_field: str,
        id_prefix: str,
        expected_hash: str,
    ) -> None:
        supplied_hash = getattr(self, hash_field)
        supplied_id = getattr(self, id_field)
        expected_id = f"{id_prefix}:{expected_hash}"
        if supplied_hash and supplied_hash != expected_hash:
            raise ValueError(f"{hash_field} does not match canonical registry content")
        if supplied_id and supplied_id != expected_id:
            raise ValueError(f"{id_field} does not match canonical registry content")
        object.__setattr__(self, hash_field, expected_hash)
        object.__setattr__(self, id_field, expected_id)

    @staticmethod
    def _validate_source_keys(sources: tuple[SourceRegistryEntry, ...]) -> None:
        seen: dict[tuple[str, str], SourceRegistryEntry] = {}
        for entry in sources:
            previous = seen.get(entry.key)
            if previous is not None:
                kind = "duplicate" if previous == entry else "conflicting"
                raise ValueError(f"{kind} source ID/version: {entry.source_id}@{entry.version}")
            seen[entry.key] = entry

    @staticmethod
    def _validate_semantic_keys(semantic_types: tuple[SemanticTypeRegistryEntry, ...]) -> None:
        seen: dict[tuple[str, str], SemanticTypeRegistryEntry] = {}
        for entry in semantic_types:
            previous = seen.get(entry.key)
            if previous is not None:
                kind = "duplicate" if previous == entry else "conflicting"
                raise ValueError(f"{kind} semantic type ID/version: {entry.semantic_type_id}@{entry.version}")
            seen[entry.key] = entry

    @staticmethod
    def _validate_identifier_keys(identifier_types: tuple[IdentifierTypeRegistryEntry, ...]) -> None:
        seen: dict[tuple[str, str], IdentifierTypeRegistryEntry] = {}
        for entry in identifier_types:
            previous = seen.get(entry.key)
            if previous is not None:
                kind = "duplicate" if previous == entry else "conflicting"
                raise ValueError(f"{kind} identifier type ID/version: {entry.identifier_type_id}@{entry.version}")
            seen[entry.key] = entry

    @staticmethod
    def _validate_semantic_meaning(
        semantic_types: tuple[SemanticTypeRegistryEntry, ...],
    ) -> dict[str, DataDomain]:
        type_domains: dict[str, DataDomain] = {}
        schema_versions: dict[tuple[str, str], tuple[str, str, str, str, tuple[str, ...]]] = {}
        fingerprint_meanings: dict[str, tuple[str, DataDomain, str, str, str, tuple[str, ...], str]] = {}

        for entry in semantic_types:
            previous_domain = type_domains.setdefault(entry.semantic_type_id, entry.domain)
            if previous_domain is not entry.domain:
                raise ValueError(f"semantic type domain changed: {entry.semantic_type_id}")

            schema_meaning = (
                entry.schema_fingerprint_sha256,
                entry.normalized_model_key,
                entry.input_model_key,
                entry.compatibility_sha256,
                entry.compatible_schema_versions,
            )
            schema_key = (entry.semantic_type_id, entry.schema_version)
            previous_schema = schema_versions.setdefault(schema_key, schema_meaning)
            if previous_schema != schema_meaning:
                raise ValueError(
                    f"conflicting semantic type/schema version: {entry.semantic_type_id}@{entry.schema_version}"
                )

            fingerprint_meaning = (
                entry.semantic_type_id,
                entry.domain,
                entry.schema_version,
                entry.normalized_model_key,
                entry.input_model_key,
                entry.compatible_schema_versions,
                entry.compatibility_sha256,
            )
            previous_meaning = fingerprint_meanings.setdefault(
                entry.schema_fingerprint_sha256,
                fingerprint_meaning,
            )
            if previous_meaning != fingerprint_meaning:
                raise ValueError("schema fingerprint was reused with incompatible semantic meaning")

        return type_domains

    @staticmethod
    def _validate_identifier_meaning(
        identifier_types: tuple[IdentifierTypeRegistryEntry, ...],
    ) -> set[str]:
        type_meanings: dict[str, tuple[IdentifierNamespaceKind, str]] = {}
        schema_versions: dict[tuple[str, str], tuple[str, tuple[str, ...], str]] = {}
        fingerprint_meanings: dict[
            str,
            tuple[str, IdentifierNamespaceKind, str, str, tuple[str, ...], str],
        ] = {}

        for entry in identifier_types:
            type_meaning = (entry.namespace_kind, entry.semantic_definition_sha256)
            previous_meaning = type_meanings.setdefault(entry.identifier_type_id, type_meaning)
            if previous_meaning[0] is not entry.namespace_kind:
                raise ValueError(f"identifier type namespace kind changed: {entry.identifier_type_id}")
            if previous_meaning[1] != entry.semantic_definition_sha256:
                raise ValueError(f"identifier type semantic definition changed: {entry.identifier_type_id}")

            schema_meaning = (
                entry.schema_fingerprint_sha256,
                entry.compatible_schema_versions,
                entry.compatibility_sha256,
            )
            schema_key = (entry.identifier_type_id, entry.schema_version)
            previous_schema = schema_versions.setdefault(schema_key, schema_meaning)
            if previous_schema != schema_meaning:
                raise ValueError(
                    f"conflicting identifier type/schema version: {entry.identifier_type_id}@{entry.schema_version}"
                )

            fingerprint_meaning = (
                entry.identifier_type_id,
                entry.namespace_kind,
                entry.semantic_definition_sha256,
                entry.schema_version,
                entry.compatible_schema_versions,
                entry.compatibility_sha256,
            )
            previous_fingerprint_meaning = fingerprint_meanings.setdefault(
                entry.schema_fingerprint_sha256,
                fingerprint_meaning,
            )
            if previous_fingerprint_meaning != fingerprint_meaning:
                raise ValueError("identifier schema fingerprint was reused with incompatible meaning")

        return set(type_meanings)

    @staticmethod
    def _validate_source_components(sources: tuple[SourceRegistryEntry, ...]) -> None:
        adapters: dict[tuple[str, str], tuple[str, str]] = {}
        normalizers: dict[tuple[str, str], tuple[str, str]] = {}
        for entry in sources:
            adapter_key = (entry.adapter_id, entry.adapter_version)
            adapter_binding = (entry.configuration_schema_sha256, entry.adapter_implementation_sha256)
            previous_adapter = adapters.setdefault(adapter_key, adapter_binding)
            if previous_adapter != adapter_binding:
                raise ValueError(f"conflicting adapter ID/version: {entry.adapter_id}@{entry.adapter_version}")

            normalizer_key = (entry.normalizer_id, entry.normalizer_version)
            normalizer_binding = (entry.mapping_schema_sha256, entry.normalizer_implementation_sha256)
            previous_normalizer = normalizers.setdefault(normalizer_key, normalizer_binding)
            if previous_normalizer != normalizer_binding:
                raise ValueError(f"conflicting normalizer ID/version: {entry.normalizer_id}@{entry.normalizer_version}")

    @staticmethod
    def _validate_type_references(
        sources: tuple[SourceRegistryEntry, ...],
        semantic_types: tuple[SemanticTypeRegistryEntry, ...],
        required_type_ids: tuple[str, ...],
        type_domains: dict[str, DataDomain],
    ) -> None:
        known_type_ids = set(type_domains)
        unknown_required = set(required_type_ids) - known_type_ids
        if unknown_required:
            raise ValueError(f"unknown required semantic types: {sorted(unknown_required)}")

        available_schema_versions = {(entry.semantic_type_id, entry.schema_version) for entry in semantic_types}
        for entry in semantic_types:
            unknown_compatible = {
                version
                for version in entry.compatible_schema_versions
                if (entry.semantic_type_id, version) not in available_schema_versions
            }
            if unknown_compatible:
                raise ValueError(
                    f"unknown compatible schema versions for {entry.semantic_type_id}: {sorted(unknown_compatible)}"
                )

        for source in sources:
            unknown_supported = set(source.supported_type_ids) - known_type_ids
            if unknown_supported:
                raise ValueError(
                    f"source {source.source_id}@{source.version} references unknown semantic types: "
                    f"{sorted(unknown_supported)}"
                )
            mismatched_domains = {
                type_id
                for type_id in source.supported_type_ids
                if type_domains[type_id] not in source.supported_domains
            }
            if mismatched_domains:
                raise ValueError(
                    f"source {source.source_id}@{source.version} omits domains for supported semantic types: "
                    f"{sorted(mismatched_domains)}"
                )

    @staticmethod
    def _validate_identifier_references(
        identifier_types: tuple[IdentifierTypeRegistryEntry, ...],
        required_identifier_type_ids: tuple[str, ...],
        known_identifier_type_ids: set[str],
    ) -> None:
        unknown_required = set(required_identifier_type_ids) - known_identifier_type_ids
        if unknown_required:
            raise ValueError(f"unknown required identifier types: {sorted(unknown_required)}")

        available_schema_versions = {(entry.identifier_type_id, entry.schema_version) for entry in identifier_types}
        for entry in identifier_types:
            unknown_compatible = {
                version
                for version in entry.compatible_schema_versions
                if (entry.identifier_type_id, version) not in available_schema_versions
            }
            if unknown_compatible:
                raise ValueError(
                    f"unknown compatible schema versions for {entry.identifier_type_id}: {sorted(unknown_compatible)}"
                )

    def resolve_identifier_type(
        self,
        identifier_type_id: IdentifierTypeId,
        version: RegistryVersion,
    ) -> IdentifierTypeRegistryEntry:
        """Resolve an exact implementation coordinate; never infer a latest version."""

        for entry in self.identifier_types:
            if entry.key == (identifier_type_id, version):
                return entry
        raise ValueError(f"unknown identifier type coordinate: {identifier_type_id}@{version}")

    def resolve_identifier_schema_fingerprint(
        self,
        identifier_type_id: IdentifierTypeId,
        schema_version: RegistryVersion,
    ) -> Sha256:
        """Resolve one immutable schema fingerprint without choosing an implementation."""

        fingerprints = {
            entry.schema_fingerprint_sha256
            for entry in self.identifier_types
            if entry.identifier_type_id == identifier_type_id and entry.schema_version == schema_version
        }
        if not fingerprints:
            raise ValueError(f"unknown identifier schema: {identifier_type_id}@{schema_version}")
        if len(fingerprints) != 1:
            raise ValueError(f"conflicting identifier schema: {identifier_type_id}@{schema_version}")
        return next(iter(fingerprints))

    def extend(
        self,
        *,
        sources: Iterable[SourceRegistryEntry] = (),
        semantic_types: Iterable[SemanticTypeRegistryEntry] = (),
        identifier_types: Iterable[IdentifierTypeRegistryEntry] = (),
        required_type_ids: Iterable[SemanticTypeId] | None = None,
        required_identifier_type_ids: Iterable[IdentifierTypeId] | None = None,
    ) -> RegistrySnapshot:
        """Build an additive child without mutating or dropping this snapshot."""

        return RegistrySnapshot(
            parent_snapshot_id=self.registry_snapshot_id,
            sources=(*self.sources, *sources),
            semantic_types=(*self.semantic_types, *semantic_types),
            identifier_types=(*self.identifier_types, *identifier_types),
            required_type_ids=(self.required_type_ids if required_type_ids is None else tuple(required_type_ids)),
            required_identifier_type_ids=(
                self.required_identifier_type_ids
                if required_identifier_type_ids is None
                else tuple(required_identifier_type_ids)
            ),
        )


class RegistryHistory(BaseModel):
    """A complete linear history whose successive snapshots are strictly additive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: tuple[RegistrySnapshot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_additive_history(self) -> RegistryHistory:
        snapshot_ids = [snapshot.registry_snapshot_id for snapshot in self.snapshots]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("registry history contains duplicate snapshots")
        if self.snapshots[0].parent_snapshot_id is not None:
            raise ValueError("registry history must begin with the root snapshot")

        for previous, current in zip(self.snapshots, self.snapshots[1:], strict=False):
            if current.parent_snapshot_id != previous.registry_snapshot_id:
                raise ValueError("registry history parent_snapshot_id does not match its predecessor")

            previous_sources = {entry.key: entry for entry in previous.sources}
            current_sources = {entry.key: entry for entry in current.sources}
            previous_types = {entry.key: entry for entry in previous.semantic_types}
            current_types = {entry.key: entry for entry in current.semantic_types}
            previous_identifier_types = {entry.key: entry for entry in previous.identifier_types}
            current_identifier_types = {entry.key: entry for entry in current.identifier_types}

            self._require_preserved(previous_sources, current_sources, "source")
            self._require_preserved(previous_types, current_types, "semantic type")
            self._require_preserved(
                previous_identifier_types,
                current_identifier_types,
                "identifier type",
            )
            added = (
                (set(current_sources) - set(previous_sources))
                | (set(current_types) - set(previous_types))
                | (set(current_identifier_types) - set(previous_identifier_types))
            )
            if not added:
                raise ValueError("each registry snapshot must add at least one versioned entry")
        return self

    @staticmethod
    def _require_preserved(
        previous: Mapping[tuple[str, str], BaseModel],
        current: Mapping[tuple[str, str], BaseModel],
        label: str,
    ) -> None:
        missing = set(previous) - set(current)
        if missing:
            raise ValueError(f"additive registry history cannot remove {label} entries: {sorted(missing)}")
        changed = {key for key, entry in previous.items() if current[key] != entry}
        if changed:
            raise ValueError(f"additive registry history cannot change {label} entries: {sorted(changed)}")
