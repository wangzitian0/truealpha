import hashlib
import json

import pytest
from pydantic import ValidationError
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.registries import (
    IdentifierNamespaceKind,
    IdentifierTypeRegistryEntry,
    RegistryHistory,
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)


def _hash(character: str) -> str:
    return character * 64


def _semantic_type(
    *,
    semantic_type_id: str = "semantic.financial-fact",
    version: str = "1.0.0",
    domain: DataDomain = DataDomain.FINANCIAL_FACTS,
    schema_version: str = "1",
    schema_fingerprint_sha256: str = _hash("a"),
    compatibility_sha256: str = _hash("b"),
    model_implementation_sha256: str = _hash("c"),
) -> SemanticTypeRegistryEntry:
    return SemanticTypeRegistryEntry(
        semantic_type_id=semantic_type_id,
        version=version,
        domain=domain,
        schema_version=schema_version,
        schema_fingerprint_sha256=schema_fingerprint_sha256,
        normalized_model_key="truealpha_contracts.models:FinancialFact",
        input_model_key="factors.types:Fact",
        repository_key="data_engine.repositories:FinancialFactRepository",
        projector_key="data_engine.projectors:FinancialFactProjector",
        compatibility_sha256=compatibility_sha256,
        model_implementation_sha256=model_implementation_sha256,
        repository_implementation_sha256=_hash("d"),
        projector_implementation_sha256=_hash("e"),
    )


def _source(
    *,
    source_id: str = "source.sec",
    version: str = "1.0.0",
    supported_type_ids: tuple[str, ...] = ("semantic.financial-fact",),
    adapter_implementation_sha256: str = _hash("f"),
    normalizer_implementation_sha256: str = _hash("1"),
) -> SourceRegistryEntry:
    return SourceRegistryEntry(
        source_id=source_id,
        version=version,
        adapter_id=f"adapter.{source_id.removeprefix('source.')}",
        adapter_version="1.0.0",
        normalizer_id=f"normalizer.{source_id.removeprefix('source.')}",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=supported_type_ids,
        configuration_schema_sha256=_hash("2"),
        mapping_schema_sha256=_hash("3"),
        adapter_implementation_sha256=adapter_implementation_sha256,
        normalizer_implementation_sha256=normalizer_implementation_sha256,
    )


def _identifier_type(
    *,
    identifier_type_id: str = "identifier.entity.cik",
    version: str = "1.0.0",
    namespace_kind: IdentifierNamespaceKind = IdentifierNamespaceKind.ENTITY,
    semantic_definition_sha256: str = _hash("4"),
    schema_version: str = "1",
    schema_fingerprint_sha256: str = _hash("5"),
    compatible_schema_versions: tuple[str, ...] = (),
    compatibility_sha256: str = _hash("6"),
    validator_implementation_sha256: str = _hash("7"),
    canonicalizer_implementation_sha256: str = _hash("8"),
) -> IdentifierTypeRegistryEntry:
    return IdentifierTypeRegistryEntry(
        identifier_type_id=identifier_type_id,
        version=version,
        namespace_kind=namespace_kind,
        semantic_definition_sha256=semantic_definition_sha256,
        schema_version=schema_version,
        schema_fingerprint_sha256=schema_fingerprint_sha256,
        compatible_schema_versions=compatible_schema_versions,
        compatibility_sha256=compatibility_sha256,
        validator_implementation_sha256=validator_implementation_sha256,
        canonicalizer_implementation_sha256=canonicalizer_implementation_sha256,
    )


def _snapshot(
    *,
    sources: tuple[SourceRegistryEntry, ...] | None = None,
    semantic_types: tuple[SemanticTypeRegistryEntry, ...] | None = None,
    identifier_types: tuple[IdentifierTypeRegistryEntry, ...] = (),
    required_type_ids: tuple[str, ...] = ("semantic.financial-fact",),
    required_identifier_type_ids: tuple[str, ...] = (),
    **overrides,
) -> RegistrySnapshot:
    return RegistrySnapshot(
        sources=sources or (_source(),),
        semantic_types=semantic_types or (_semantic_type(),),
        identifier_types=identifier_types,
        required_type_ids=required_type_ids,
        required_identifier_type_ids=required_identifier_type_ids,
        **overrides,
    )


