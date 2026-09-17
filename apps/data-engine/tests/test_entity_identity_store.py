"""#877 PR-2: the entity identity store (migration 20260917T0412) and its backfill (20260917T0604,
run by the Dagster job `entity_identity_backfill`, never at boot).

The backfill's inputs are what the deployed pipeline stores: normalized observation
payloads (seeded here through the same capture repository the executor writes with, once
for the checked-in TOPT corpus and once for a CIK/FIGI-keyed plane corpus), N-PORT holding
lines, and the knowledge graph's ISIN identifiers (written through
`factors.shared.entity_resolution`, the writer holdings enrichment uses). The function under
test is the one the migration runs on every boot.

Skips without a local Postgres; CI runs it against the migrated service container.
Everything happens in one transaction that is rolled back, except the determinism test,
which builds (and drops) fresh databases of its own.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from factors.shared import entity_resolution as er
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_contracts.common import canonical_sha256

sys.path.insert(0, str(Path(__file__).parent))
from production_topt.test_materialization import CUTOFF, _seed_complete_production_run  # noqa: E402

MIGRATIONS = Path(__file__).parents[3] / "db" / "migrations"
STORE_MIGRATION = MIGRATIONS / "20260917T0412_datahub_entity_identity_store.sql"
BACKFILL_MIGRATION = MIGRATIONS / "20260917T0604_datahub_entity_backfill_job.sql"

# The id rule, restated independently of the SQL: UUIDv5 of `scheme:value` under the kind's
# namespace, which is UUIDv5 of `kind:<kind>` under the root.
ROOT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/blob/main/docs/entity-identity.md"
)


def expected_entity_id(kind: str, scheme: str, value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.uuid5(ROOT_NAMESPACE, f"kind:{kind}"), f"{scheme}:{value}")


# What the store knows "now" and on which real-world day, for reads that are not about time.
NOW = datetime(2100, 1, 1, tzinfo=UTC)
TODAY = date(2099, 12, 31)

# N-PORT filed and OpenFIGI answered after the TOPT capture above (CUTOFF), so the merge
# test can read the store as known in between.
NPORT_FILED = datetime(2026, 5, 28, tzinfo=UTC)
FIGI_MAPPED = datetime(2026, 5, 29, tzinfo=UTC)
BETWEEN = datetime(2026, 5, 1, tzinfo=UTC)
ASSERTED = datetime(2026, 1, 2, tzinfo=UTC)

# Alphabet's two share classes and Apple, as N-PORT reports them (LEI, CUSIP, ISIN) and as
# the plane universe keys them (CIK, share-class FIGI).
AAPL = {
    "lei": "HWUPKR0MPOU8FGXBT394",
    "cusip": "037833100",
    "isin": "US0378331005",
    "cik": "0000320193",
    "figi": "bbg001s5n8v8",
    "listing": "listing:xnas:aapl",
}
GOOG = {
    "lei": "5493006MHB84DD0ZWV18",
    "cusip": "02079K107",
    "isin": "US02079K1079",
    "cik": "0001652044",
    "figi": "bbg009s3nb21",
    "listing": "listing:xnas:goog",
}
GOOGL = {**GOOG, "cusip": "02079K305", "isin": "US02079K3059", "figi": "bbg009s39jy5", "listing": "listing:xnas:googl"}


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


def _plane_corpus() -> dict:
    """A CIK/FIGI-keyed universe sharing three listings with TOPT, built the way
    `universe_plane.build_denominator` builds one."""
    fields = ["issuer_id", "instrument_id", "listing_id", "ticker"]
    instruments = [
        [
            f"issuer:cik:{ids['cik']}",
            f"security:figi:{ids['figi']}",
            ids["listing"],
            ids["listing"].split(":")[2].upper(),
        ]
        for ids in (AAPL, GOOG, GOOGL)
    ]
    report_date = CUTOFF.date().isoformat()
    return {
        "topt_denominator": {
            "universe_id": f"universe:entity-plane-{report_date}",
            "report_date": report_date,
            "instrument_count": len(instruments),
            "issuer_count": 2,
            "instrument_tuple_fields": fields,
            "instruments": instruments,
            "instrument_mapping_sha256": canonical_sha256({"fields": fields, "instruments": instruments}),
        }
    }


def _seed_nport_crosswalk(connection, *holdings: dict) -> None:
    """One N-PORT line per holding, and the knowledge graph resolving its ISIN to the
    issuer's CIK entity through a newer OpenFIGI vintage plus the same_as hop, which is the
    shape holdings enrichment leaves in production."""
    fund = "etf:series:S000877002"
    er.ensure_entity(connection, fund, "etf", "entity-store test fund")
    for holding in holdings:
        minted = f"company:isin:{holding['isin']}"
        issuer = f"issuer:cik:{holding['cik']}"
        er.ensure_entity(connection, minted, "company", holding["isin"])
        er.ensure_entity(connection, issuer, "company", holding["cik"])
        connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time, recorded_at,
                 cusip, isin, lei, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 1, 1, %s)
            """,
            (
                fund,
                minted,
                f"holding {holding['cusip']}",
                date(2026, 3, 31),
                NPORT_FILED,
                NPORT_FILED,
                holding["cusip"],
                holding["isin"],
                holding["lei"],
                "raw.fetches:0",
            ),
        )
        er.assert_identifier(
            connection,
            entity_id=minted,
            source="nport",
            identifier_type="isin",
            identifier_value=holding["isin"],
            confidence=1.0,
            transaction_time=NPORT_FILED,
            valid_from="2026-03-31",
            raw_ref="raw.fetches:0",
        )
        er.assert_identifier(
            connection,
            entity_id=issuer,
            source="openfigi",
            identifier_type="isin",
            identifier_value=holding["isin"],
            confidence=0.98,
            transaction_time=FIGI_MAPPED,
            valid_from="2026-05-29",
            raw_ref="raw.fetches:1",
        )
        er.add_edge(
            connection,
            from_id=minted,
            to_id=issuer,
            relation_type="same_as",
            confidence=0.98,
            source="openfigi",
            transaction_time=FIGI_MAPPED,
            valid_from="2026-05-29",
            raw_ref="raw.fetches:1",
        )


