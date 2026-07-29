"""The concept mapping is data, and the read of it is governed (#496).

What these assert is not "the default is correct" — that is #501's job — but that a
mapping can be CHANGED without a deploy and that the change is attributable afterwards.
Those two together are what make a larger universe a configuration problem.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.concept_mapping import (
    DEFAULT_RULESET,
    publish_ruleset,
    resolve_ruleset,
)
from truealpha_contracts.concept_mapping import ConceptMappingRuleset, ResolutionKind


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _with_extra_revenue_concept() -> ConceptMappingRuleset:
    """The realistic edit: an issuer starts tagging revenue under a variant we did not know."""
    payload = DEFAULT_RULESET.model_dump(mode="json", exclude={"ruleset_id", "content_sha256"})
    payload["version"] = "production-topt-concepts:v2"
    for mapping in payload["mappings"]:
        if mapping["field"] == "revenue":
            mapping["concepts"].append({"taxonomy": "us-gaap", "concept": "SalesRevenueNet"})
    return ConceptMappingRuleset.model_validate(payload)


def test_identity_is_derived_from_content() -> None:
    """Two publishers declaring the same rules must land the same object, and any changed
    rule must be a different object — that is what lets a figure name its rules later."""
    same = ConceptMappingRuleset(version=DEFAULT_RULESET.version, mappings=DEFAULT_RULESET.mappings)
    assert same.ruleset_id == DEFAULT_RULESET.ruleset_id
    assert _with_extra_revenue_concept().ruleset_id != DEFAULT_RULESET.ruleset_id


def test_a_fresh_database_resolves_the_shipped_default(connection) -> None:
    assert resolve_ruleset(connection).ruleset_id == DEFAULT_RULESET.ruleset_id


def test_publishing_changes_what_a_run_resolves_without_a_deploy(connection) -> None:
    """The point of the whole task."""
    updated = _with_extra_revenue_concept()
    contract_id, sequence = publish_ruleset(connection, updated, note="issuer began tagging SalesRevenueNet")
    assert contract_id == updated.ruleset_id
    assert sequence >= 1

    resolved = resolve_ruleset(connection)
    assert resolved.ruleset_id == updated.ruleset_id
    revenue = resolved.mapping_for("revenue")
    assert revenue is not None
    assert any(item.concept == "SalesRevenueNet" for item in revenue.concepts)


def test_the_head_is_the_latest_advance_not_the_latest_object(connection) -> None:
    """Reverting is a pointer advance, not a deletion — so a bad mapping is backed out
    without losing the record that it was once in force."""
    updated = _with_extra_revenue_concept()
    publish_ruleset(connection, updated, note="forward")
    publish_ruleset(connection, DEFAULT_RULESET, note="revert: variant was a segment tag")
    assert resolve_ruleset(connection).ruleset_id == DEFAULT_RULESET.ruleset_id
    rows = connection.execute(
        "select count(*) from staging.accepted_rulesets where kind = 'concept-mapping'"
    ).fetchone()
    assert rows is not None and rows[0] >= 2, "history of what was in force must survive a revert"


def test_the_pointer_cannot_be_edited(connection) -> None:
    publish_ruleset(connection, DEFAULT_RULESET, note="seed")
    with pytest.raises(psycopg.errors.RaiseException):
        connection.execute("update staging.accepted_rulesets set note = 'x' where kind = 'concept-mapping'")


def test_every_declared_field_states_its_resolution_kind() -> None:
    """Synonym vs stand-in cannot be defaulted: collapsing them is how a 29%-wrong share
    count gets in with nothing looking wrong."""
    for mapping in DEFAULT_RULESET.mappings:
        assert mapping.kind in (ResolutionKind.SYNONYM, ResolutionKind.FALLBACK)
        assert mapping.concepts
