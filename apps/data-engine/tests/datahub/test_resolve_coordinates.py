"""Real-Postgres coordinate resolution tests for temporal claimant bounds (#1798)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest
from data_engine.datahub.resolve_coordinates import resolve_coordinates

pytestmark = pytest.mark.deps


def test_closed_historical_listing_relation_does_not_merge_new_issuer(connection: psycopg.Connection[Any]) -> None:
    """A closed (`valid_to <= as_of`, via row `valid_to` or retraction) relation must not merge a later issuer."""
    old_as_of = date(2020, 1, 15)
    old_known = datetime(2020, 1, 15, tzinfo=UTC)
    close_date = date(2023, 1, 1)
    new_as_of = date(2024, 2, 1)
    new_known = datetime(2024, 2, 1, tzinfo=UTC)

    listing = "listing:xnas:t1798"
    old_trio = ("issuer:cik:0000001798", "security:figi:bbg001798old", listing, "T1798")
    bounded_trio = ("issuer:cik:0000004798", "security:figi:bbg001798bnd", listing, "T1798")
    new_trio = ("issuer:cik:0000002798", "security:figi:bbg001798new", listing, "T1798")
    active_trio = ("issuer:cik:0000003798", "security:figi:bbg001798act", listing, "T1798")

    old_issuer, old_inst, listing_uuid, _ = resolve_coordinates(
        connection, [old_trio], as_of=old_as_of, known_at=old_known, refresh_mart=False
    )[listing]
    connection.execute(
        "insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref)"
        " select relation_id, %s, 'ticker_reassigned', %s, 'datahub.test', 'retract:1798'"
        " from staging.entity_relations"
        " where (relation_type = 'listed_as' and to_entity_id = %s)"
        "    or (relation_type = 'issues' and from_entity_id = %s and to_entity_id = %s)",
        (close_date, new_known, listing_uuid, old_issuer, old_inst),
    )

    # Also test a relation inserted directly with `valid_to = close_date`.
    bnd_issuer_row = connection.execute(
        "select staging.entity_mint('issuer', 'legacy-id', %s, 'datahub.test')",
        (bounded_trio[0],),
    ).fetchone()
    bnd_inst_row = connection.execute(
        "select staging.entity_mint('instrument', 'legacy-id', %s, 'datahub.test')",
        (bounded_trio[1],),
    ).fetchone()
    assert bnd_issuer_row is not None and bnd_inst_row is not None
    bnd_issuer, bnd_inst = bnd_issuer_row[0], bnd_inst_row[0]
    connection.execute(
        "insert into staging.entity_relations"
        " (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, valid_to,"
        "  transaction_time, source, raw_ref, method, confidence, mapping_version)"
        " values"
        " (staging.entity_relation_uuid('listed_as', %s, %s, %s, %s, 'datahub.test', 'asserted'),"
        "  'listed_as', %s, %s, %s, %s, %s, 'datahub.test', 'rel:bnd-listed', 'asserted', 1.0, 'datahub:v1'),"
        " (staging.entity_relation_uuid('issues', %s, %s, %s, %s, 'datahub.test', 'asserted'),"
        "  'issues', %s, %s, %s, %s, %s, 'datahub.test', 'rel:bnd-issues', 'asserted', 1.0, 'datahub:v1')",
        (
            bnd_inst,
            listing_uuid,
            old_as_of,
            old_known,
            bnd_inst,
            listing_uuid,
            old_as_of,
            close_date,
            old_known,
            bnd_issuer,
            bnd_inst,
            old_as_of,
            old_known,
            bnd_issuer,
            bnd_inst,
            old_as_of,
            close_date,
            old_known,
        ),
    )

    new_issuer, new_inst, _, _ = resolve_coordinates(
        connection, [new_trio], as_of=new_as_of, known_at=new_known, refresh_mart=False
    )[listing]
    assert new_issuer not in {old_issuer, str(bnd_issuer)}
    assert new_inst not in {old_inst, str(bnd_inst)}

    # Active (`valid_to IS NULL`) claimants at `new_as_of` still collapse onto the current owner.
    merged_issuer, merged_inst, _, _ = resolve_coordinates(
        connection, [active_trio], as_of=new_as_of, known_at=new_known, refresh_mart=False
    )[listing]
    assert merged_issuer == new_issuer
    assert merged_inst == new_inst