def _backfill(connection) -> dict:
    row = connection.execute("select staging.entity_backfill()").fetchone()
    assert row is not None
    return row[0]


def _resolve(connection, scheme: str, value: str, *, valid_at: date = TODAY, known_at: datetime = NOW):
    row = connection.execute(
        "select staging.entity_resolve(%s, %s, %s, %s)", (scheme, value, valid_at, known_at)
    ).fetchone()
    assert row is not None
    return row[0]


def _store_counts(connection) -> tuple[int, ...]:
    row = connection.execute(
        """
        select (select count(*) from staging.entities),
               (select count(*) from staging.entity_aliases),
               (select count(*) from staging.entity_relations)
        """
    ).fetchone()
    assert row is not None
    return tuple(row)


def _edge(connection, relation_type: str, from_id, to_id) -> bool:
    """A fact edge between the entities the two ends survive as; an identity edge
    (same_as, superseded_by) between exactly the two entities named."""
    row = connection.execute(
        """
        select exists (
            select 1
            from staging.entity_relations relation
            join staging.entity_relation_types relation_type using (relation_type)
            where relation.relation_type = %(type)s
              and case when relation_type.is_identity
                       then relation.from_entity_id = %(from)s and relation.to_entity_id = %(to)s
                       else staging.entity_survivor(relation.from_entity_id, 'infinity') = %(from)s
                        and staging.entity_survivor(relation.to_entity_id, 'infinity') = %(to)s
                  end)
        """,
        {"type": relation_type, "from": from_id, "to": to_id},
    ).fetchone()
    assert row is not None
    return bool(row[0])


def _seed_both_universes(connection) -> None:
    _seed_complete_production_run(connection)
    _seed_complete_production_run(connection, corpus=_plane_corpus())


