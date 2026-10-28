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
        universe_published_at=None,
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
    assert replace(base, ledger_seat=None, capacity=CapacityDeclaration(1, 1)).entry_id != base.entry_id
    assert replace(base, ledger_seat="nport", capacity=None).entry_id != base.entry_id
    whole = replace(base, ledger_seat=None, capacity=CapacityDeclaration(8, 60, 800, account="twelvedata"))
    assert whole.entry_id != replace(base, ledger_seat=None, capacity=CapacityDeclaration(8, 60, 800)).entry_id


def test_entry_id_covers_the_share_of_every_environment(monkeypatch) -> None:
    """A percent that moves in the contract makes a new registration id: the shares sign the registration (#1520)."""
    import json

    from truealpha_contracts.environment_contract import EnvironmentContract, load_environment_contract

    prices = registration_for("market-price")  # origins: yahoo, twelvedata, moomoo
    filings = registration_for("financial-fact")  # seat sec, origin moomoo: no Twelve Data
    before = (prices.entry_id, filings.entry_id)
    raw = json.loads(load_environment_contract().model_dump_json())
    raw["vendor_accounts"]["accounts"]["twelvedata"]["percent"]["staging"] = 30
    changed = EnvironmentContract.model_validate_json(json.dumps(raw))
    monkeypatch.setattr(registry, "load_environment_contract", lambda: changed)

    assert prices.entry_id != before[0]
    assert filings.entry_id == before[1], "the percent of one account moves no registration that does not call it"


# -- rule 6: capacity is declared once, per ledger seat, and every vendor names its seat -------


def test_every_vendor_registration_and_origin_names_a_declared_seat() -> None:
    """#729 criterion 1: a registered source without a capacity fails here. Only the
    release-frozen configuration calls no vendor."""
    for registration in REGISTRATIONS:
        if registration.corroboration_class != "release":
            assert registration.ledger_seat in registry.LEDGER_CAPACITIES, registration.source_id
            assert registration.capacity is registry.LEDGER_CAPACITIES[registration.ledger_seat]
        for origin in registration.origins:
            assert origin.ledger_seat in registry.LEDGER_CAPACITIES, origin.origin_source
            assert origin.capacity is registry.LEDGER_CAPACITIES[origin.ledger_seat]
    seats = {
        origin.ledger_seat: origin.origin_source for registration in REGISTRATIONS for origin in registration.origins
    }
    assert {"yahoo", "twelvedata", "moomoo"} <= set(seats)
    assert registration_for("financial-fact").ledger_seat == "sec"


def test_a_seat_and_a_capacity_that_disagree_are_refused() -> None:
    import pytest
    from data_engine.datahub.production_topt.source_registrations import CapacityDeclaration

    with pytest.raises(ValueError, match="contradicts the 'sec' seat"):
        OriginRegistration("probe:v1", "origin:probe", "close", ("p:v1",), CapacityDeclaration(10, 1), "sec")
    with pytest.raises(ValueError, match="no capacity declaration"):
        OriginRegistration("probe:v1", "origin:probe", "close", ("p:v1",), ledger_seat="never-declared")
    origin = OriginRegistration("probe:v1", "origin:probe", "close", ("p:v1",), ledger_seat="sec")
    assert origin.capacity is registry.LEDGER_CAPACITIES["sec"]


def test_every_registered_source_and_origin_declares_reliability_prior_in_init_order() -> None:
    """#1517 B1: the registry holds one reliability prior per source in init.md order."""
    from dataclasses import replace
    from decimal import Decimal

    import pytest
    from data_engine.datahub.production_topt.source_registrations import (
        MACRO_REGISTRATIONS,
        source_reliability_prior,
    )

    for registration in REGISTRATIONS:
        assert registration.reliability_prior is not None
        assert Decimal("0") < registration.reliability_prior <= Decimal("1")
        for origin in registration.origins:
            assert origin.reliability_prior is not None
            assert Decimal("0") < origin.reliability_prior <= Decimal("1")

    for macro in MACRO_REGISTRATIONS:
        assert macro.reliability_prior is not None
        assert Decimal("0") < macro.reliability_prior <= Decimal("1")

    p_release = source_reliability_prior("release-derived")
    p_sec = source_reliability_prior("sec-company-facts")
    p_moomoo = source_reliability_prior("origin:moomoo-financials:v1")
    p_twelve = source_reliability_prior("origin:twelve-data:v1")
    p_yahoo = source_reliability_prior("origin:yahoo:v1")
    p_prose = source_reliability_prior("origin:sec-10k:v1")
    assert p_release is not None and p_sec is not None and p_moomoo is not None
    assert p_twelve is not None and p_yahoo is not None and p_prose is not None
    assert p_release == Decimal("1") > p_sec > p_moomoo > p_twelve > p_yahoo > p_prose

    base = REGISTRATIONS[-1]
    mutated = replace(base)
    object.__setattr__(mutated, "reliability_prior", Decimal("0.50"))
    assert mutated.entry_id != base.entry_id

    with pytest.raises(ValueError, match="contradicts"):
        OriginRegistration(
            "probe:v1",
            "origin:sec-company-facts:v1",
            "revenue",
            ("p:v1",),
            ledger_seat="sec",
            reliability_prior=Decimal("0.50"),
        )
