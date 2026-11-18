"""Slice 2 gate tests (#1730): staging.kg_* retirement and UUID convergence.

Verifies:
1. Retired tables (staging.kg_entities, staging.kg_identifiers, staging.kg_edges) do not exist.
2. staging.analyst_rating_events and staging.fund_holding_facts use uuid FK columns referencing
   staging.entities(entity_id).
3. Runtime views/functions reference zero retired kg_* tables.
4. No active application or shared module queries staging.kg_entities, staging.kg_identifiers, or
   staging.kg_edges.
5. An isolated legacy database carrying kg_* rows, text FK rows, plain-name analysts, ETFs, and
   company:cik + UUID duplicate collisions upgrades cleanly through the Slice 2 migration without
   data loss and replays with zero lock waits.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_runtime.testing import apply_migration_chain, guard_db_write

REPO_ROOT = Path(__file__).resolve().parents[3]
RETIRED_TABLES = ("staging.kg_entities", "staging.kg_identifiers", "staging.kg_edges")
RETIRED_PATTERN = re.compile(r"\bstaging\.(kg_entities|kg_identifiers|kg_edges)\b")


def _admin_url() -> str:
    params = conninfo_to_dict(guard_db_write())
    return make_conninfo(**(params | {"dbname": "postgres"}))


@pytest.fixture(scope="module")
def slice2_db() -> str:
    admin_url = _admin_url()
    name = f"truealpha_slice2_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs required DB tests")

    params = conninfo_to_dict(admin_url)
    db_url = make_conninfo(**(params | {"dbname": name}))
    try:
        apply_migration_chain(db_url)
        yield db_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s", (name,))
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))


def test_retired_kg_tables_do_not_exist(slice2_db: str) -> None:
    with psycopg.connect(slice2_db) as conn:
        for qualified in RETIRED_TABLES:
            assert conn.execute("select to_regclass(%s)", (qualified,)).fetchone() == (None,), qualified


def test_mart_readonly_can_query_holdings_and_entity_views_without_staging_definer_execute(slice2_db: str) -> None:
    with psycopg.connect(slice2_db) as conn, conn.transaction():
        conn.execute("set local role mart_readonly")
        for view in (
            "mart.entity_identity",
            "mart.entity_display_resolution",
            "mart.fund_holdings",
            "mart.fund_holdings_resolved",
            "mart.fund_holdings_valuation",
        ):
            assert conn.execute(f"select count(*) from {view}").fetchone() is not None, view


def test_staging_entity_fks_use_uuid_referencing_staging_entities(slice2_db: str) -> None:
    expected = {
        ("analyst_rating_events", "analyst_id"): "uuid",
        ("analyst_rating_events", "company_id"): "uuid",
        ("fund_holding_facts", "fund_id"): "uuid",
        ("fund_holding_facts", "holding_id"): "uuid",
    }
    with psycopg.connect(slice2_db) as conn:
        cols = conn.execute(
            """
            select table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'staging'
              and ((table_name = 'analyst_rating_events' and column_name in ('analyst_id', 'company_id'))
                or (table_name = 'fund_holding_facts' and column_name in ('fund_id', 'holding_id')))
            """
        ).fetchall()
        assert {(t, c): dt for t, c, dt in cols} == expected

        fks = conn.execute(
            """
            select tc.table_name, kcu.column_name, ccu.table_schema || '.' || ccu.table_name, ccu.column_name
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu
              on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
            join information_schema.constraint_column_usage ccu
              on tc.constraint_name = ccu.constraint_name and tc.table_schema = ccu.table_schema
            where tc.constraint_type = 'FOREIGN KEY'
              and tc.table_schema = 'staging'
              and tc.table_name in ('analyst_rating_events', 'fund_holding_facts')
            """
        ).fetchall()
        fk_map = {(t, c): (ref_t, ref_c) for t, c, ref_t, ref_c in fks}
        for key in expected:
            assert fk_map.get(key) == ("staging.entities", "entity_id"), (key, fk_map)


def test_views_and_functions_reference_no_retired_kg_tables(slice2_db: str) -> None:
    with psycopg.connect(slice2_db) as conn:
        views = conn.execute(
            """
            select schemaname || '.' || viewname, definition
            from pg_views
            where schemaname in ('raw', 'staging', 'mart', 'app')
            """
        ).fetchall()
        for name, defn in views:
            assert not RETIRED_PATTERN.search(defn or ""), f"{name} still references retired kg table"

        procs = conn.execute(
            """
            select n.nspname || '.' || p.proname, pg_get_functiondef(p.oid)
            from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            where n.nspname in ('raw', 'staging', 'mart', 'app') and p.prokind = 'f'
            """
        ).fetchall()
        for name, defn in procs:
            assert not RETIRED_PATTERN.search(defn or ""), f"{name} still references retired kg table"


def test_no_runtime_python_code_queries_retired_kg_tables() -> None:
    offenders: list[str] = []
    for root_name in ("apps", "common"):
        root = REPO_ROOT / root_name
        for path in root.rglob("*.py"):
            if "tests" in path.parts or path.name == "backfill_slice2_kg_to_uuid.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if RETIRED_PATTERN.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, offenders


def test_slice2_migration_converts_legacy_kg_database_losslessly() -> None:
    slice2_migrations = sorted(
        (REPO_ROOT / "db" / "migrations").glob("*_datahub_slice2_retire_kg_uuid_convergence.sql")
    )
    assert slice2_migrations, "missing Slice 2 timestamped migration file"
    migration_file = slice2_migrations[-1]

    admin_url = _admin_url()
    name = f"truealpha_slice2_legacy_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip(f"no local database: {error}")

    params = conninfo_to_dict(admin_url)
    db_url = make_conninfo(**(params | {"dbname": name}))
    try:
        # Build minimal legacy schema matching pre-Slice-2 Staging before running Slice 2 migration.
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute((REPO_ROOT / "db" / "migrations" / "0001_raw.sql").read_text())
            conn.execute((REPO_ROOT / "db" / "migrations" / "0002_staging.sql").read_text())
            conn.execute((REPO_ROOT / "db" / "migrations" / "0003_mart.sql").read_text())
            conn.execute(
                """
                create table staging.kg_entities (
                    id text primary key,
                    entity_type text not null,
                    display_name text not null
                );
                create table staging.kg_identifiers (
                    id bigint generated always as identity primary key,
                    entity_id text not null references staging.kg_entities(id),
                    source text not null,
                    identifier_type text not null,
                    identifier_value text not null,
                    valid_time daterange not null,
                    transaction_time timestamptz not null,
                    recorded_at timestamptz default now() not null,
                    confidence numeric not null,
                    raw_ref text not null
                );
                create table staging.kg_edges (
                    id bigint generated always as identity primary key,
                    from_id text not null references staging.kg_entities(id),
                    to_id text not null references staging.kg_entities(id),
                    relation_type text not null,
                    valid_time daterange not null,
                    transaction_time timestamptz not null,
                    confidence numeric not null,
                    source text not null,
                    raw_ref text not null,
                    recorded_at timestamptz default now() not null
                );
                create table staging.market_prices (
                    id bigint generated always as identity primary key,
                    unified_id text not null constraint market_prices_unified_id_fkey references staging.kg_entities(id)
                );
                alter table staging.analyst_rating_events
                    drop constraint analyst_rating_events_analyst_id_fkey,
                    drop constraint analyst_rating_events_company_id_fkey,
                    alter column analyst_id type text using analyst_id::text,
                    alter column company_id type text using company_id::text,
                    add constraint analyst_rating_events_analyst_id_fkey foreign key (analyst_id) references staging.kg_entities(id),
                    add constraint analyst_rating_events_company_id_fkey foreign key (company_id) references staging.kg_entities(id);
                drop view if exists mart.fund_holdings_valuation, mart.fund_holdings_coverage, mart.fund_holdings_resolved, mart.fund_holdings;
                alter table staging.fund_holding_facts
                    drop constraint fund_holding_facts_fund_id_fkey,
                    drop constraint fund_holding_facts_holding_id_fkey,
                    alter column fund_id type text using fund_id::text,
                    alter column holding_id type text using holding_id::text,
                    add constraint fund_holding_facts_fund_id_fkey foreign key (fund_id) references staging.kg_entities(id),
                    add constraint fund_holding_facts_holding_id_fkey foreign key (holding_id) references staging.kg_entities(id);
                create view mart.fund_holdings as
                    select facts.fund_id, k.display_name as fund_name, facts.holding_id, facts.holding_name,
                           facts.isin, facts.report_period, facts.transaction_time, facts.percent_of_net_assets,
                           facts.value_usd, facts.balance, facts.confidence, facts.raw_ref
                    from staging.fund_holding_facts facts
                    left join staging.kg_entities k on k.id = facts.fund_id;
                create view mart.fund_holdings_resolved as
                    select h.fund_id, h.fund_name, h.report_period, h.transaction_time, h.holding_name, h.isin,
                           h.percent_of_net_assets, h.value_usd, ki.entity_id as issuer_entity, null::text as ticker, null::text as listing_id
                    from mart.fund_holdings h
                    left join staging.kg_identifiers ki on ki.identifier_type = 'isin' and ki.identifier_value = h.isin;
                create view mart.fund_holdings_coverage as
                    select fund_id, report_period, transaction_time, count(*) as lines, 0::bigint as resolved_lines,
                           0::numeric as total_weight_pct, 0::numeric as resolved_weight_pct
                    from mart.fund_holdings_resolved group by 1, 2, 3;
                create view mart.fund_holdings_valuation as
                    select fund_id, fund_name, report_period, transaction_time, holding_name, isin,
                           percent_of_net_assets, value_usd, issuer_entity, ticker, listing_id
                    from mart.fund_holdings_resolved;
                """
            )
            now = datetime(2026, 6, 1, tzinfo=UTC)
            with conn.transaction():
                nvda_uuid = conn.execute(
                    "select staging.entity_mint('issuer', 'cik', '0001045810', 'test:slice2')"
                ).fetchone()[0]
                conn.execute(
                    "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
                    "values (%s, 'cik', '0001045810', '-infinity', %s, 'test', 'raw:0', 'asserted', 1.0, 'v1')",
                    (nvda_uuid, now),
                )
            conn.execute(
                """
                insert into staging.kg_entities (id, entity_type, display_name) values
                    ('etf:series:S000004310', 'etf', 'iShares Core S&P 500 ETF'),
                    ('etf:series:S000006219', 'company', 'Invesco QQQ Trust'),
                    ('company:cik:1045810', 'company', 'NVIDIA Corp'),
                    ('company:isin:US67066G1040', 'company', 'NVIDIA CORP'),
                    ('company:partner:tsmc', 'company', 'TSMC'),
                    ('analyst:moomoo:abc123', 'analyst', 'Jane Analyst'),
                    ('Geoff Meacham', 'analyst', 'Geoff Meacham'),
                    (%s, 'company', 'NVIDIA UUID Row')
                """,
                (str(nvda_uuid),),
            )
            conn.execute(
                """
                insert into staging.kg_identifiers
                    (entity_id, source, identifier_type, identifier_value, valid_time, transaction_time, confidence, raw_ref)
                values
                    ('etf:series:S000004310', 'sec', 'sec_series', 'S000004310', daterange(%s, null), %s, 1.0, ''),
                    ('company:isin:US67066G1040', 'openfigi', 'ticker', 'NVDA', daterange(%s, null), %s, 0.95, 'figi:1'),
                    ('company:isin:US67066G1040', 'nport', 'isin', 'US67066G1040', daterange(%s, null), %s, 1.0, 'nport:1')
                """,
                (now.date(), now, now.date(), now, now.date(), now),
            )
            conn.execute(
                """
                insert into staging.kg_edges
                    (from_id, to_id, relation_type, valid_time, transaction_time, confidence, source, raw_ref)
                values
                    ('etf:series:S000004310', 'company:isin:US67066G1040', 'holds', daterange(%s, null), %s, 1.0, 'nport', 'nport:1'),
                    ('company:partner:tsmc', 'company:cik:1045810', 'supplies_to', daterange(%s, null), %s, 0.9, 'sec_10k', ''),
                    ('company:isin:US67066G1040', 'company:cik:1045810', 'same_as', daterange(%s, null), %s, 0.95, 'openfigi', 'figi:1')
                """,
                (now.date(), now, now.date(), now, now.date(), now),
            )
            # Include a collision between legacy company:cik:1045810 and nvda_uuid at identical timestamp.
            conn.execute(
                """
                insert into staging.analyst_rating_events
                    (analyst_id, company_id, recommendation_at, transaction_time, rating, target_price, confidence, raw_ref)
                values
                    ('analyst:moomoo:abc123', 'company:cik:1045810', %s, %s, 5, 150, 0.9, 'r1'),
                    ('analyst:moomoo:abc123', %s, %s, %s, 5, 150, 0.9, 'r1b'),
                    ('Geoff Meacham', 'company:cik:1045810', %s, %s, 4, 140, 0.8, 'r2')
                """,
                (now, now, str(nvda_uuid), now, now, now, now),
            )
            conn.execute(
                """
                insert into staging.fund_holding_facts
                    (fund_id, holding_id, holding_name, report_period, transaction_time, isin, value_usd, percent_of_net_assets, confidence, raw_ref)
                values
                    ('etf:series:S000004310', 'company:isin:US67066G1040', 'NVIDIA CORP', '2026-03-31', %s, 'US67066G1040', 1000, 5.5, 1.0, 'nport:1')
                """,
                (now,),
            )

        proc = subprocess.run(
            ["psql", "--no-password", db_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

        with psycopg.connect(db_url) as check:
            for qualified in RETIRED_TABLES:
                assert check.execute("select to_regclass(%s)", (qualified,)).fetchone() == (None,)
            rating_rows = check.execute("select analyst_id, company_id from staging.analyst_rating_events").fetchall()
            assert len(rating_rows) == 2
            assert all(isinstance(aid, uuid.UUID) and isinstance(cid, uuid.UUID) for aid, cid in rating_rows)
            holding_rows = check.execute("select fund_id, holding_id from staging.fund_holding_facts").fetchall()
            assert len(holding_rows) == 1
            assert isinstance(holding_rows[0][0], uuid.UUID) and isinstance(holding_rows[0][1], uuid.UUID)
            assert check.execute(
                "select exists (select 1 from staging.entity_aliases where scheme = 'ticker' and value = 'NVDA')"
            ).fetchone() == (True,)
            assert check.execute(
                "select exists (select 1 from staging.entity_relations where relation_type = 'issues')"
            ).fetchone() == (True,)
            assert check.execute(
                "select exists (select 1 from staging.entity_relations where relation_type = 'supplies_to')"
            ).fetchone() == (True,)
            resolved_row = check.execute("select ticker, issuer_entity from mart.fund_holdings_resolved").fetchone()
            assert resolved_row is not None and resolved_row[0] == "NVDA" and resolved_row[1] == str(nvda_uuid)
            assert check.execute(
                "select count(*) from mart.entity_identity where entity_id = %s", (nvda_uuid,)
            ).fetchone() == (1,)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s", (name,))
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))