def test_every_id_form_of_one_company_resolves_to_one_entity(connection) -> None:
    _seed_both_universes(connection)
    _seed_nport_crosswalk(connection, AAPL, GOOG, GOOGL)

    written = _backfill(connection)
    assert written["failed"] == [], written

    issuer_forms = {
        ("legacy-id", f"issuer:lei:{AAPL['lei']}"),
        ("legacy-id", f"issuer:cik:{AAPL['cik']}"),
        ("lei", AAPL["lei"]),
        ("cik", AAPL["cik"].lstrip("0")),  # the resolver takes an unpadded CIK too
    }
    issuers = {_resolve(connection, scheme, value) for scheme, value in issuer_forms}
    assert len(issuers) == 1 and None not in issuers, issuers
    (apple,) = issuers

    instrument_forms = {
        ("legacy-id", f"security:cusip:{AAPL['cusip']}"),
        ("legacy-id", f"security:figi:{AAPL['figi']}"),
        ("cusip", AAPL["cusip"]),
        ("figi", AAPL["figi"]),
        ("isin", AAPL["isin"]),
    }
    instruments = {_resolve(connection, scheme, value) for scheme, value in instrument_forms}
    assert len(instruments) == 1 and None not in instruments, instruments
    (apple_share,) = instruments
    assert apple_share != apple

    listing = _resolve(connection, "legacy-id", AAPL["listing"])
    assert listing == _resolve(connection, "mic-ticker", "xnas:aapl")
    assert listing not in (None, apple, apple_share)
    assert _edge(connection, "issues", apple, apple_share)
    assert _edge(connection, "listed_as", apple_share, listing)

    # Alphabet: one issuer, two share classes that stay two instruments.
    alphabet = _resolve(connection, "lei", GOOG["lei"])
    assert alphabet == _resolve(connection, "legacy-id", f"issuer:cik:{GOOG['cik']}")
    class_c = _resolve(connection, "cusip", GOOG["cusip"])
    class_a = _resolve(connection, "cusip", GOOGL["cusip"])
    assert class_c == _resolve(connection, "figi", GOOG["figi"])
    assert class_a == _resolve(connection, "figi", GOOGL["figi"])
    assert class_c != class_a
    assert _edge(connection, "issues", alphabet, class_c) and _edge(connection, "issues", alphabet, class_a)

    # Every id is the UUIDv5 of the entity's birth alias: the pipeline id known first, then
    # the smallest. Both universes' ids became knowable at the same instant here.
    assert apple == expected_entity_id("issuer", "legacy-id", f"issuer:cik:{AAPL['cik']}")
    assert apple_share == expected_entity_id("instrument", "legacy-id", f"security:cusip:{AAPL['cusip']}")
    assert listing == expected_entity_id("listing", "legacy-id", AAPL["listing"])

    # A TOPT issuer nothing crosswalks is still an entity, and the plan says why it is alone.
    jpm_issuer = _resolve(connection, "legacy-id", "issuer:lei:8I5DZWZKVSZI1NUHU748")
    assert jpm_issuer is not None and jpm_issuer not in issuers
    states = dict(
        connection.execute(
            """
            select legacy_id, link_state from staging.entity_backfill_plan
            where claim = 'alias' and scheme = 'legacy-id' and legacy_id = any(%s)
            """,
            (
                [
                    f"issuer:lei:{AAPL['lei']}",
                    "issuer:lei:8I5DZWZKVSZI1NUHU748",
                    "security:cusip:46625H100",
                    f"security:cusip:{AAPL['cusip']}",
                ],
            ),
        ).fetchall()
    )
    assert states == {
        f"issuer:lei:{AAPL['lei']}": "linked",
        "issuer:lei:8I5DZWZKVSZI1NUHU748": "unlinked:no-crosswalk",
        "security:cusip:46625H100": "unlinked:no-counterpart",
        f"security:cusip:{AAPL['cusip']}": "linked",
    }


def test_backfill_is_idempotent(connection) -> None:
    _seed_both_universes(connection)
    _seed_nport_crosswalk(connection, AAPL, GOOG, GOOGL)

    first = _backfill(connection)
    assert first["minted"] and first["aliases"] and first["relations"], first
    after_first = _store_counts(connection)
    apple = _resolve(connection, "lei", AAPL["lei"])

    second = _backfill(connection)
    assert second["minted"] == {} and second["aliases"] == {} and second["relations"] == {}, second
    assert second["failed"] == []
    assert _store_counts(connection) == after_first
    assert _resolve(connection, "lei", AAPL["lei"]) == apple


