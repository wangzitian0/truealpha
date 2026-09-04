"""The composition root derives from the source registrations and never enumerates
a semantic or a vendor itself (#72, init.md rule 22).

Behaviour is asserted where it can be executed (route builders resolve and build,
requests carry the owning registration's id); the one text-level check is the rule's
own shape — a semantic-type or origin literal appearing in generic code — which is a
property of the source, not of a run.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

from data_engine.datahub.production_topt import source_registrations as registry
from data_engine.datahub.production_topt.source_registrations import (
    REGISTRATIONS,
    OriginRegistration,
    RouteCell,
    RouteContext,
    SourceRegistration,
    registered_semantic_types,
    registration_for,
    source_by_parser,
)

GENERIC = Path(__file__).resolve().parents[2] / "src" / "data_engine" / "datahub"
#: Generic capture code: no semantic-type or origin literal may live here. Slice 1 of
#: #72 covers the composition root; `materialization.py` still branches on semantics to
#: shape the factor-input projection (`by_type["financial-fact"]`, `common_metric(...)`,
#: the snapshot SQL) — that is the projector port rule 22 names and #284's factor-output
#: declaration, the next slice. Its *enumeration* is already derived (see
#: test_derived_enumerations_are_the_registry_objects).
GENERIC_MODULES = (GENERIC / "production_topt" / "composition.py",)


def test_every_semantic_has_exactly_one_owner_and_the_order_is_the_deployed_one() -> None:
    owners: dict[str, list[str]] = {}
    for registration in REGISTRATIONS:
        for semantic in registration.semantic_types:
            owners.setdefault(semantic, []).append(registration.source_id)
    shared = {semantic: ids for semantic, ids in owners.items() if len(ids) > 1}
    assert not shared, f"semantics with more than one owning registration: {shared}"
    # Obligation ordinals feed content-addressed identities: the order is part of
    # every deployed run's identity and changes only with a deliberate release.
    assert registered_semantic_types() == ("market-price", "listing-identity", "universe-membership", "financial-fact")


def test_every_registration_declares_freshness_for_each_semantic_it_owns() -> None:
    for registration in REGISTRATIONS:
        missing = set(registration.semantic_types) - set(registration.freshness_max_age)
        assert not missing, f"{registration.source_id} owns {sorted(missing)} but declares no freshness window"


def test_every_route_builder_resolves_to_a_callable() -> None:
    for registration in REGISTRATIONS:
        builder = registration.resolve_route_builder()
        assert callable(builder), registration.route_builder


def test_release_route_builds_without_a_connection_or_a_vendor() -> None:
    """The cheapest executed proof that a builder receives cells and returns a port:
    the release-derived source needs nothing but the plan."""
    context = RouteContext(
        cutoff=datetime(2026, 9, 4, 22, 15, tzinfo=UTC),
        cutoff_date=datetime(2026, 9, 4, tzinfo=UTC).date(),
        price_cutoff_date=datetime(2026, 9, 4, tzinfo=UTC).date(),
        partition_start=datetime(2026, 6, 30, tzinfo=UTC),
        coordinates={},
        connection=None,
    )
    cells = [
        RouteCell("wi-1", "listing-identity", "issuer:cik:1", "instrument:1", "listing:xnas:aaa", "AAA"),
        RouteCell("wi-2", "universe-membership", "issuer:cik:1", "instrument:1", "listing:xnas:aaa", "AAA"),
    ]
    adapter = registration_for("listing-identity").resolve_route_builder()(context, cells)
    assert set(adapter.targets) == {"wi-1", "wi-2"}
    assert adapter.targets["wi-1"].semantic_type == "listing-identity"
    assert adapter.targets["wi-2"].payload["ticker"] == "AAA"


def test_registry_entry_ids_are_content_addressed_and_distinct() -> None:
    ids = [registration.entry_id for registration in REGISTRATIONS]
    assert len(set(ids)) == len(ids)
    assert all(
        entry.startswith("source-registry-entry:") and len(entry) == len("source-registry-entry:") + 64 for entry in ids
    )
    # Same declaration, same id: the hash is over the declared facts, not the object.
    first = REGISTRATIONS[0]
    assert first.entry_id == type(first)(**{**first.__dict__}).entry_id


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def test_generic_modules_carry_no_semantic_or_origin_literal() -> None:
    """The rule-22 shape: generic capture and materialization code names no semantic
    type and no vendor origin. Before #72 composition.py enumerated the four semantics
    in three places and branched on them; those enumerations now live only in the
    registrations."""
    forbidden = (
        set(registered_semantic_types())
        | {origin.origin_source for r in REGISTRATIONS for origin in r.origins}
        | {origin.origin_id for r in REGISTRATIONS for origin in r.origins}
    )
    offenders: dict[str, set[str]] = {}
    for path in GENERIC_MODULES:
        constants = _string_constants(path)
        # Identifiers carry no whitespace; prose (docstrings, messages) is not the rule's subject.
        identifiers = {c for c in constants if c and not any(ch.isspace() for ch in c)}
        hits = {c for c in identifiers if c in forbidden or any(f in c for f in ("yahoo", "twelve", "companyfacts"))}
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"semantic/origin literals in generic modules (#72): {offenders}"


def test_derived_enumerations_are_the_registry_objects() -> None:
    """materialization and quality_report do not re-list the semantics; they hold the
    registry's derivations by identity."""
    from data_engine.datahub import quality_report
    from data_engine.datahub.production_topt import composition, materialization

    assert composition.SEMANTIC_TYPES == registry.SEMANTIC_TYPES
    assert materialization._REQUIRED_TYPES == frozenset(registry.SEMANTIC_TYPES)
    assert quality_report._SOURCE_BY_PARSER is registry.SOURCE_BY_PARSER
    assert quality_report._IDENTITY_SEMANTICS is registry.RELEASE_SEMANTICS


def test_a_parser_vintage_claimed_by_two_origins_is_refused() -> None:
    """Silent overwrite would mis-attribute every historical observation written under
    the vintage; the registry refuses the conflict at derivation time."""
    import pytest

    def registration(origin_id: str, value_key: str) -> SourceRegistration:
        return SourceRegistration(
            source_id=f"probe-{origin_id}",
            version="v1",
            semantic_types=("probe",),
            freshness_max_age={},
            route_builder="x:y",
            origins=(OriginRegistration("probe:v1", origin_id, value_key, ("probe-parser:v1",)),),
        )

    with pytest.raises(ValueError, match="probe-parser:v1"):
        source_by_parser((registration("origin:a", "close"), registration("origin:b", "close")))
    # The same coordinate declared twice is not a conflict.
    assert source_by_parser((registration("origin:a", "close"), registration("origin:a", "close")))


def test_entry_id_covers_capacity_and_ledger_seat() -> None:
    from dataclasses import replace

    from data_engine.datahub.production_topt.source_registrations import CapacityDeclaration

    base = REGISTRATIONS[-1]
    assert replace(base, capacity=CapacityDeclaration(1, 1)).entry_id != base.entry_id
    assert replace(base, ledger_seat="probe").entry_id != base.entry_id
