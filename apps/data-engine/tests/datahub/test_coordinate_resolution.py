"""Unit and integration tests for resolve_coordinates (#877 PR-3)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.resolve_coordinates import (
    alias_of,
    parse_alias,
    resolve_coordinates,
    resolve_entity,
)

CUTOFF = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
TODAY = date(2026, 3, 31)


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


def test_parse_alias() -> None:
    assert parse_alias("issuer:lei:5493006MHB84DD0ZWV18", "issuer") == ("lei", "5493006MHB84DD0ZWV18")
    assert parse_alias("issuer:cik:320193", "issuer") == ("cik", "0000320193")
    assert parse_alias("security:cusip:037833100", "instrument") == ("cusip", "037833100")
    assert parse_alias("security:figi:bbg001s5n8v8", "instrument") == ("figi", "BBG001S5N8V8")
    assert parse_alias("listing:xnas:aapl", "listing") == ("mic-ticker", "XNAS:AAPL")
    assert parse_alias("custom:random:id", "issuer") == ("legacy-id", "custom:random:id")


def test_resolve_entity_and_mint_on_miss(connection) -> None:
    """A new entity is minted deterministically on miss with birth alias and legacy-id."""
    tag = uuid.uuid4().hex[:8]
    lei_val = f"549300{tag.upper().zfill(12)}18"
    raw_issuer = f"issuer:lei:{lei_val}"
    issuer_uuid = resolve_entity(connection, raw_issuer, "issuer", as_of=TODAY, known_at=CUTOFF)
    assert isinstance(issuer_uuid, uuid.UUID)

    # Resolving again returns the identical UUID
    issuer_uuid_second = resolve_entity(connection, raw_issuer, "issuer", as_of=TODAY, known_at=CUTOFF)
    assert issuer_uuid == issuer_uuid_second

    # Check alias_of retrieves the LEI value
    val = alias_of(connection, issuer_uuid, "lei", valid_at=TODAY, known_at=CUTOFF)
    assert val == lei_val


def test_goog_and_googl_share_issuer_but_distinct_instruments(connection) -> None:
    """GOOG and GOOGL share the same issuer entity, but have distinct instrument entities."""
    tag = uuid.uuid4().hex[:8]
    raw_issuer = f"issuer:lei:GOOGLE{tag}01234567"
    raw_goog = f"security:cusip:GOOG{tag}1"
    raw_googl = f"security:cusip:GOOG{tag}2"
    raw_list_goog = f"listing:xnas:goog{tag}"
    raw_list_googl = f"listing:xnas:googl{tag}"

    instruments = [
        [raw_issuer, raw_goog, raw_list_goog, f"GOOG{tag}"],
        [raw_issuer, raw_googl, raw_list_googl, f"GOOGL{tag}"],
    ]
    resolved = resolve_coordinates(connection, instruments, as_of=TODAY, known_at=CUTOFF)

    goog_trio = resolved[raw_list_goog]
    googl_trio = resolved[raw_list_googl]

    # Both share the same issuer UUID
    assert goog_trio[0] == googl_trio[0]
    # But different instrument UUIDs
    assert goog_trio[1] != googl_trio[1]
    # And different listing UUIDs
    assert goog_trio[2] != googl_trio[2]


def test_resolve_coordinates_connection_none_fallback() -> None:
    """When connection is None, resolve_coordinates returns raw inputs untouched."""
    instruments = [
        ["issuer:lei:X", "security:cusip:Y", "listing:xnas:aapl", "AAPL"],
    ]
    resolved = resolve_coordinates(None, instruments, as_of=TODAY, known_at=CUTOFF)
    assert resolved["listing:xnas:aapl"] == ("issuer:lei:X", "security:cusip:Y", "listing:xnas:aapl", "AAPL")


def test_aapl_resolves_identically_across_topt_and_qqq(connection) -> None:
    """AAPL listing coordinate resolves to the identical listing UUID regardless of input universe source."""
    aapl_lei = "HWUPKR0MPOU8FGXBT394"
    aapl_cik = "0000320193"
    aapl_cusip = "037833100"
    aapl_figi = "BBG001S5N8V8"
    aapl_listing = "listing:xnas:aapl"

    topt_aapl = [f"issuer:lei:{aapl_lei}", f"security:cusip:{aapl_cusip}", aapl_listing, "AAPL"]
    qqq_aapl = [f"issuer:cik:{aapl_cik}", f"security:figi:{aapl_figi}", aapl_listing, "AAPL"]

    topt_res = resolve_coordinates(connection, [topt_aapl], as_of=TODAY, known_at=CUTOFF)
    qqq_res = resolve_coordinates(connection, [qqq_aapl], as_of=TODAY, known_at=CUTOFF)

    assert topt_res[aapl_listing][2] == qqq_res[aapl_listing][2]


def test_plan_and_persist_coordinates_are_uuids(connection) -> None:
    """plan_and_persist produces canonical UUIDs for all coordinates."""
    from data_engine.datahub.production_topt.composition import plan_and_persist

    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-uuid-coords")
    assert len(plan.coordinates) == 21
    for _subject_id, (issuer_id, instrument_id, listing_id, ticker) in plan.coordinates.items():
        assert uuid.UUID(issuer_id)
        assert uuid.UUID(instrument_id)
        assert uuid.UUID(listing_id)
        assert len(ticker) > 0


def test_capture_entity_refs_persisted(connection) -> None:
    """Captured observations write lineage into staging.capture_entity_refs side table."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parents[1]))
    from production_topt.test_persistence import _capture

    plan = _capture(connection, version="test-refs-persist")
    refs = connection.execute(
        """
        select role, scheme, value, entity_id
        from staging.capture_entity_refs
        where observation_id in (
            select observation_id from staging.capture_observation_obligations
            where capture_obligation_id in (
                select obligation_id from raw.capture_obligations where run_id = %s
            )
        )
        """,
        (plan.run_id,),
    ).fetchall()
    assert len(refs) > 0
    roles = {r[0] for r in refs}
    assert {"issuer", "instrument", "listing"} <= roles