def test_a_later_proof_merges_by_adding_edges_and_rewrites_nothing(connection) -> None:
    _seed_both_universes(connection)

    # Before any N-PORT line: the LEI and the CIK are two issuers, and the listing both
    # universes name is held back rather than given to either instrument.
    before = _backfill(connection)
    assert before["held_back"].get("conflict:listing-claimed-by-several-instruments", 0) >= 2, before
    by_lei = _resolve(connection, "lei", AAPL["lei"])
    by_cik = _resolve(connection, "cik", AAPL["cik"])
    assert by_lei is not None and by_cik is not None and by_lei != by_cik
    aliases_before = connection.execute(
        "select alias_id, entity_id, scheme, value from staging.entity_aliases order by alias_id"
    ).fetchall()

    _seed_nport_crosswalk(connection, AAPL, GOOG, GOOGL)
    after = _backfill(connection)
    assert after["failed"] == [], after
    assert after["relations"].get("superseded_by", 0) >= 1 and after["minted"] == {}, after

    # Every alias row that existed is still there, on the entity it was written for.
    assert (
        connection.execute(
            "select alias_id, entity_id, scheme, value from staging.entity_aliases "
            "where alias_id <= %s order by alias_id",
            (aliases_before[-1][0],),
        ).fetchall()
        == aliases_before
    )
    # The survivor is the entity a store that knew the link from the start would have
    # minted; the other one points at it.
    survivor = _resolve(connection, "lei", AAPL["lei"])
    assert survivor == _resolve(connection, "cik", AAPL["cik"])
    assert survivor == by_cik == expected_entity_id("issuer", "legacy-id", f"issuer:cik:{AAPL['cik']}")
    assert _edge(connection, "same_as", by_lei, by_cik)
    assert _edge(connection, "superseded_by", by_lei, by_cik)
    # As known before the proof existed, the two ids still name two entities.
    assert _resolve(connection, "cik", AAPL["cik"], known_at=BETWEEN) == by_cik
    assert _resolve(connection, "lei", AAPL["lei"], known_at=BETWEEN) == by_lei
    # The shared listing now has its one instrument.
    listing = _resolve(connection, "mic-ticker", "XNAS:AAPL")
    assert _edge(connection, "listed_as", _resolve(connection, "figi", AAPL["figi"]), listing)


def _mint(connection, kind: str, legacy_id: str):
    row = connection.execute("select staging.entity_mint(%s, 'legacy-id', %s, 'test')", (kind, legacy_id)).fetchone()
    assert row is not None
    entity_id = row[0]
    _alias(connection, entity_id, "legacy-id", legacy_id, valid_from="-infinity", at=datetime(2026, 1, 1, tzinfo=UTC))
    return entity_id


def _alias(connection, entity_id, scheme: str, value: str, *, valid_from: str, at: datetime, valid_to=None):
    connection.execute(
        """
        insert into staging.entity_aliases
            (entity_id, scheme, value, valid_from, valid_to, transaction_time, source, raw_ref,
             method, confidence, mapping_version)
        values (%s, %s, %s, %s, %s, %s, 'test', 'test', 'asserted', 1, 'test')
        """,
        (entity_id, scheme, value, valid_from, valid_to, at),
    )


def _line(connection, instrument, listing, *, retracted_on: str | None = None) -> None:
    """A `listed_as` edge asserted in 2020, optionally ended by a retraction."""
    known = datetime(2020, 1, 1, tzinfo=UTC)
    derived = connection.execute(
        "select staging.entity_relation_uuid('listed_as', %s, %s, '2020-01-01', %s, 'test', 'asserted')",
        (instrument, listing, known),
    ).fetchone()
    assert derived is not None
    connection.execute(
        """
        insert into staging.entity_relations
            (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
             transaction_time, source, raw_ref, method, confidence, mapping_version)
        values (%s, 'listed_as', %s, %s, '2020-01-01', %s, 'test', 'test', 'asserted', 1, 'test')
        """,
        (derived[0], instrument, listing, known),
    )
    if retracted_on is not None:
        connection.execute(
            "insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref) "
            "values (%s, %s, 'line delisted', %s, 'test', 'test')",
            (derived[0], retracted_on, datetime(2021, 1, 1, tzinfo=UTC)),
        )