def test_open_string_ids_accept_new_sources_and_types_without_enum_changes():
    semantic_type = _semantic_type(semantic_type_id="semantic.vendor-sentiment")
    source = _source(source_id="source.new-vendor", supported_type_ids=(semantic_type.semantic_type_id,))

    snapshot = _snapshot(
        sources=(source,), semantic_types=(semantic_type,), required_type_ids=(semantic_type.semantic_type_id,)
    )

    assert snapshot.sources[0].source_id == "source.new-vendor"
    assert snapshot.semantic_types[0].semantic_type_id == "semantic.vendor-sentiment"


@pytest.mark.parametrize(
    ("namespace_kind", "identifier_type_id"),
    (
        (IdentifierNamespaceKind.ENTITY, "identifier.entity.cik"),
        (IdentifierNamespaceKind.SECURITY, "identifier.security.isin"),
        (IdentifierNamespaceKind.LISTING, "identifier.listing.xnas-ticker"),
        (IdentifierNamespaceKind.INSTRUMENT, "identifier.instrument.openfigi"),
        (IdentifierNamespaceKind.DOCUMENT, "identifier.document.sec-accession"),
        (IdentifierNamespaceKind.METRIC, "identifier.metric.us-gaap.revenue"),
        (IdentifierNamespaceKind.RELATIONSHIP, "identifier.relationship.issuer-security"),
    ),
)
def test_identifier_namespaces_are_closed_while_ids_inside_them_remain_open(
    namespace_kind: IdentifierNamespaceKind,
    identifier_type_id: str,
):
    entry = _identifier_type(
        identifier_type_id=identifier_type_id,
        namespace_kind=namespace_kind,
    )

    assert entry.identifier_type_id == identifier_type_id
    assert entry.namespace_kind is namespace_kind


def test_unknown_or_mismatched_identifier_namespaces_fail_closed():
    with pytest.raises(ValidationError):
        _identifier_type(identifier_type_id="identifier.portfolio.internal-id")
    with pytest.raises(ValidationError, match="namespace does not match"):
        _identifier_type(
            identifier_type_id="identifier.entity.cik",
            namespace_kind=IdentifierNamespaceKind.SECURITY,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("source_id", "SEC"), ("source_id", "source.sec "), ("semantic_type_id", "financial-fact")),
)
def test_open_ids_reject_noncanonical_values(field: str, value: str):
    with pytest.raises(ValidationError):
        if field == "source_id":
            _source(source_id=value)
        else:
            _semantic_type(semantic_type_id=value)


def test_entries_are_frozen_and_forbid_unregistered_fields():
    source = _source()
    semantic_type = _semantic_type()
    identifier_type = _identifier_type()
    assert source.source_registry_entry_id == f"source-registry-entry:{source.content_sha256}"
    assert semantic_type.semantic_type_registry_entry_id == (
        f"semantic-type-registry-entry:{semantic_type.content_sha256}"
    )
    assert identifier_type.identifier_type_registry_entry_id == (
        f"identifier-type-registry-entry:{identifier_type.content_sha256}"
    )
    with pytest.raises(ValidationError, match="frozen"):
        source.version = "2.0.0"
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceRegistryEntry(**source.model_dump(), active=True)


