"""`mart.entity_identity` computes survivors and alias ends in one set-based pass (#1337).

The view of 20260923T1800 called `staging.entity_survivor()` and `staging.entity_alias_valid_to()`
for each row. On production, one evaluation took 1.4 s to 3 s. 20261015T0900 gives the same values
for all rows at once, through common table expressions.

The checks:

1. The view returns the rows of the old definition. The old definition is the `$view$` text of
   20260923T1800. Two seeds hold merge chains, a retracted merge, expired and withdrawn aliases,
   a tie on confidence, a double match in `staging.kg_entities` and entities without an alias.
2. Each case of the small seed has an explicit expected row. So the rules have a pin that does
   not depend on the old text.
3. One evaluation calls `staging.entity_survivor` at most once for each alias row and each
   identity row. It calls `staging.entity_alias_valid_to` at most once for each alias row.
   The verdict reads call counts from `track_functions`, never the clock.
4. A replay of each file that defines the view, and a second run of the chain, keep the view.
   A replay takes no strong lock on a mart relation.

The module builds its own database from the migration chain, without a template. So the
database holds only the rows that these tests seed. Each test seeds rows in a transaction that
is rolled back.

When two aliases of one survivor and scheme have equal confidence and equal transaction time,
neither definition sets which one wins. The seeds hold no such tie, and a check proves it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
#: The definition before #1337. Its `$view$` text is the reference of the equivalence checks.
LEGACY_ID_MIGRATION = MIGRATIONS / "20260923T1800_datahub_mart_entity_identity_legacy_id.sql"
#: The definition of #1337. It is the last file that defines the view.
SET_BASED_MIGRATION = MIGRATIONS / "20261015T0900_datahub_mart_entity_identity_set_based.sql"
#: Every file that defines the view. Each boot replays all of them.
VIEW_MIGRATIONS = (
    MIGRATIONS / "20260921T2100_datahub_mart_entity_identity.sql",
    MIGRATIONS / "20260923T0800_datahub_mart_entity_identity_listing_id.sql",
    LEGACY_ID_MIGRATION,
    SET_BASED_MIGRATION,
)

COLUMNS = ("entity_id", "kind", "current_ticker", "name", "cik", "lei", "listing_id", "legacy_id")
HELPERS = ("entity_survivor", "entity_alias_valid_to")
#: The relation that holds the old definition inside one test transaction.
OLD_VIEW = "pg_temp.entity_identity_before_1337"

T0 = datetime(2026, 1, 5, tzinfo=UTC)
HOUR = timedelta(hours=1)

Row = tuple[Any, ...]


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """One database built from the chain, not cloned. It holds only the rows the tests seed."""
    with isolated_test_database("entity_identity", template=None) as db:
        yield str(db)


@pytest.fixture
def conn(database: str) -> Iterator[psycopg.Connection]:
    """A connection in a transaction that is always rolled back."""
    with psycopg.connect(database) as connection:
        try:
            yield connection
        finally:
            connection.rollback()


def lei(n: int) -> str:
    return f"TA1337{n:012d}00"


def figi(n: int) -> str:
    return f"BBG{n:08d}7"


class Store:
    """Writes entity rows through the insert guards of 20260917T0412."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def mint(self, kind: str, scheme: str, value: str, *, at: datetime = T0, birth_alias: bool = True) -> str:
        """Mints an entity. Without `birth_alias` the entity has no alias (the transaction never commits)."""
        row = self.conn.execute(
            "select staging.entity_mint(%s, %s, %s, 'test:1337')::text", (kind, scheme, value)
        ).fetchone()
        assert row is not None
        if birth_alias:
            self.alias(row[0], scheme, value, at=at)
        return str(row[0])

    def alias(
        self,
        entity_id: str,
        scheme: str,
        value: str,
        *,
        at: datetime = T0,
        confidence: str = "1",
        valid_from: str = "-infinity",
        valid_to: str | None = None,
    ) -> int:
        row = self.conn.execute(
            """
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, valid_to, transaction_time, source, raw_ref,
                 method, confidence, mapping_version)
            values (%s::uuid, %s, %s, %s::date, %s::date, %s, 'test:1337', 'test:1337',
                    'asserted', %s::numeric, 'test:1337')
            returning alias_id
            """,
            (entity_id, scheme, value, valid_from, valid_to, at, confidence),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def relate(self, relation_type: str, source: str, target: str, *, at: datetime = T0) -> str:
        method = "merge" if relation_type == "superseded_by" else "asserted"
        row = self.conn.execute(
            """
            insert into staging.entity_relations
                (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time,
                 source, raw_ref, method, confidence, mapping_version)
            values (staging.entity_relation_uuid(%(type)s, %(source)s::uuid, %(target)s::uuid,
                                                 '-infinity'::date, %(at)s, 'test:1337', %(method)s),
                    %(type)s, %(source)s::uuid, %(target)s::uuid, '-infinity', %(at)s,
                    'test:1337', 'test:1337', %(method)s, 1, 'test:1337')
            returning relation_id::text
            """,
            {"type": relation_type, "source": source, "target": target, "at": at, "method": method},
        ).fetchone()
        assert row is not None
        return str(row[0])

    def end_alias(self, alias_id: int, valid_to: str, *, at: datetime) -> None:
        self.conn.execute(
            """
            insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref)
            values (%s, %s::date, 'test:1337', %s, 'test:1337', 'test:1337')
            """,
            (alias_id, valid_to, at),
        )

    def retract_relation(self, relation_id: str, *, at: datetime) -> None:
        self.conn.execute(
            """
            insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref)
            values (%s::uuid, '2026-01-01', 'test:1337', %s, 'test:1337', 'test:1337')
            """,
            (relation_id, at),
        )

    def kg(self, kg_id: str, display_name: str) -> None:
        self.conn.execute(
            "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', %s)",
            (kg_id, display_name),
        )


def seed_cases(conn: psycopg.Connection) -> tuple[dict[str, str], dict[str, list[Row]]]:
    """The small seed: one entity or group for each rule. Returns the ids and the expected rows."""
    store = Store(conn)
    ids: dict[str, str] = {}
    expected: dict[str, list[Row]] = {}

    # A plain issuer, instrument and listing. The issuer and the instrument get the ticker
    # through the `issues` and `listed_as` relations.
    ids["issuer"] = store.mint("issuer", "lei", lei(1))
    store.alias(ids["issuer"], "cik", "0000001337")
    store.alias(ids["issuer"], "name", "Plain Issuer", confidence="0.9")
    store.alias(ids["issuer"], "legacy-id", f"issuer:lei:{lei(1)}")
    store.kg(f"issuer:lei:{lei(1)}", "KG Plain")
    ids["instrument"] = store.mint("instrument", "figi", figi(1))
    store.alias(ids["instrument"], "legacy-id", f"security:figi:{figi(1).lower()}")
    ids["listing"] = store.mint("listing", "mic-ticker", "XNAS:PLN")
    store.alias(ids["listing"], "legacy-id", "listing:xnas:pln")
    store.relate("issues", ids["issuer"], ids["instrument"])
    store.relate("listed_as", ids["instrument"], ids["listing"])
    expected["issuer"] = [
        ("issuer", "PLN", "Plain Issuer", "0000001337", lei(1), "listing:xnas:pln", f"issuer:lei:{lei(1)}")
    ]
    expected["instrument"] = [("instrument", "PLN", None, None, None, None, f"security:figi:{figi(1).lower()}")]
    expected["listing"] = [("listing", "PLN", None, None, None, "listing:xnas:pln", "listing:xnas:pln")]

    # A merge chain of depth 3: a -> b -> c -> survivor. Every member reads the values of the
    # whole group. The newest LEI and the most confident name win.
    ids["survivor"] = store.mint("issuer", "lei", lei(10))
    store.alias(ids["survivor"], "name", "Survivor Name", confidence="0.6")
    ids["merged_c"] = store.mint("issuer", "lei", lei(11), at=T0 + 1 * HOUR)
    store.alias(ids["merged_c"], "name", "C Name", confidence="0.9", at=T0 + 1 * HOUR)
    ids["merged_b"] = store.mint("issuer", "lei", lei(12), at=T0 + 2 * HOUR)
    store.alias(ids["merged_b"], "legacy-id", f"issuer:lei:{lei(12)}", at=T0 + 2 * HOUR)
    ids["merged_a"] = store.mint("issuer", "lei", lei(13), at=T0 + 3 * HOUR)
    store.relate("superseded_by", ids["merged_a"], ids["merged_b"], at=T0 + 4 * HOUR)
    store.relate("superseded_by", ids["merged_b"], ids["merged_c"], at=T0 + 5 * HOUR)
    store.relate("superseded_by", ids["merged_c"], ids["survivor"], at=T0 + 6 * HOUR)
    for label in ("survivor", "merged_c", "merged_b", "merged_a"):
        expected[label] = [("issuer", None, "C Name", None, lei(13), None, f"issuer:lei:{lei(12)}")]

    # A retracted merge. x -> y is newer but retracted, so x continues as z. w -> y is retracted
    # and w has no other edge, so w is its own survivor.
    ids["merge_target_z"] = store.mint("issuer", "lei", lei(20))
    store.alias(ids["merge_target_z"], "name", "Z Name", confidence="0.7")
    ids["merge_target_y"] = store.mint("issuer", "lei", lei(21))
    store.alias(ids["merge_target_y"], "name", "Y Name", confidence="0.9")
    ids["merge_source_x"] = store.mint("issuer", "lei", lei(22), at=T0 + 1 * HOUR)
    store.alias(ids["merge_source_x"], "name", "X Name", confidence="0.5")
    retracted = store.relate("superseded_by", ids["merge_source_x"], ids["merge_target_y"], at=T0 + 48 * HOUR)
    store.retract_relation(retracted, at=T0 + 49 * HOUR)
    store.relate("superseded_by", ids["merge_source_x"], ids["merge_target_z"], at=T0 + 24 * HOUR)
    ids["merge_source_w"] = store.mint("issuer", "lei", lei(23))
    store.alias(ids["merge_source_w"], "name", "W Name", confidence="0.8")
    retracted = store.relate("superseded_by", ids["merge_source_w"], ids["merge_target_y"], at=T0 + 2 * HOUR)
    store.retract_relation(retracted, at=T0 + 3 * HOUR)
    for label in ("merge_target_z", "merge_source_x"):
        expected[label] = [("issuer", None, "Z Name", None, lei(22), None, None)]
    expected["merge_target_y"] = [("issuer", None, "Y Name", None, lei(21), None, None)]
    expected["merge_source_w"] = [("issuer", None, "W Name", None, lei(23), None, None)]

    # Expired and withdrawn aliases. Each listing has an older fallback ticker F<k> and a newer
    # case ticker C<k>. The case ticker wins only when its validity is not withdrawn.
    # (valid_to, retraction ends, case ticker stays)
    expiry_cases = {
        1: (None, ("2024-01-01",), False),  # a retraction at valid_from withdraws the alias
        2: (None, ("2025-06-01",), True),  # a retraction after valid_from only ends it
        3: (None, ("2027-01-01", "2023-01-01"), False),  # the earliest retraction decides
        4: ("2025-01-01", (), True),  # expired before today, still a claim
        5: ("2030-01-01", (), True),  # ends in the future
        6: ("2025-01-01", ("2029-01-01",), True),  # the alias's own end is earlier than the retraction
        7: (None, ("2022-01-01",), False),  # a retraction before valid_from withdraws the alias
        8: ("2030-01-01", ("2024-01-01",), False),  # the retraction is earlier than the alias's end
    }
    for k, (valid_to, retractions, stays) in expiry_cases.items():
        label = f"expiry_{k}"
        ids[label] = store.mint("listing", "mic-ticker", f"XNAS:F{k}")
        alias_id = store.alias(
            ids[label], "mic-ticker", f"XNAS:C{k}", at=T0 + 1 * HOUR, valid_from="2024-01-01", valid_to=valid_to
        )
        for n, retraction_end in enumerate(retractions, start=2):
            store.end_alias(alias_id, retraction_end, at=T0 + n * HOUR)
        ticker = f"C{k}" if stays else f"F{k}"
        expected[label] = [("listing", ticker, None, None, None, f"listing:xnas:{ticker.lower()}", None)]

    # A tie on confidence: the newer name wins. A newer name with less confidence loses.
    ids["confidence_tie"] = store.mint("issuer", "lei", lei(30))
    store.alias(ids["confidence_tie"], "name", "Newer Name", confidence="0.8", at=T0 + 2 * HOUR)
    store.alias(ids["confidence_tie"], "name", "Older Name", confidence="0.8", at=T0 + 1 * HOUR)
    store.alias(ids["confidence_tie"], "name", "Low Name", confidence="0.3", at=T0 + 3 * HOUR)
    expected["confidence_tie"] = [("issuer", None, "Newer Name", None, lei(30), None, None)]

    # Two staging.kg_entities rows match one entity: one by UUID and one by legacy id.
    ids["double_match"] = store.mint("issuer", "lei", lei(40))
    store.alias(ids["double_match"], "legacy-id", f"issuer:lei:{lei(40)}")
    store.kg(ids["double_match"], "KG by UUID")
    store.kg(f"issuer:lei:{lei(40)}", "KG by legacy id")
    expected["double_match"] = [
        ("issuer", None, "KG by UUID", None, lei(40), None, f"issuer:lei:{lei(40)}"),
        ("issuer", None, "KG by legacy id", None, lei(40), None, f"issuer:lei:{lei(40)}"),
    ]

    # No alias: one entity has no alias row, one has only a withdrawn alias and a KG name.
    ids["no_alias"] = store.mint("issuer", "lei", lei(50), birth_alias=False)
    expected["no_alias"] = [("issuer", None, None, None, None, None, None)]
    ids["withdrawn_only"] = store.mint("issuer", "lei", lei(51), birth_alias=False)
    withdrawn = store.alias(ids["withdrawn_only"], "lei", lei(51))
    store.end_alias(withdrawn, "-infinity", at=T0 + 1 * HOUR)
    store.kg(ids["withdrawn_only"], "KG Only")
    expected["withdrawn_only"] = [("issuer", None, "KG Only", None, None, None, None)]

    # A merged listing. The old listing reads the ticker of its survivor. The relation names
    # the old listing, so the instrument gets no ticker through it.
    ids["merged_instrument"] = store.mint("instrument", "figi", figi(60))
    ids["listing_old"] = store.mint("listing", "mic-ticker", "XNAS:OLDM")
    ids["listing_new"] = store.mint("listing", "mic-ticker", "XNAS:NEWM", at=T0 + 1 * HOUR)
    store.relate("superseded_by", ids["listing_old"], ids["listing_new"], at=T0 + 2 * HOUR)
    store.relate("listed_as", ids["merged_instrument"], ids["listing_old"])
    for label in ("listing_old", "listing_new"):
        expected[label] = [("listing", "NEWM", None, None, None, "listing:xnas:newm", None)]
    expected["merged_instrument"] = [("instrument", None, None, None, None, None, None)]
    return ids, expected


#: A seed of production size: 338 entities and 782 aliases (production had 338 and 764).
#: Members of one survivor group and scheme never share confidence and transaction time.
PRODUCTION_SIZE_SEED = """
do $$
declare
    n integer;
    v_lei text;
    v_figi text;
    v_issuer uuid;
    v_instrument uuid;
    v_listing uuid;
    v_a uuid;
    v_b uuid;
    v_alias bigint;
    v_relation uuid;
    v_t constant timestamptz := '2026-01-05T00:00:00Z';
begin
    for n in 1..110 loop
        v_lei := 'TB1337' || lpad(n::text, 12, '0') || '00';
        v_figi := 'BBG' || lpad((1000 + n)::text, 8, '0') || '7';
        v_issuer := staging.entity_mint('issuer', 'lei', v_lei, 'test:1337');
        v_instrument := staging.entity_mint('instrument', 'figi', v_figi, 'test:1337');
        v_listing := staging.entity_mint('listing', 'mic-ticker', 'XNYS:T' || n, 'test:1337');
        insert into staging.entity_aliases
            (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
        values (v_issuer, 'lei', v_lei, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
               (v_issuer, 'name', 'Issuer ' || n, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 0.9, 'test:1337'),
               (v_issuer, 'legacy-id', 'issuer:lei:' || v_lei, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
               (v_instrument, 'figi', v_figi, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
               (v_instrument, 'legacy-id', 'security:figi:' || lower(v_figi), '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
               (v_listing, 'mic-ticker', 'XNYS:T' || n, '2020-01-01', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
               (v_listing, 'legacy-id', 'listing:xnys:t' || n, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337');
        insert into staging.entity_relations
            (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
        values (staging.entity_relation_uuid('issues', v_issuer, v_instrument, '-infinity', v_t, 'test:1337', 'asserted'),
                'issues', v_issuer, v_instrument, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
               (staging.entity_relation_uuid('listed_as', v_instrument, v_listing, '-infinity', v_t, 'test:1337', 'asserted'),
                'listed_as', v_instrument, v_listing, '-infinity', v_t, 'test:1337', 'test:1337', 'asserted', 1, 'test:1337');
        insert into staging.kg_entities (id, entity_type, display_name) values ('issuer:lei:' || v_lei, 'company', 'KG Issuer ' || n);
        if n % 10 = 0 then
            insert into staging.kg_entities (id, entity_type, display_name) values (v_issuer::text, 'company', 'KG UUID ' || n);
        end if;
        -- Four merge chains a -> b -> issuer n. The third edge of chain 4 is retracted.
        if n <= 4 then
            v_a := staging.entity_mint('issuer', 'lei', 'TC1337' || lpad((10 * n + 1)::text, 12, '0') || '00', 'test:1337');
            v_b := staging.entity_mint('issuer', 'lei', 'TC1337' || lpad((10 * n + 2)::text, 12, '0') || '00', 'test:1337');
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (v_a, 'lei', 'TC1337' || lpad((10 * n + 1)::text, 12, '0') || '00', '-infinity', v_t + interval '1 hour',
                    'test:1337', 'test:1337', 'asserted', 1, 'test:1337'),
                   (v_a, 'name', 'Merged ' || n, '-infinity', v_t + interval '1 hour',
                    'test:1337', 'test:1337', 'asserted', 0.95, 'test:1337'),
                   (v_b, 'lei', 'TC1337' || lpad((10 * n + 2)::text, 12, '0') || '00', '-infinity', v_t + interval '2 hour',
                    'test:1337', 'test:1337', 'asserted', 1, 'test:1337');
            insert into staging.entity_relations
                (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (staging.entity_relation_uuid('superseded_by', v_a, v_b, '-infinity', v_t + interval '3 hour', 'test:1337', 'merge'),
                    'superseded_by', v_a, v_b, '-infinity', v_t + interval '3 hour', 'test:1337', 'test:1337', 'merge', 1, 'test:1337');
            v_relation := staging.entity_relation_uuid('superseded_by', v_b, v_issuer, '-infinity', v_t + interval '4 hour', 'test:1337', 'merge');
            insert into staging.entity_relations
                (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (v_relation, 'superseded_by', v_b, v_issuer, '-infinity', v_t + interval '4 hour', 'test:1337', 'test:1337', 'merge', 1, 'test:1337');
            if n = 4 then
                insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref)
                values (v_relation, '2026-01-01', 'test:1337', v_t + interval '5 hour', 'test:1337', 'test:1337');
            end if;
        end if;
    end loop;
    -- Withdraw five names, end five tickers, and give one ticker two retractions.
    for v_alias in select alias_id from staging.entity_aliases where scheme = 'name' and value like 'Issuer %' order by alias_id limit 5 loop
        insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref)
        values (v_alias, '-infinity', 'test:1337', v_t + interval '6 hour', 'test:1337', 'test:1337');
    end loop;
    for v_alias in select alias_id from staging.entity_aliases where scheme = 'mic-ticker' and value like 'XNYS:%' order by alias_id limit 5 loop
        insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref)
        values (v_alias, '2026-06-01', 'test:1337', v_t + interval '6 hour', 'test:1337', 'test:1337'),
               (v_alias, case when v_alias % 2 = 0 then '2019-01-01'::date else '2027-01-01'::date end,
                'test:1337', v_t + interval '7 hour', 'test:1337', 'test:1337');
    end loop;
end
$$;
"""


def seed_production_size(conn: psycopg.Connection) -> tuple[int, int]:
    """Seeds the production-size set. Returns (identity rows, alias rows) of the whole store."""
    conn.execute(PRODUCTION_SIZE_SEED)
    row = conn.execute(
        "select (select count(*) from staging.entities), (select count(*) from staging.entity_aliases)"
    ).fetchone()
    assert row is not None
    identities, aliases = int(row[0]), int(row[1])
    assert identities >= 300 and aliases >= 700, (identities, aliases)
    return identities, aliases


def old_definition() -> str:
    """The view text of 20260923T1800, the definition before #1337."""
    parts = LEGACY_ID_MIGRATION.read_text(encoding="utf-8").split("$view$")
    assert len(parts) == 3, f"{LEGACY_ID_MIGRATION.name} must hold exactly one $view$ body"
    assert "staging.entity_survivor(" in parts[1], "the reference text must be the definition that calls the helpers"
    return parts[1]


def create_old_view(conn: psycopg.Connection) -> None:
    conn.execute(f"create temp view {OLD_VIEW.split('.')[1]} as " + old_definition())


def columns(conn: psycopg.Connection, relation: str) -> list[tuple[str, str]]:
    return [
        (str(name), str(kind))
        for name, kind in conn.execute(
            """
            select attname, format_type(atttypid, atttypmod)
            from pg_attribute
            where attrelid = %s::regclass and attnum > 0 and not attisdropped
            order by attnum
            """,
            (relation,),
        ).fetchall()
    ]


def all_rows(conn: psycopg.Connection, relation: str) -> list[Row]:
    rows = conn.execute(f"select {', '.join(COLUMNS)} from {relation}").fetchall()  # noqa: S608
    return sorted((tuple(row) for row in rows), key=repr)


def rows_by_label(conn: psycopg.Connection, ids: dict[str, str]) -> dict[str, list[Row]]:
    """The rows of `mart.entity_identity` for each seeded entity, without the entity id."""
    labels = {entity_id: label for label, entity_id in ids.items()}
    found: dict[str, list[Row]] = {label: [] for label in ids}
    for row in conn.execute(
        f"select {', '.join(COLUMNS)} from mart.entity_identity where entity_id = any(%s::uuid[])",  # noqa: S608
        (list(ids.values()),),
    ).fetchall():
        found[labels[str(row[0])]].append(tuple(row[1:]))
    return {label: sorted(rows, key=repr) for label, rows in found.items()}


def full_ties(conn: psycopg.Connection) -> int:
    """Groups of active aliases that share survivor, scheme, confidence and transaction time.

    It reads through the two helpers, so it does not depend on the view under test.
    """
    row = conn.execute(
        """
        select count(*)
        from (
            select staging.entity_survivor(alias.entity_id, 'infinity'), alias.scheme,
                   alias.confidence, alias.transaction_time
            from staging.entity_aliases alias
            where staging.entity_alias_valid_to(alias.alias_id, 'infinity') is null
               or staging.entity_alias_valid_to(alias.alias_id, 'infinity') > alias.valid_from
            group by 1, 2, 3, 4
            having count(*) > 1
        ) tie
        """
    ).fetchone()
    assert row is not None
    return int(row[0])


def assert_same_rows_as_the_old_definition(conn: psycopg.Connection) -> list[Row]:
    """Compares the view with the old definition, as multisets, in both directions."""
    assert full_ties(conn) == 0, "a full tie has no defined winner; the comparison needs a seed without one"
    create_old_view(conn)
    for left, right in (("mart.entity_identity", OLD_VIEW), (OLD_VIEW, "mart.entity_identity")):
        extra = conn.execute(
            f"select * from (select * from {left} except all select * from {right}) extra"  # noqa: S608
        ).fetchall()
        assert extra == [], f"rows of {left} that {right} does not return: {extra}"
    after = all_rows(conn, "mart.entity_identity")
    before = all_rows(conn, OLD_VIEW)
    assert after == before
    return after


def test_the_view_keeps_its_columns_names_and_order(conn: psycopg.Connection) -> None:
    """The readers select these columns by name. The type of each column stays the same too."""
    create_old_view(conn)
    assert [name for name, _ in columns(conn, "mart.entity_identity")] == list(COLUMNS)
    assert columns(conn, "mart.entity_identity") == columns(conn, OLD_VIEW)


def test_each_seeded_case_reads_as_the_rules_say(conn: psycopg.Connection) -> None:
    """#1337 criterion 1, pinned: an explicit row for each case of the small seed."""
    ids, expected = seed_cases(conn)
    assert rows_by_label(conn, ids) == {label: sorted(rows, key=repr) for label, rows in expected.items()}


def test_the_small_seed_returns_the_rows_of_the_old_definition(conn: psycopg.Connection) -> None:
    """#1337 criterion 1: merge chains, a retracted merge, expired aliases, a double match, no alias."""
    ids, _ = seed_cases(conn)
    rows = assert_same_rows_as_the_old_definition(conn)
    assert len(rows) == len(ids) + 1  # the double match adds one row


def test_the_production_size_seed_returns_the_rows_of_the_old_definition(conn: psycopg.Connection) -> None:
    """#1337 criterion 1 on 338 entities: every row and every value is equal."""
    seed_cases(conn)
    identities, _ = seed_production_size(conn)
    rows = assert_same_rows_as_the_old_definition(conn)
    assert len(rows) > identities  # each double match adds one row


def helper_calls(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select funcname, calls
        from pg_stat_xact_user_functions
        where schemaname = 'staging' and funcname = any(%s)
        """,
        (list(HELPERS),),
    ).fetchall()
    calls = {name: 0 for name in HELPERS}
    calls.update({str(name): int(count) for name, count in rows})
    return calls


def calls_of_one_evaluation(conn: psycopg.Connection, relation: str) -> dict[str, int]:
    before = helper_calls(conn)
    conn.execute(f"select count(*) from {relation}").fetchone()  # noqa: S608
    after = helper_calls(conn)
    return {name: after[name] - before[name] for name in HELPERS}


def test_one_evaluation_calls_each_helper_at_most_once_for_each_row(conn: psycopg.Connection) -> None:
    """#1337 criterion 2. The verdict counts calls; it never reads the clock.

    `track_functions` needs a superuser. CI connects as one. Another role fails here loudly.
    """
    identities, aliases = seed_production_size(conn)
    conn.execute("set local track_functions = 'all'")
    survivor_bound = aliases + identities

    calls = calls_of_one_evaluation(conn, "mart.entity_identity")
    assert calls["entity_survivor"] <= survivor_bound, (calls, identities, aliases)
    assert calls["entity_alias_valid_to"] <= aliases, (calls, identities, aliases)

    # Control: on the same rows the old definition breaks the bound. So the probe sees calls.
    create_old_view(conn)
    old = calls_of_one_evaluation(conn, OLD_VIEW)
    assert old["entity_survivor"] > survivor_bound, (old, identities, aliases)


def test_mart_readonly_reads_the_view(conn: psycopg.Connection) -> None:
    """The consumers read as `mart_readonly`. That role has no usage on schema staging."""
    ids, _ = seed_cases(conn)
    as_owner = all_rows(conn, "mart.entity_identity")
    conn.execute("set local role mart_readonly")
    assert all_rows(conn, "mart.entity_identity") == as_owner
    assert len(as_owner) == len(ids) + 1


def view_state(conn: psycopg.Connection) -> tuple[str, str]:
    """The transaction that last wrote the rewrite rule of the view, and its definition."""
    row = conn.execute(
        """
        select rule.xmin::text, pg_get_viewdef(rule.ev_class)
        from pg_rewrite rule
        where rule.ev_class = to_regclass('mart.entity_identity') and rule.rulename = '_RETURN'
        """
    ).fetchone()
    assert row is not None, "mart.entity_identity does not exist"
    return str(row[0]), str(row[1])


def strong_mart_locks(conn: psycopg.Connection) -> list[tuple[str, str]]:
    held: Iterable[tuple[str, str]] = conn.execute(
        """
        select format('%s.%s', n.nspname, c.relname), l.mode
        from pg_locks l
        join pg_class c on c.oid = l.relation
        join pg_namespace n on n.oid = c.relnamespace
        where l.pid = pg_backend_pid() and l.locktype = 'relation' and l.granted and n.nspname = 'mart'
        """
    ).fetchall()
    return sorted(
        (name, mode)
        for name, mode in held
        if mode not in {"AccessShareLock", "RowExclusiveLock", "ShareUpdateExclusiveLock"}
    )


@pytest.mark.parametrize("migration", VIEW_MIGRATIONS, ids=[path.name[:13] for path in VIEW_MIGRATIONS])
def test_a_replay_keeps_the_view_and_takes_no_strong_lock(conn: psycopg.Connection, migration: Path) -> None:
    """#1337 criterion 3: a boot replays every file that defines the view.

    No file replaces the view: the rewrite rule keeps its xmin and its definition. On a mart
    relation each file takes only ACCESS SHARE, ROW EXCLUSIVE or SHARE UPDATE EXCLUSIVE.
    """
    before = view_state(conn)
    conn.execute(migration.read_text(encoding="utf-8"))
    assert strong_mart_locks(conn) == []
    assert view_state(conn) == before


def test_a_second_run_of_the_chain_keeps_the_view(database: str) -> None:
    """#1337 criterion 3: the fixture applied the chain once. A second run changes nothing."""
    with psycopg.connect(database) as connection:
        before = view_state(connection)
    apply_migration_chain(database)
    with psycopg.connect(database) as connection:
        assert view_state(connection) == before