def test_a_retracted_listing_line_does_not_block_a_later_one(connection) -> None:
    """One instrument per listing at a time: an earlier instrument whose line ended before
    the new evidence starts is history and does not block the new `listed_as` edge; a line
    still open does."""
    delisted = _mint(connection, "listing", AAPL["listing"])
    _line(
        connection,
        _mint(connection, "instrument", "security:test:aapl-predecessor"),
        delisted,
        retracted_on="2021-01-01",
    )
    occupied = _mint(connection, "listing", "listing:xnas:msft")
    _line(connection, _mint(connection, "instrument", "security:test:msft-squatter"), occupied)

    _seed_complete_production_run(connection)
    written = _backfill(connection)
    assert written["failed"] == [], written
    assert written["held_back"].get("skipped:store-holds-another-endpoint") == 1, written

    assert _resolve(connection, "legacy-id", AAPL["listing"]) == delisted
    assert _edge(connection, "listed_as", _resolve(connection, "cusip", AAPL["cusip"]), delisted)
    assert _resolve(connection, "legacy-id", "listing:xnas:msft") == occupied
    assert not _edge(connection, "listed_as", _resolve(connection, "cusip", "594918104"), occupied)


def test_a_reused_symbol_resolves_by_valid_date_and_by_what_was_known(connection) -> None:
    tag = uuid.uuid4().hex[:6].upper()
    symbol = f"XTST:E{tag}"
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)
    old = _mint(connection, "listing", f"listing:xtst:old{tag.lower()}")
    new = _mint(connection, "listing", f"listing:xtst:new{tag.lower()}")
    _alias(connection, old, "mic-ticker", symbol, valid_from="2026-01-01", at=t1)
    old_alias = connection.execute(
        "select alias_id from staging.entity_aliases where scheme = 'mic-ticker' and value = %s", (symbol,)
    ).fetchone()[0]

    # While the old line is open, nobody else may take the symbol.
    with pytest.raises(psycopg.errors.RaiseException, match="overlapping validity"):
        with connection.transaction():
            _alias(connection, new, "mic-ticker", symbol, valid_from="2026-06-01", at=t2)

    # The symbol is handed over on 2026-06-01: the old claim ends by a retraction row.
    connection.execute(
        "insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref) "
        "values (%s, '2026-06-01', 'symbol reassigned', %s, 'test', 'test')",
        (old_alias, t2),
    )
    _alias(connection, new, "mic-ticker", symbol, valid_from="2026-06-01", at=t2)

    assert _resolve(connection, "mic-ticker", symbol, valid_at=date(2026, 3, 1)) == old
    assert _resolve(connection, "mic-ticker", symbol, valid_at=date(2026, 7, 1)) == new
    assert _resolve(connection, "mic-ticker", symbol, valid_at=date(2025, 12, 31)) is None
    # Before the handover was known, the old line was believed open.
    assert _resolve(connection, "mic-ticker", symbol, valid_at=date(2026, 7, 1), known_at=t1) == old
    # A third claimant overlapping either side is refused.
    third = _mint(connection, "listing", f"listing:xtst:third{tag.lower()}")
    with pytest.raises(psycopg.errors.RaiseException, match="overlapping validity"):
        with connection.transaction():
            _alias(connection, third, "mic-ticker", symbol, valid_from="2026-05-01", at=t2)
    with pytest.raises(psycopg.errors.RaiseException, match="explicit"):
        with connection.transaction():
            connection.execute("select staging.entity_resolve('mic-ticker', %s, null, now())", (symbol,))


_RELATION = """
    insert into staging.entity_relations
        (relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
         transaction_time, source, raw_ref, method, confidence, mapping_version)
    values (%(id)s, 'issues', %(from)s, %(to)s, '-infinity', %(at)s, 't', 't', 'asserted', 1, 't')
"""
_DERIVED_RELATION_ID = (
    "select staging.entity_relation_uuid('issues', %(from)s, %(to)s, '-infinity', %(at)s, 't', 'asserted')"
)


