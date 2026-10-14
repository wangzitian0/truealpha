"""#496: the issuer CIK predecessor registry (migration 0037) and its
resolution precedence in planning. Skips without a local Postgres."""

import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.composition import predecessor_ciks

_XOM_ISSUER = "issuer:lei:J3WHBG0MTS7O8ZVMDC91"
_XOM_LISTING = "listing:xnys:xom"


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_seeded_registry_row_resolves_xom(connection) -> None:
    resolved = predecessor_ciks(connection, [_XOM_LISTING], {_XOM_LISTING: _XOM_ISSUER})
    assert resolved.get(_XOM_LISTING) == 34088, "the owner-signed 0037 seed must resolve XOM"


def test_registry_rows_are_append_only_and_signed(connection) -> None:
    reason, approver = connection.execute(
        "select reason, approved_by from staging.issuer_cik_predecessors where issuer_id = %s",
        (_XOM_ISSUER,),
    ).fetchone()
    assert "2115436" in reason and "zitian" in approver
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update staging.issuer_cik_predecessors set predecessor_cik = 1 where issuer_id = %s",
            (_XOM_ISSUER,),
        )


def test_unregistered_issuer_resolves_nothing(connection) -> None:
    resolved = predecessor_ciks(
        connection, ["listing:xnas:aapl"], {"listing:xnas:aapl": "issuer:lei:HWUPKR0MPOU8FGXBT394"}
    )
    assert "listing:xnas:aapl" not in resolved, "no registry row and no lineage -> honest absence"


def test_canonical_uuid_resolves_signed_predecessor_via_entity_aliases(connection, monkeypatch) -> None:
    """#1263: canonical_universe hands `predecessor_ciks` and `build_route` canonical entity UUIDs,
    while `staging.issuer_cik_predecessors` keys ExxonMobil by `issuer:lei:J3WHBG0MTS7O8ZVMDC91`."""
    from datetime import UTC, date, datetime

    from data_engine.datahub.production_topt import issuer_registry
    from data_engine.datahub.production_topt.sec_financial_adapter import OperatingBranch, build_route
    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
    from data_engine.datahub.resolve_coordinates import resolve_entity

    monkeypatch.setattr(
        issuer_registry,
        "resolve_issuer_classifications",
        lambda ciks: {
            cik: issuer_registry.IssuerClassification(
                operating_branch=OperatingBranch.NON_FINANCIAL,
                revenue_proxy_allowed=False,
            )
            for cik in ciks.values()
        },
    )

    known_at = datetime(2024, 1, 1, tzinfo=UTC)
    xom_uuid = str(resolve_entity(connection, _XOM_ISSUER, "issuer", as_of=known_at.date(), known_at=known_at))
    xom_instrument_uuid = str(
        resolve_entity(
            connection, "instrument:equity:xom:common", "instrument", as_of=known_at.date(), known_at=known_at
        )
    )
    xom_listing_uuid = str(
        resolve_entity(connection, _XOM_LISTING, "listing", as_of=known_at.date(), known_at=known_at)
    )
    connection.execute(
        """
        insert into staging.entity_aliases (
            entity_id, scheme, value, valid_from, valid_to, transaction_time, source,
            raw_ref, method, confidence, mapping_version
        ) values (%s::uuid, 'cik', '0002115436', '-infinity'::date, null, least(%s, clock_timestamp()), 'datahub.capture', %s, 'asserted', 1.0, 'datahub:v1')
        on conflict do nothing
        """,
        (xom_uuid, known_at, f"mint:{xom_uuid}:cik"),
    )

    resolved = predecessor_ciks(connection, [xom_listing_uuid], {xom_listing_uuid: xom_uuid})
    assert resolved.get(xom_listing_uuid) == 34088
    assert resolved[xom_listing_uuid].signed is True

    cutoff = datetime(2026, 9, 2, tzinfo=UTC)
    context = RouteContext(
        cutoff=cutoff,
        cutoff_date=date(2026, 9, 2),
        price_cutoff_date=date(2026, 9, 2),
        partition_start=datetime(2026, 9, 1, tzinfo=UTC),
        universe_published_at=None,
        coordinates={
            xom_listing_uuid: (xom_uuid, xom_instrument_uuid, xom_listing_uuid, "XOM"),
        },
        connection=connection,
    )
    cell = RouteCell(
        work_item_id="wi:xom:financial-fact",
        semantic_type="financial-fact",
        issuer_id=xom_uuid,
        instrument_id=xom_instrument_uuid,
        listing_id=xom_listing_uuid,
        ticker="XOM",
    )

    adapter = build_route(context, [cell])
    target = adapter._targets[cell.work_item_id]
    assert target.cik == 2115436
    assert target.predecessor_cik == 34088
    assert target.predecessor_signed is True


def test_alphabet_uuid_constants_match_migrated_entity_aliases(connection) -> None:
    """#1265: verify the Alphabet listing/instrument UUIDs in `canonical_issuer.py` match
    `resolve_entity` (`staging.entity_mint`) in the migrated schema so UUID-plane
    preference ordering never silently drifts."""
    from datetime import UTC, date, datetime

    from data_engine.datahub.canonical_issuer import (
        PRIMARY_INSTRUMENT_IDS,
        PRIMARY_LISTING_IDS,
        SECONDARY_INSTRUMENT_IDS,
        SECONDARY_LISTING_IDS,
        instrument_sort_key,
        listing_sort_key,
    )
    from data_engine.datahub.resolve_coordinates import resolve_entity

    as_of = date(2026, 9, 2)
    known_at = datetime(2026, 9, 2, tzinfo=UTC)
    googl_listing_uuid = str(
        resolve_entity(connection, "listing:xnas:googl", "listing", as_of=as_of, known_at=known_at)
    )
    goog_listing_uuid = str(resolve_entity(connection, "listing:xnas:goog", "listing", as_of=as_of, known_at=known_at))
    googl_inst_uuid = str(
        resolve_entity(connection, "security:cusip:02079K305", "instrument", as_of=as_of, known_at=known_at)
    )
    goog_inst_uuid = str(
        resolve_entity(connection, "security:cusip:02079K107", "instrument", as_of=as_of, known_at=known_at)
    )

    assert googl_listing_uuid in PRIMARY_LISTING_IDS
    assert goog_listing_uuid in SECONDARY_LISTING_IDS
    assert googl_inst_uuid in PRIMARY_INSTRUMENT_IDS
    assert goog_inst_uuid in SECONDARY_INSTRUMENT_IDS
    assert min([goog_listing_uuid, goog_listing_uuid, googl_listing_uuid], key=listing_sort_key) == googl_listing_uuid
    assert min([goog_inst_uuid, googl_inst_uuid], key=instrument_sort_key) == googl_inst_uuid
