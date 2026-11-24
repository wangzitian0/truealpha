"""Unit and integration tests for resolve_coordinates (#877 PR-3)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.canonical_issuer import expand_issuer_alias_ids
from data_engine.datahub.resolve_coordinates import (
    MERGE_SET_CTE,
    _merge,
    alias_of,
    parse_alias,
    resolve_coordinates,
    resolve_entity,
)

CUTOFF = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
TODAY = date(2026, 3, 31)
MERGED_AT = datetime(2026, 4, 1, 1, 45, tzinfo=UTC)


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


def _two_spellings() -> tuple[list[str], list[str], str]:
    """One listing spelled as a corpus spells it (LEI + CUSIP) and as a plane list spells it (CIK + FIGI)."""
    number = uuid.uuid4().int
    listing = f"listing:xnys:t{number % 10**10:010d}"
    corpus = [f"issuer:lei:5493{number % 10**14:014d}18", f"security:cusip:{number % 10**8:08d}7", listing, "TST"]
    plane = [f"issuer:cik:{number % 10**10:010d}", f"security:figi:BBG{number % 10**8:08d}3", listing, "TST"]
    return corpus, plane, listing


def _split_listings(connection, listing_uuid: str) -> tuple[int, int]:
    """The count of distinct surviving instruments of the listing, and of distinct surviving issuers of those."""
    return connection.execute(
        """
        with instruments as (
            select relation.from_entity_id as id,
                   staging.entity_survivor(relation.from_entity_id, 'infinity'::timestamptz) as survivor
            from staging.entity_relations relation
            where relation.relation_type = 'listed_as' and relation.to_entity_id = %s::uuid
        )
        select (select count(distinct survivor) from instruments),
               (select count(distinct staging.entity_survivor(relation.from_entity_id, 'infinity'::timestamptz))
                from staging.entity_relations relation
                where relation.relation_type = 'issues'
                  and relation.to_entity_id in (select id from instruments union select survivor from instruments))
        """,
        (listing_uuid,),
    ).fetchone()


def test_two_spellings_of_one_listing_resolve_to_one_issuer_and_one_instrument(connection) -> None:
    """#1798: a listing has one issuer and one instrument in each universe. The second spelling
    becomes an alias of the entities that the first spelling named."""
    corpus, plane, listing = _two_spellings()
    first = resolve_coordinates(connection, [corpus], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    second = resolve_coordinates(connection, [plane], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    assert second == first
    assert _split_listings(connection, first[2]) == (1, 1)
    assert str(resolve_entity(connection, plane[0], "issuer", as_of=TODAY, known_at=CUTOFF)) == first[0]
    assert str(resolve_entity(connection, plane[1], "instrument", as_of=TODAY, known_at=CUTOFF)) == first[1]


def test_a_spelling_that_already_names_other_entities_is_merged_into_the_listing(connection) -> None:
    """#1798: the plane spelling has its own entities before the first capture (the universe head
    minted them). The capture merges them into the entities of the listing."""
    corpus, plane, listing = _two_spellings()
    first = resolve_coordinates(connection, [corpus], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    plane_issuer = resolve_entity(connection, plane[0], "issuer", as_of=TODAY, known_at=CUTOFF)
    plane_instrument = resolve_entity(connection, plane[1], "instrument", as_of=TODAY, known_at=CUTOFF)
    assert str(plane_issuer) != first[0] and str(plane_instrument) != first[1]

    second = resolve_coordinates(connection, [plane], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    assert second == first
    survivors = connection.execute(
        "select staging.entity_survivor(%s, 'infinity'::timestamptz)::text,"
        " staging.entity_survivor(%s, 'infinity'::timestamptz)::text",
        (plane_issuer, plane_instrument),
    ).fetchone()
    assert survivors == (first[0], first[1])
    again = resolve_coordinates(connection, [plane], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    assert again == first, "a second resolution writes no second merge and returns the same trio"


def test_a_listing_that_is_already_split_is_made_one_at_the_next_resolution(connection) -> None:
    """#1798: staging holds a listing with two instruments and two issuers. The next resolution
    merges the newer claimants into the oldest claimant."""
    from data_engine.datahub.resolve_coordinates import _assert_relation

    corpus, plane, listing = _two_spellings()
    first = resolve_coordinates(connection, [corpus], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    plane_issuer = resolve_entity(connection, plane[0], "issuer", as_of=TODAY, known_at=CUTOFF)
    plane_instrument = resolve_entity(connection, plane[1], "instrument", as_of=TODAY, known_at=CUTOFF)
    later = datetime(2026, 4, 1, 1, 45, tzinfo=UTC)
    _assert_relation(connection, "issues", plane_issuer, plane_instrument, known_at=later)
    _assert_relation(connection, "listed_as", plane_instrument, uuid.UUID(first[2]), known_at=later)
    assert _split_listings(connection, first[2]) == (2, 2)

    healed = resolve_coordinates(connection, [corpus], as_of=TODAY, known_at=later, refresh_mart=False)[listing]
    assert healed == first
    assert _split_listings(connection, first[2]) == (1, 1)


def test_plan_and_persist_coordinates_are_uuids(connection) -> None:
    """plan_and_persist produces canonical UUIDs for all coordinates."""
    from data_engine.datahub.capture_core.composition import plan_and_persist

    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-uuid-coords")
    assert len(plan.coordinates) == 21
    for _subject_id, (issuer_id, instrument_id, listing_id, ticker) in plan.coordinates.items():
        assert uuid.UUID(issuer_id)
        assert uuid.UUID(instrument_id)
        assert uuid.UUID(listing_id)
        assert len(ticker) > 0


def test_resolve_coordinates_asserts_issuer_display_name_aliases_for_all_topt_members(connection) -> None:
    """#1517, #1518: `resolve_coordinates` asserts a `scheme = 'name'` alias on every TOPT issuer entity
    without any hardcoded Python dictionary."""
    from data_engine.datahub import resolve_coordinates as rc
    from data_engine.datahub.capture_core.composition import plan_and_persist

    assert not hasattr(rc, "_CANONICAL_ISSUER_NAMES") and not hasattr(rc, "canonical_issuer_name")
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-xnys-names")
    seen_issuers: dict[str, str] = {}
    for _subject_id, (issuer_id, _instrument_id, _listing_id, ticker) in plan.coordinates.items():
        name_val = alias_of(connection, issuer_id, "name", valid_at=TODAY, known_at=CUTOFF)
        assert name_val is not None and name_val != ticker and name_val != issuer_id
        seen_issuers[ticker] = name_val
    assert seen_issuers["JPM"] == "JPMorgan Chase & Co."
    assert seen_issuers["LLY"] == "Eli Lilly and Company"
    assert seen_issuers["BRK.B"] == "Berkshire Hathaway Inc."
    assert seen_issuers["WMT"] == "Walmart Inc."


def test_resolve_coordinates_holds_no_hardcoded_issuer_name_dictionary() -> None:
    """#1518: `resolve_coordinates` resolves TOPT issuer names from the packaged corpus without a hardcoded Python dict,
    and `_holding_display_name` returns None rather than an ISIN or bare ticker when both names are blank."""
    from unittest.mock import MagicMock

    from data_engine.datahub import resolve_coordinates as rc
    from data_engine.datahub.capture_core.holdings_enrichment import _holding_display_name

    assert not hasattr(rc, "_CANONICAL_ISSUER_NAMES") and not hasattr(rc, "canonical_issuer_name")
    assert rc._corpus_issuer_names()["JPM"] == "JPMorgan Chase & Co."
    conn_empty = MagicMock(**{"execute.return_value.fetchone.return_value": None})
    assert _holding_display_name(conn_empty, isin="US0378331005", ticker="AAPL", listing_name="  ") is None
    assert conn_empty.execute.call_args[0][1] == ("US0378331005", "AAPL", "US0378331005")
    conn_named = MagicMock(**{"execute.return_value.fetchone.return_value": ("Apple Inc.",)})
    assert _holding_display_name(conn_named, isin="US0378331005", ticker="AAPL", listing_name=None) == "Apple Inc."
    assert _holding_display_name(conn_empty, isin="US0378331005", ticker="AAPL", listing_name="APPLE INC") == (
        "APPLE INC"
    )


def test_a_merge_that_a_retraction_ended_is_not_written_again(connection) -> None:
    """#1798: a retraction of the `superseded_by` row says the two entities are different. The
    next resolution of the same spelling stops with the listing and both entity ids."""
    corpus, plane, listing = _two_spellings()
    first = resolve_coordinates(connection, [corpus], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)[listing]
    plane_issuer = resolve_entity(connection, plane[0], "issuer", as_of=TODAY, known_at=CUTOFF)
    resolve_coordinates(connection, [plane], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)
    connection.execute(
        "insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref)"
        " select relation_id, %s, 'wrong-listing-id', %s, 'test', 'test' from staging.entity_relations"
        " where relation_type in ('same_as', 'superseded_by') and from_entity_id = %s and to_entity_id = %s::uuid",
        (TODAY, CUTOFF, plane_issuer, first[0]),
    )
    with pytest.raises(ValueError, match=f"issuer {plane_issuer} was separated from {first[0]}"):
        resolve_coordinates(connection, [plane], as_of=TODAY, known_at=CUTOFF, refresh_mart=False)


def _issuer(connection, raw_id: str) -> tuple[uuid.UUID, str]:
    """Mint an issuer at CUTOFF from `raw_id`. Return its id and the value of its LEI or CIK alias."""
    return resolve_entity(connection, raw_id, "issuer", as_of=TODAY, known_at=CUTOFF), parse_alias(raw_id, "issuer")[1]


def _retract_merge(connection, loser: uuid.UUID, survivor: uuid.UUID, *, known_at: datetime) -> None:
    connection.execute(
        "insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref)"
        " select relation_id, %s, 'wrong-listing-id', %s, 'test', 'test' from staging.entity_relations"
        " where relation_type in ('same_as', 'superseded_by') and from_entity_id = %s and to_entity_id = %s",
        (TODAY, known_at, loser, survivor),
    )


def _merged_pair(connection) -> tuple[uuid.UUID, str, str, uuid.UUID, str, str]:
    """An issuer minted from an LEI, and an issuer minted from a CIK that is merged into it at MERGED_AT.

    Return (survivor, its LEI, its legacy id, loser, its CIK, its legacy id). Only the survivor has
    an LEI and only the loser has a CIK, so each `alias_of` below has one candidate row.
    """
    corpus, plane, _listing = _two_spellings()
    survivor, lei = _issuer(connection, corpus[0])
    loser, cik = _issuer(connection, plane[0])
    _merge(connection, loser, survivor, listing=uuid.uuid4(), known_at=MERGED_AT)
    return survivor, lei, corpus[0], loser, cik, plane[0]


def test_alias_of_reads_the_aliases_of_each_entity_that_is_merged_with_the_asked_entity(connection) -> None:
    """#1829: an alias of the loser is an alias of the survivor, and an alias of the survivor is an alias of the loser."""
    survivor, lei, _, loser, cik, _ = _merged_pair(connection)
    assert alias_of(connection, survivor, "cik", valid_at=TODAY, known_at=MERGED_AT) == cik
    assert alias_of(connection, loser, "lei", valid_at=TODAY, known_at=MERGED_AT) == lei
    assert alias_of(connection, loser, "cik", valid_at=TODAY, known_at=MERGED_AT) == cik
    assert alias_of(connection, survivor, "lei", valid_at=TODAY, known_at=MERGED_AT) == lei