def test_the_store_is_append_only_typed_and_derived(connection) -> None:
    tag = uuid.uuid4().hex[:8]
    listing = _mint(connection, "listing", f"listing:xtst:g{tag}")
    for statement in (
        "update staging.entities set minted_by = 'x' where entity_id = %s",
        "delete from staging.entities where entity_id = %s",
        "update staging.entity_aliases set value = 'x' where entity_id = %s",
        "delete from staging.entity_aliases where entity_id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with connection.transaction():
                connection.execute(statement, (listing,))

    # A scheme only names the kind it identifies, in canonical form.
    with pytest.raises(psycopg.errors.RaiseException, match="identifies"):
        with connection.transaction():
            _alias(connection, listing, "lei", "HWUPKR0MPOU8FGXBT394", valid_from="-infinity", at=NOW)
    with pytest.raises(psycopg.errors.RaiseException, match="canonical"):
        with connection.transaction():
            _alias(connection, listing, "mic-ticker", "xtst:lower", valid_from="-infinity", at=NOW)

    # Abstract kinds are not minted, and a relation respects its domain and range.
    with pytest.raises(psycopg.errors.RaiseException, match="cannot be minted"):
        with connection.transaction():
            _mint(connection, "organization", f"issuer:test:{tag}")
    issuer = _mint(connection, "issuer", f"issuer:test:{tag}")
    backwards = {"from": listing, "to": issuer, "at": ASSERTED}
    with pytest.raises(psycopg.errors.RaiseException, match="starts at"):
        with connection.transaction():
            derived = connection.execute(_DERIVED_RELATION_ID, backwards).fetchone()
            connection.execute(_RELATION, {**backwards, "id": derived[0] if derived else None})

    # Ids are derived, never chosen: an entity or relation id that is not its claim's is refused.
    share = _mint(connection, "instrument", f"security:test:{tag}")
    forwards = {"from": issuer, "to": share, "at": ASSERTED}
    with pytest.raises(psycopg.errors.RaiseException, match="not derived"):
        with connection.transaction():
            connection.execute(_RELATION, {**forwards, "id": uuid.uuid5(uuid.NAMESPACE_URL, tag)})
    derived = connection.execute(_DERIVED_RELATION_ID, forwards).fetchone()
    assert derived is not None
    connection.execute(_RELATION, {**forwards, "id": derived[0]})
    with pytest.raises(psycopg.errors.RaiseException, match="not derived"):
        with connection.transaction():
            connection.execute(
                "insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value) "
                "values (%s, 'listing', 'uuidv5:v1', 'test', 'legacy-id', 'listing:xtst:chosen')",
                (uuid.uuid5(uuid.NAMESPACE_URL, tag),),
            )

    # An entity nothing names cannot be committed.
    with pytest.raises(psycopg.errors.RaiseException, match="birth alias"):
        with connection.transaction():
            connection.execute(
                "select staging.entity_mint('listing', 'legacy-id', %s, 'test')", (f"listing:xtst:orphan{tag}",)
            )
            connection.execute("set constraints all immediate")


def test_the_backfill_plan_reads_only_tables_that_predate_it() -> None:
    """The #877 dry run executes the plan's SELECT read-only against an environment that
    has applied neither migration. That only works while the plan references nothing the
    two migrations create."""
    backfill = BACKFILL_MIGRATION.read_text()
    both = STORE_MIGRATION.read_text() + backfill
    # The view body is the `$view$` literal its boot-lock guard compares and applies (#915).
    replace = backfill.index("create or replace view staging.entity_backfill_plan as ")
    opening = backfill.rindex("$view$", 0, backfill.rindex("$view$", 0, replace))
    body = backfill[opening + len("$view$") : backfill.index("$view$", opening + len("$view$"))]
    assert "from relation_state" in body and "$view$" not in body
    created = set(re.findall(r"create (?:or replace )?(?:table if not exists|view|function) (staging\.\w+)", both))
    assert {"staging.entity_backfill_plan", "staging.entities", "staging.entity_mint"} <= created
    referenced = set(re.findall(r"staging\.\w+", body)) - {"staging.entity_backfill_plan"}
    assert referenced and not (referenced & created), sorted(referenced & created)


@pytest.fixture
def fresh_databases():
    """Three databases of our own, migrated from scratch, dropped afterwards."""
    params = conninfo_to_dict(settings.database_url)
    names = [f"entity_determinism_{uuid.uuid4().hex[:10]}" for _ in range(3)]
    try:
        admin = psycopg.connect(make_conninfo(**params), connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    created: list[str] = []
    try:
        for name in names:
            try:
                admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
            except psycopg.errors.InsufficientPrivilege:
                if os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
                    raise
                pytest.skip("the configured role cannot create databases")
            created.append(name)
            with psycopg.connect(make_conninfo(**{**params, "dbname": name}), autocommit=True) as fresh:
                for migration in sorted(MIGRATIONS.glob("*.sql")):
                    fresh.execute(migration.read_text())
        yield [make_conninfo(**{**params, "dbname": name}) for name in names]
    finally:
        for name in created:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
        admin.close()


def _identity_snapshot(conninfo: str, *, learn_the_link_late: bool) -> dict:
    with psycopg.connect(conninfo) as connection:
        _seed_both_universes(connection)
        if learn_the_link_late:
            assert _backfill(connection)["failed"] == []
        _seed_nport_crosswalk(connection, AAPL, GOOG, GOOGL)
        assert _backfill(connection)["failed"] == []
        entities = connection.execute(
            "select entity_id, kind, birth_scheme, birth_value, birth_generation from staging.entities"
        ).fetchall()
        relations = connection.execute("select relation_id from staging.entity_relations").fetchall()
        resolved = connection.execute(
            """
            select value, staging.entity_resolve('legacy-id', value, %s, %s)
            from staging.entity_aliases where scheme = 'legacy-id'
            """,
            (TODAY, NOW),
        ).fetchall()
    return {
        "entities": set(entities),
        "relations": {relation for (relation,) in relations},
        "resolved": dict(resolved),
    }


def test_the_same_evidence_mints_the_same_ids_in_any_database(fresh_databases) -> None:
    first_url, second_url, late_url = fresh_databases
    first = _identity_snapshot(first_url, learn_the_link_late=False)
    second = _identity_snapshot(second_url, learn_the_link_late=False)

    # Two fresh databases, the same evidence: the same entities, relations and resolutions.
    assert first["entities"] and first["relations"] and len(first["resolved"]) > 40
    assert first == second
    # And each id is the documented function of its birth alias, nothing else.
    for entity_id, kind, scheme, value, generation in first["entities"]:
        assert generation == 1
        assert entity_id == expected_entity_id(kind, scheme, value), (kind, scheme, value)

    # A database that learned the crosswalk one run later merged instead of minting; every
    # legacy id still resolves to the entity the fresh databases minted.
    late = _identity_snapshot(late_url, learn_the_link_late=True)
    assert late["resolved"] == first["resolved"]
    assert first["entities"] < late["entities"]


# -- the backfill is a Dagster job, never a boot step ----------------------------------


#: A statement that touches rows, as it would appear inside a DO block. Trigger definitions
#: say "BEFORE DELETE OR UPDATE ON ...", which none of these match.
_DATA_STATEMENT = re.compile(
    r"\binsert\s+into\b|\bupdate\s+[\w.]+\s+set\b|\bdelete\s+from\b|\bmerge\s+into\b|\btruncate\b|entity_backfill\(",
    re.IGNORECASE,
)


def _top_level_statements(sql_text: str) -> list[str]:
    """Statements of a migration file, split on `;` outside comments, quotes and $$ bodies."""
    text = re.sub(r"--[^\n]*", "", sql_text)
    statements, current, index = [], [], 0
    while index < len(text):
        if text.startswith("$$", index):
            end = text.index("$$", index + 2) + 2
            current.append(text[index:end])
            index = end
        elif text[index] == "'":
            end = index + 1
            while True:
                end = text.index("'", end) + 1
                if not text.startswith("'", end):
                    break
                end += 1
            current.append(text[index:end])
            index = end
        elif text[index] == ";":
            statements.append("".join(current).strip())
            current, index = [], index + 1
        else:
            current.append(text[index])
            index += 1
    statements.append("".join(current).strip())
    return [statement for statement in statements if statement]


def test_no_boot_migration_reads_or_writes_data_for_the_entity_store() -> None:
    """Every migration re-applies on every llm-service boot; the backfill held staging's boot
    for 34 s and failed a rollout. The entity migrations may only define things and seed
    their own registries, and no migration may call the backfill."""
    assert _top_level_statements((MIGRATIONS / "20260917T0430_datahub_entity_backfill.sql").read_text()) == []
    registries = ("staging.entity_kinds", "staging.entity_alias_schemes", "staging.entity_relation_types")
    for path in (STORE_MIGRATION, BACKFILL_MIGRATION):
        for statement in _top_level_statements(path.read_text()):
            head = statement.lower().split(None, 3)
            if head[0] == "insert":
                assert head[2] in registries, f"{path.name}: {statement[:80]}"
            elif head[0] == "do":
                # #915: a DO block may only guard a definition (create it when the catalog
                # says it is missing or different) -- it never reads or writes rows.
                assert not _DATA_STATEMENT.search(statement), f"{path.name} runs {statement[:120]!r} at boot"
            else:
                assert head[0] in {"create", "comment", "drop"}, f"{path.name} runs {statement[:80]!r} at boot"
    for path in MIGRATIONS.glob("*.sql"):
        for statement in _top_level_statements(path.read_text()):
            assert "entity_backfill(" not in statement.lower() or statement.lower().startswith(("create", "comment")), (
                f"{path.name} runs the entity backfill at boot"
            )


def _seeded_database(conninfo: str) -> None:
    with psycopg.connect(conninfo) as connection:
        _seed_both_universes(connection)
        _seed_nport_crosswalk(connection, AAPL, GOOG, GOOGL)


def test_the_job_fills_the_store_once_and_reports_what_it_did(fresh_databases, monkeypatch) -> None:
    from data_engine.lanes import entity_identity

    conninfo = fresh_databases[0]
    _seeded_database(conninfo)
    monkeypatch.setattr(settings, "database_url", conninfo)

    first = entity_identity.entity_identity_backfill_job.execute_in_process()
    assert first.success
    summary = first.output_for_node("entity_identity_backfill_op")
    assert summary["failed"] == [] and summary["minted"] and summary["duration_seconds"] >= 0
    materialized = [
        event.event_specific_data.metadata
        for event in first.all_node_events
        if event.event_type_value == "STEP_OUTPUT" and event.event_specific_data is not None
    ]
    assert materialized and materialized[0]["entities_minted"].value == sum(summary["minted"].values())

    second = entity_identity_backfill_job_output(entity_identity)
    assert second["minted"] == {} and second["aliases"] == {} and second["relations"] == {}

    with psycopg.connect(conninfo) as connection:
        apple = _resolve(connection, "lei", AAPL["lei"])
    assert apple == expected_entity_id("issuer", "legacy-id", f"issuer:cik:{AAPL['cik']}")


def entity_identity_backfill_job_output(entity_identity) -> dict:
    result = entity_identity.entity_identity_backfill_job.execute_in_process()
    assert result.success
    return result.output_for_node("entity_identity_backfill_op")


def test_the_sensor_launches_once_for_new_evidence_and_is_otherwise_quiet(fresh_databases, monkeypatch) -> None:
    from data_engine.lanes import entity_identity

    conninfo = fresh_databases[0]
    monkeypatch.setattr(settings, "database_url", conninfo)
    sensor = entity_identity.entity_identity_backfill_sensor

    def evaluate(cursor: str | None):
        context = dg.build_sensor_context(cursor=cursor)
        result = sensor(context)
        return result, context.cursor

    # An empty database: nothing is in use, nothing to do.
    result, cursor = evaluate(None)
    assert isinstance(result, dg.SkipReason)

    _seeded_database(conninfo)
    result, cursor = evaluate(cursor)
    assert isinstance(result, dg.RunRequest) and result.job_name == entity_identity.ENTITY_BACKFILL_JOB_NAME
    assert cursor is not None

    entity_identity_backfill_job_output(entity_identity)
    # Nothing arrived since the launch: one watermark read, no launch.
    result, same_cursor = evaluate(cursor)
    assert isinstance(result, dg.SkipReason) and same_cursor == cursor

    with psycopg.connect(conninfo) as connection:
        # A fresh deployment (no cursor) over a filled store still looks once at the
        # crosswalk evidence, and is quiet once that evidence is behind its cursor.
        assert connection.execute("select staging.entity_backfill_due(null)").fetchone() == ("new-crosswalk-evidence",)
        assert connection.execute("select staging.entity_backfill_due(%s)", (NOW,)).fetchone() == (None,)
        # A published universe with an id nobody has seen is due at once.
        fields = ["issuer_id", "instrument_id", "listing_id", "ticker"]
        instruments = [["issuer:cik:0000877877", "security:figi:bbg000877877", "listing:xnas:zzzz", "ZZZZ"]]
        payload = {"instrument_tuple_fields": fields, "instruments": instruments, "report_date": "2026-06-30"}
        digest = canonical_sha256(payload)
        connection.execute(
            "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
            "values (%s, 'universe-list:entity-test', %s, %s)",
            (f"universe-list:{digest}", digest, psycopg.types.json.Jsonb(payload)),
        )
        connection.commit()
    result, _ = evaluate(cursor)
    assert isinstance(result, dg.RunRequest)
    assert entity_identity_backfill_job_output(entity_identity)["minted"] == {
        "issuer": 1,
        "instrument": 1,
        "listing": 1,
    }