def test_snapshot_id_and_hash_are_canonical_and_order_independent():
    second_type = _semantic_type(
        semantic_type_id="semantic.market-price",
        domain=DataDomain.MARKET_PRICES,
        schema_fingerprint_sha256=_hash("4"),
    )
    second_source = _source(
        source_id="source.price-vendor",
        supported_type_ids=(second_type.semantic_type_id,),
    ).model_copy(update={"supported_domains": (DataDomain.MARKET_PRICES,)})
    first_identifier_type = _identifier_type()
    second_identifier_type = _identifier_type(
        identifier_type_id="identifier.listing.xnas-ticker",
        namespace_kind=IdentifierNamespaceKind.LISTING,
        semantic_definition_sha256=_hash("9"),
        schema_fingerprint_sha256=_hash("0"),
    )
    first = _snapshot(
        sources=(_source(), second_source),
        semantic_types=(_semantic_type(), second_type),
        identifier_types=(first_identifier_type, second_identifier_type),
    )
    reversed_order = _snapshot(
        sources=(second_source, _source()),
        semantic_types=(second_type, _semantic_type()),
        identifier_types=(second_identifier_type, first_identifier_type),
    )

    assert first.registry_snapshot_id == reversed_order.registry_snapshot_id
    assert first.content_sha256 == reversed_order.content_sha256
    assert first.registry_snapshot_id == f"registry-snapshot:{first.content_sha256}"
    assert first.source_registry_snapshot_id == f"source-registry:{first.source_registry_sha256}"
    assert first.semantic_type_registry_snapshot_id == (f"semantic-type-registry:{first.semantic_type_registry_sha256}")
    assert first.identifier_type_registry_snapshot_id == (
        f"identifier-type-registry:{first.identifier_type_registry_sha256}"
    )


def test_snapshot_rejects_caller_supplied_content_hash_or_id_mismatch():
    with pytest.raises(ValidationError, match="content_sha256 does not match"):
        _snapshot(content_sha256=_hash("9"))
    with pytest.raises(ValidationError, match="registry_snapshot_id does not match"):
        _snapshot(registry_snapshot_id=f"registry-snapshot:{_hash('9')}")
    with pytest.raises(ValidationError, match="source_registry_sha256 does not match"):
        _snapshot(source_registry_sha256=_hash("9"))
    with pytest.raises(ValidationError, match="semantic_type_registry_snapshot_id does not match"):
        _snapshot(semantic_type_registry_snapshot_id=f"semantic-type-registry:{_hash('9')}")
    with pytest.raises(ValidationError, match="identifier_type_registry_sha256 does not match"):
        _snapshot(identifier_type_registry_sha256=_hash("9"))
    with pytest.raises(ValidationError, match="identifier_type_registry_snapshot_id does not match"):
        _snapshot(identifier_type_registry_snapshot_id=f"identifier-type-registry:{_hash('9')}")


def test_snapshot_round_trip_revalidates_every_content_address():
    snapshot = _snapshot()

    restored = RegistrySnapshot.model_validate_json(snapshot.model_dump_json())

    assert restored == snapshot