def test_alias_of_stops_reading_the_aliases_of_an_entity_after_its_merge_is_retracted(connection) -> None:
    """#1829: a retraction of the merge, as known at `known_at`, separates the aliases of the two entities again."""
    survivor, lei, _, loser, cik, _ = _merged_pair(connection)
    retracted_at = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
    _retract_merge(connection, loser, survivor, known_at=retracted_at)
    assert alias_of(connection, survivor, "cik", valid_at=TODAY, known_at=MERGED_AT) == cik
    assert alias_of(connection, survivor, "cik", valid_at=TODAY, known_at=retracted_at) is None
    assert alias_of(connection, loser, "lei", valid_at=TODAY, known_at=retracted_at) is None
    assert alias_of(connection, loser, "cik", valid_at=TODAY, known_at=retracted_at) == cik


def test_alias_of_does_not_follow_a_merge_recorded_after_known_at(connection) -> None:
    """#1829: point in time. Before MERGED_AT, the two entities and their aliases are separate."""
    survivor, lei, _, loser, cik, _ = _merged_pair(connection)
    before = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    assert alias_of(connection, survivor, "cik", valid_at=TODAY, known_at=before) is None
    assert alias_of(connection, loser, "lei", valid_at=TODAY, known_at=before) is None
    assert alias_of(connection, survivor, "lei", valid_at=TODAY, known_at=before) == lei
    assert alias_of(connection, loser, "cik", valid_at=TODAY, known_at=before) == cik


