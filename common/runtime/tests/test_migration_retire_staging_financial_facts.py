# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; from pathlib import Path; from urllib.parse import urlsplit, urlunsplit; import psycopg; import pytest; from psycopg import sql; from truealpha_runtime.testing import apply_migration_chain, guard_db_write; REPO_ROOT = Path(__file__).resolve().parents[3]; MIGRATION = REPO_ROOT / "db" / "migrations" / "20261013T0700_datahub_retire_staging_financial_facts.sql"; RUNTIME_CONTRACT = REPO_ROOT / "db" / "tests" / "runtime_contract.sql"
def _named(database: str) -> str: base = urlsplit(guard_db_write()); return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))
def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(_named("postgres"), connect_timeout=5, autocommit=True) as admin: admin.execute(statement)
def _drop(database: str) -> None: _admin(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))
def _scratch_name(kind: str) -> str: return f"truealpha_test_fin_facts_{kind}_{os.getpid()}"
@pytest.fixture
def clean_database() -> str:
    dbname = _scratch_name("clean"); _drop(dbname); _admin(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    try: apply_migration_chain(_named(dbname)); yield dbname
    finally: _drop(dbname)
def test_a_fresh_chain_does_not_create_staging_financial_facts(clean_database: str) -> None:
    with psycopg.connect(_named(clean_database), connect_timeout=5) as conn: row = conn.execute("select to_regclass('staging.financial_facts')").fetchone(); assert row is not None; assert row[0] is None, f"staging.financial_facts was recreated by migration chain: {row[0]}"
def test_runtime_contract_sql_passes_and_verifies_financial_facts_absence(clean_database: str) -> None:
    with psycopg.connect(_named(clean_database), connect_timeout=5) as conn: conn.execute(RUNTIME_CONTRACT.read_text())
def test_the_retire_migration_drops_empty_staging_financial_facts() -> None:
    dbname = _scratch_name("drop_empty"); _drop(dbname); _admin(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    try:
        url = _named(dbname); apply_migration_chain(url)
        with psycopg.connect(url, connect_timeout=5, autocommit=True) as conn: conn.execute( '\n                create table if not exists staging.financial_facts (\n                    id bigint generated always as identity primary key,\n                    unified_id text not null references staging.kg_entities(id),\n                    metric text not null,\n                    fiscal_period text not null,\n                    valid_time daterange not null,\n                    transaction_time timestamptz not null,\n                    value numeric,\n                    confidence numeric not null,\n                    source text not null,\n                    raw_ref text,\n                    is_restatement boolean not null default false\n                );\n                ' ); assert conn.execute("select to_regclass('staging.financial_facts')").fetchone()[0] is not None; conn.execute(MIGRATION.read_text()); assert conn.execute("select to_regclass('staging.financial_facts')").fetchone()[0] is None
    finally: _drop(dbname)
def test_the_retire_migration_keeps_populated_staging_financial_facts() -> None:
    dbname = _scratch_name("keep_populated"); _drop(dbname); _admin(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    try:
        url = _named(dbname); apply_migration_chain(url)
        with psycopg.connect(url, connect_timeout=5, autocommit=True) as conn: conn.execute( "\n                insert into staging.kg_entities (id, entity_type, display_name)\n                values ('issuer:test:keep', 'company', 'Keep Corp')\n                on conflict do nothing;\n                " ); conn.execute( "\n                create table if not exists staging.financial_facts (\n                    id bigint generated always as identity primary key,\n                    unified_id text not null references staging.kg_entities(id),\n                    metric text not null,\n                    fiscal_period text not null,\n                    valid_time daterange not null,\n                    transaction_time timestamptz not null,\n                    value numeric,\n                    confidence numeric not null,\n                    source text not null,\n                    raw_ref text,\n                    is_restatement boolean not null default false\n                );\n                insert into staging.financial_facts (\n                    unified_id, metric, fiscal_period, valid_time, transaction_time,\n                    value, confidence, source, raw_ref\n                ) values (\n                    'issuer:test:keep', 'revenue', '2026Q1', daterange('2026-01-01', '2026-03-31'),\n                    now(), 1000, 1.0, 'sec', 'raw.fetches:1'\n                );\n                " ); assert conn.execute("select count(*) from staging.financial_facts").fetchone()[0] == 1; conn.execute(MIGRATION.read_text()); assert conn.execute("select to_regclass('staging.financial_facts')").fetchone()[0] is not None; assert conn.execute("select count(*) from staging.financial_facts").fetchone()[0] == 1
    finally: _drop(dbname)