def test_empty_identifier_registry_preserves_legacy_snapshot_content_address():
    snapshot = _snapshot()
    legacy_payload = snapshot.model_dump(
        mode="json",
        exclude={
            "registry_snapshot_id",
            "content_sha256",
            "identifier_type_registry_snapshot_id",
            "identifier_type_registry_sha256",
            "identifier_types",
            "required_identifier_type_ids",
        },
    )
    encoded = json.dumps(
        legacy_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()

    assert snapshot.content_sha256 == hashlib.sha256(encoded).hexdigest()
    assert snapshot.identifier_type_registry_snapshot_id.startswith("identifier-type-registry:")


def test_duplicate_declared_values_fail_instead_of_being_silently_deduplicated():
    source = _source()
    with pytest.raises(ValidationError, match="supported_domains must not contain duplicates"):
        SourceRegistryEntry(
            **source.model_dump(exclude={"supported_domains"}),
            supported_domains=(DataDomain.FINANCIAL_FACTS, DataDomain.FINANCIAL_FACTS),
        )
    with pytest.raises(ValidationError, match="supported_type_ids must not contain duplicates"):
        SourceRegistryEntry(
            **source.model_dump(exclude={"supported_type_ids"}),
            supported_type_ids=("semantic.financial-fact", "semantic.financial-fact"),
        )
    with pytest.raises(ValidationError, match="required_type_ids must not contain duplicates"):
        _snapshot(required_type_ids=("semantic.financial-fact", "semantic.financial-fact"))
    with pytest.raises(ValidationError, match="required_identifier_type_ids must not contain duplicates"):
        _snapshot(
            identifier_types=(_identifier_type(),),
            required_identifier_type_ids=("identifier.entity.cik", "identifier.entity.cik"),
        )


def test_duplicate_and_conflicting_source_coordinates_fail_closed():
    source = _source()
    with pytest.raises(ValidationError, match="duplicate source ID/version"):
        _snapshot(sources=(source, source))

    conflicting = source.model_copy(update={"adapter_implementation_sha256": _hash("8")})
    with pytest.raises(ValidationError, match="conflicting source ID/version"):
        _snapshot(sources=(source, conflicting))


def test_duplicate_and_conflicting_semantic_coordinates_fail_closed():
    semantic_type = _semantic_type()
    with pytest.raises(ValidationError, match="duplicate semantic type ID/version"):
        _snapshot(semantic_types=(semantic_type, semantic_type))

    conflicting = semantic_type.model_copy(update={"repository_implementation_sha256": _hash("8")})
    with pytest.raises(ValidationError, match="conflicting semantic type ID/version"):
        _snapshot(semantic_types=(semantic_type, conflicting))


def test_duplicate_and_conflicting_identifier_coordinates_fail_closed():
    identifier_type = _identifier_type()
    with pytest.raises(ValidationError, match="duplicate identifier type ID/version"):
        _snapshot(identifier_types=(identifier_type, identifier_type))

    conflicting = identifier_type.model_copy(
        update={"validator_implementation_sha256": _hash("9")},
    )
    with pytest.raises(ValidationError, match="conflicting identifier type ID/version"):
        _snapshot(identifier_types=(identifier_type, conflicting))


def test_unknown_required_or_source_supported_types_fail_before_snapshot_construction():
    with pytest.raises(ValidationError, match="unknown required semantic types"):
        _snapshot(required_type_ids=("semantic.unknown",))
    with pytest.raises(ValidationError, match="references unknown semantic types"):
        _snapshot(sources=(_source(supported_type_ids=("semantic.unknown",)),))
    with pytest.raises(ValidationError, match="unknown required identifier types"):
        _snapshot(required_identifier_type_ids=("identifier.entity.unknown",))


def test_source_type_domain_mismatch_fails_closed():
    source = _source().model_copy(update={"supported_domains": (DataDomain.MARKET_PRICES,)})
    with pytest.raises(ValidationError, match="omits domains"):
        _snapshot(sources=(source,))


def test_schema_fingerprint_cannot_be_reused_for_an_incompatible_meaning():
    first = _semantic_type()
    renamed = _semantic_type(
        semantic_type_id="semantic.revenue-only",
        schema_fingerprint_sha256=first.schema_fingerprint_sha256,
    )
    second_source = _source(
        source_id="source.revenue-vendor",
        supported_type_ids=(renamed.semantic_type_id,),
    )
    with pytest.raises(ValidationError, match="schema fingerprint was reused"):
        _snapshot(sources=(_source(), second_source), semantic_types=(first, renamed))


def test_schema_version_cannot_drift_without_a_new_schema_version():
    first = _semantic_type()
    drifted = _semantic_type(
        version="1.1.0",
        schema_fingerprint_sha256=_hash("7"),
    )
    with pytest.raises(ValidationError, match="conflicting semantic type/schema version"):
        _snapshot(semantic_types=(first, drifted))


def test_stable_semantic_type_id_cannot_move_to_another_domain():
    first = _semantic_type()
    moved = _semantic_type(
        version="2.0.0",
        domain=DataDomain.MARKET_PRICES,
        schema_version="2",
        schema_fingerprint_sha256=_hash("7"),
    )
    with pytest.raises(ValidationError, match="semantic type domain changed"):
        _snapshot(semantic_types=(first, moved))


def test_stable_identifier_type_cannot_change_namespace_or_semantic_definition():
    first = _identifier_type()
    moved = _identifier_type(version="2.0.0").model_copy(
        update={"namespace_kind": IdentifierNamespaceKind.SECURITY},
    )
    with pytest.raises(ValidationError, match="namespace does not match namespace_kind"):
        _snapshot(identifier_types=(first, moved))

    redefined = _identifier_type(
        version="2.0.0",
        semantic_definition_sha256=_hash("9"),
    )
    with pytest.raises(ValidationError, match="identifier type semantic definition changed"):
        _snapshot(identifier_types=(first, redefined))


def test_identifier_schema_version_and_fingerprint_reuse_cannot_change_meaning():
    first = _identifier_type()
    schema_drift = _identifier_type(
        version="1.1.0",
        schema_fingerprint_sha256=_hash("9"),
    )
    with pytest.raises(ValidationError, match="conflicting identifier type/schema version"):
        _snapshot(identifier_types=(first, schema_drift))

    reused = _identifier_type(
        identifier_type_id="identifier.security.isin",
        namespace_kind=IdentifierNamespaceKind.SECURITY,
        semantic_definition_sha256=_hash("9"),
    )
    with pytest.raises(ValidationError, match="identifier schema fingerprint was reused"):
        _snapshot(identifier_types=(first, reused))


def test_implementation_can_change_additively_without_changing_schema_meaning():
    first = _semantic_type()
    implementation_revision = _semantic_type(
        version="1.1.0",
        model_implementation_sha256=_hash("7"),
    )

    snapshot = _snapshot(semantic_types=(first, implementation_revision))

    assert len(snapshot.semantic_types) == 2


def test_reused_adapter_or_normalizer_coordinates_cannot_change_hash_bindings():
    first = _source()
    conflicting_adapter = _source(
        source_id="source.sec-mirror",
        adapter_implementation_sha256=_hash("7"),
    ).model_copy(update={"adapter_id": first.adapter_id})
    with pytest.raises(ValidationError, match="conflicting adapter ID/version"):
        _snapshot(sources=(first, conflicting_adapter))

    conflicting_normalizer = _source(
        source_id="source.sec-mirror",
        normalizer_implementation_sha256=_hash("7"),
    ).model_copy(update={"normalizer_id": first.normalizer_id})
    with pytest.raises(ValidationError, match="conflicting normalizer ID/version"):
        _snapshot(sources=(first, conflicting_normalizer))


def test_compatibility_references_must_resolve_inside_the_snapshot():
    semantic_type = _semantic_type().model_copy(update={"compatible_schema_versions": ("0",)})
    with pytest.raises(ValidationError, match="unknown compatible schema versions"):
        _snapshot(semantic_types=(semantic_type,))

    identifier_type = _identifier_type(compatible_schema_versions=("0",))
    with pytest.raises(ValidationError, match="unknown compatible schema versions"):
        _snapshot(identifier_types=(identifier_type,))


def test_additive_identifier_registration_preserves_historical_resolution():
    historical = _identifier_type()
    root = _snapshot(
        identifier_types=(historical,),
        required_identifier_type_ids=(historical.identifier_type_id,),
    )
    added = _identifier_type(
        identifier_type_id="identifier.listing.xnas-ticker",
        namespace_kind=IdentifierNamespaceKind.LISTING,
        semantic_definition_sha256=_hash("9"),
        schema_fingerprint_sha256=_hash("0"),
    )
    compatible_revision = _identifier_type(
        version="2.0.0",
        schema_version="2",
        schema_fingerprint_sha256=_hash("a"),
        compatible_schema_versions=(historical.schema_version,),
        compatibility_sha256=_hash("b"),
        validator_implementation_sha256=_hash("c"),
        canonicalizer_implementation_sha256=_hash("d"),
    )

    child = root.extend(identifier_types=(added, compatible_revision))
    history = RegistryHistory(snapshots=(root, child))

    assert (
        history.snapshots[1].resolve_identifier_type(
            historical.identifier_type_id,
            historical.version,
        )
        == historical
    )
    assert (
        history.snapshots[1].resolve_identifier_schema_fingerprint(
            historical.identifier_type_id,
            historical.schema_version,
        )
        == historical.schema_fingerprint_sha256
    )
    assert historical.schema_version in compatible_revision.compatible_schema_versions
    with pytest.raises(ValueError, match="unknown identifier type coordinate"):
        child.resolve_identifier_type(historical.identifier_type_id, "latest")
    with pytest.raises(ValueError, match="unknown identifier schema"):
        child.resolve_identifier_schema_fingerprint(historical.identifier_type_id, "latest")


def test_additive_history_preserves_prior_snapshot_and_entries():
    root = _snapshot()
    second_type = _semantic_type(
        semantic_type_id="semantic.market-price",
        domain=DataDomain.MARKET_PRICES,
        schema_fingerprint_sha256=_hash("4"),
    )
    second_source = _source(
        source_id="source.price-vendor",
        supported_type_ids=(second_type.semantic_type_id,),
    ).model_copy(update={"supported_domains": (DataDomain.MARKET_PRICES,)})

    child = root.extend(sources=(second_source,), semantic_types=(second_type,))
    history = RegistryHistory(snapshots=(root, child))

    assert history.snapshots[0] is root
    assert history.snapshots[0].registry_snapshot_id == root.registry_snapshot_id
    assert history.snapshots[1].parent_snapshot_id == root.registry_snapshot_id
    assert root.sources == (_source(),)


def test_history_rejects_removed_changed_or_unlinked_snapshots():
    root = _snapshot()
    second_type = _semantic_type(
        semantic_type_id="semantic.market-price",
        domain=DataDomain.MARKET_PRICES,
        schema_fingerprint_sha256=_hash("4"),
    )
    second_source = _source(
        source_id="source.price-vendor",
        supported_type_ids=(second_type.semantic_type_id,),
    ).model_copy(update={"supported_domains": (DataDomain.MARKET_PRICES,)})
    child = root.extend(sources=(second_source,), semantic_types=(second_type,))

    removed = RegistrySnapshot(
        parent_snapshot_id=root.registry_snapshot_id,
        sources=(second_source,),
        semantic_types=(second_type,),
    )
    with pytest.raises(ValidationError, match="cannot remove source entries"):
        RegistryHistory(snapshots=(root, removed))

    changed_source = _source(version="1.0.0").model_copy(update={"adapter_implementation_sha256": _hash("8")})
    changed = RegistrySnapshot(
        parent_snapshot_id=root.registry_snapshot_id,
        sources=(changed_source, second_source),
        semantic_types=(_semantic_type(), second_type),
    )
    with pytest.raises(ValidationError, match="cannot change source entries"):
        RegistryHistory(snapshots=(root, changed))

    unlinked = RegistrySnapshot(
        parent_snapshot_id=f"registry-snapshot:{_hash('9')}",
        sources=child.sources,
        semantic_types=child.semantic_types,
        required_type_ids=child.required_type_ids,
    )
    with pytest.raises(ValidationError, match="parent_snapshot_id"):
        RegistryHistory(snapshots=(root, unlinked))

    no_op = root.extend()
    with pytest.raises(ValidationError, match="must add at least one"):
        RegistryHistory(snapshots=(root, no_op))


def test_history_rejects_removed_or_mutated_identifier_entries():
    historical = _identifier_type()
    root = _snapshot(identifier_types=(historical,))
    added = _identifier_type(
        identifier_type_id="identifier.listing.xnas-ticker",
        namespace_kind=IdentifierNamespaceKind.LISTING,
        semantic_definition_sha256=_hash("9"),
        schema_fingerprint_sha256=_hash("0"),
    )
    child = root.extend(identifier_types=(added,))

    removed = RegistrySnapshot(
        parent_snapshot_id=root.registry_snapshot_id,
        sources=child.sources,
        semantic_types=child.semantic_types,
        identifier_types=(added,),
        required_type_ids=child.required_type_ids,
    )
    with pytest.raises(ValidationError, match="cannot remove identifier type entries"):
        RegistryHistory(snapshots=(root, removed))

    mutated_historical = historical.model_copy(
        update={"canonicalizer_implementation_sha256": _hash("9")},
    )
    mutated = RegistrySnapshot(
        parent_snapshot_id=root.registry_snapshot_id,
        sources=child.sources,
        semantic_types=child.semantic_types,
        identifier_types=(mutated_historical, added),
        required_type_ids=child.required_type_ids,
    )
    with pytest.raises(ValidationError, match="cannot change identifier type entries"):
        RegistryHistory(snapshots=(root, mutated))