def test_expand_issuer_alias_ids_reads_the_legacy_ids_and_lei_of_each_merged_entity(connection) -> None:
    """#1829, review of #1812: the legacy id of a merged-in entity expands the survivor. The legacy id
    and the LEI of the survivor expand the loser."""
    survivor, lei, survivor_legacy, loser, _cik, loser_legacy = _merged_pair(connection)
    expanded = expand_issuer_alias_ids(connection, {"survivor": str(survivor), "loser": str(loser)})
    aliases = {survivor_legacy, loser_legacy, f"issuer:lei:{lei}"}
    assert set(expanded["survivor"]) == aliases | {str(survivor)}
    assert set(expanded["loser"]) == aliases | {str(loser)}


def _remerged_issuer(connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, dict[str, datetime]]:
    """Issuer `moved` is merged into `first` at MERGED_AT. That merge is retracted at `retracted`.
    Then `moved` is merged into `second` with the earlier transaction time `remerged`.

    Between `remerged` and `retracted`, `moved` has two live `superseded_by` edges.
    `staging.entity_survivor` follows only the latest one, so `moved` survives as `second`.
    """
    corpus, plane, _listing = _two_spellings()
    first, _ = _issuer(connection, corpus[0])
    moved, cik = _issuer(connection, plane[0])
    second, _ = _issuer(connection, _two_spellings()[0][0])
    times = {
        "before": datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        "merged": MERGED_AT,
        "remerged": datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
        "both_live": datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
        "retracted": datetime(2026, 4, 3, 0, 0, tzinfo=UTC),
    }
    _merge(connection, moved, first, listing=uuid.uuid4(), known_at=times["merged"])
    _retract_merge(connection, moved, first, known_at=times["retracted"])
    _merge(connection, moved, second, listing=uuid.uuid4(), known_at=times["remerged"])
    return first, moved, second, cik, times


def test_alias_of_follows_only_the_latest_live_merge_of_an_entity(connection) -> None:
    """#1829: an entity with two live `superseded_by` edges is in the merge set of the latest
    target only, as `staging.entity_survivor` decides."""
    first, _moved, second, cik, times = _remerged_issuer(connection)
    assert alias_of(connection, first, "cik", valid_at=TODAY, known_at=times["merged"]) == cik
    assert alias_of(connection, second, "cik", valid_at=TODAY, known_at=times["merged"]) is None
    assert alias_of(connection, first, "cik", valid_at=TODAY, known_at=times["both_live"]) is None
    assert alias_of(connection, second, "cik", valid_at=TODAY, known_at=times["both_live"]) == cik


def test_merge_set_is_the_set_of_entities_with_the_survivor_of_the_asked_entity(connection) -> None:
    """#1829: for each entity and each known_at, `merge_set` holds the entities whose
    `staging.entity_survivor` is the survivor of the asked entity: the predicate before #1829."""
    first, moved, second, _cik, times = _remerged_issuer(connection)
    entities = [first, moved, second]
    survivors_of_moved = set()
    for known_at in times.values():
        survivors = dict(
            connection.execute(
                "select id, staging.entity_survivor(id, %s) from unnest(%s::uuid[]) as id", (known_at, entities)
            ).fetchall()
        )
        survivors_of_moved.add(survivors[moved])
        for asked in entities:
            merge_set = {
                row[0]
                for row in connection.execute(
                    f"with recursive {MERGE_SET_CTE} select entity_id from merge_set",
                    {"entity_ids": [asked], "known_at": known_at},
                ).fetchall()
            }
            assert merge_set == {e for e in entities if survivors[e] == survivors[asked]}, (asked, known_at)
    assert survivors_of_moved == {moved, first, second}, "the times cover each survivor of `moved`"
