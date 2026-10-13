"""The retire migration drops staging.financial_facts and never waits for a lock (#1258).

staging.financial_facts held 0 rows in Production and Staging on 2026-10-12 (#1237).
All four of its original writers were deleted in #731 (#739). No code reads or writes it.

The chain replays on every boot. Two rules follow:
* An old migration must not create the retired relation. A later drop would churn on every boot.
* The drop must never wait for a lock, and it must leave the table if it holds any row.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from truealpha_runtime.testing import apply_migration_chain, guard_db_write

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "migrations" / "20261013T0700_datahub_retire_staging_financial_facts.sql"
RUNTIME_CONTRACT = REPO_ROOT / "db" / "tests" / "runtime_contract.sql"


def _named(database: str) -> str:
    base = urlsplit(guard_db_write())
    return urlunsplit((base.scheme, base.netloc, f"/{database}", base.query, ""))


def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(_named("postgres"), connect_timeout=5, autocommit=True) as admin:
        admin.execute(statement)


def _drop(database: str) -> None:
    _admin(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))


def _scratch_name(kind: str) -> str:
    return f"truealpha_test_fin_facts_{kind}_{os.getpid()}"


@pytest.fixture
def clean_database() -> str:
    dbname = _scratch_name("clean")
    _drop(dbname)
    _admin(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    try:
        apply_migration_chain(_named(dbname))
        yield dbname
    finally:
        _drop(dbname)


def test_a_fresh_chain_does_not_create_staging_financial_facts(clean_database: str) -> None:
    """A clean migration chain from scratch must never recreate the retired table."""
    with psycopg.connect(_named(clean_database), connect_timeout=5) as conn:
        row = conn.execute("select to_regclass('staging.financial_facts')").fetchone()
        assert row is not None
        assert row[0] is None, f"staging.financial_facts was recreated by migration chain: {row[0]}"


def test_runtime_contract_sql_passes_and_verifies_financial_facts_absence(clean_database: str) -> None:
    """runtime_contract.sql verifies staging.financial_facts is absent and transaction_time is explicit."""
    with psycopg.connect(_named(clean_database), connect_timeout=5) as conn:
        # Must execute without exception
        conn.execute(RUNTIME_CONTRACT.read_text())


def test_the_retire_migration_drops_empty_staging_financial_facts() -> None:
    """When staging.financial_facts exists and holds 0 rows, the migration drops it."""
    dbname = _scratch_name("drop_empty")
    _drop(dbname)
    _admin(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    try:
        url = _named(dbname)
        apply_migration_chain(url)
        # Artificially create staging.financial_facts with 0 rows as an older shape
        with psycopg.connect(url, connect_timeout=5, autocommit=True) as conn:
            conn.execute(
                """
                create table if not exists staging.financial_facts (
                    id bigint generated always as identity primary key,
                    unified_id text not null references staging.kg_entities(id),
                    metric text not null,
                    fiscal_period text not null,
                    valid_time daterange not null,
                    transaction_time timestamptz not null,
                    value numeric,
                    confidence numeric not null,
                    source text not null,
                    raw_ref text,
                    is_restatement boolean not null default false
                );
                """
            )
            assert conn.execute("select to_regclass('staging.financial_facts')").fetchone()[0] is not None

            # Apply the retire migration directly
            conn.execute(MIGRATION.read_text())

            # Verify it was dropped
            assert conn.execute("select to_regclass('staging.financial_facts')").fetchone()[0] is None
    finally:
        _drop(dbname)


def test_the_retire_migration_keeps_populated_staging_financial_facts() -> None:
    """When staging.financial_facts holds data, the migration refuses to drop it and emits a warning."""
    dbname = _scratch_name("keep_populated")
    _drop(dbname)
    _admin(sql.SQL("create database {}").format(sql.Identifier(dbname)))
    try:
        url = _named(dbname)
        apply_migration_chain(url)
        with psycopg.connect(url, connect_timeout=5, autocommit=True) as conn:
            # Seed a parent entity
            conn.execute(
                """
                insert into staging.kg_entities (id, entity_type, display_name)
                values ('issuer:test:keep', 'company', 'Keep Corp')
                on conflict do nothing;
                """
            )
            # Create table and populate with 1 row
            conn.execute(
                """
                create table if not exists staging.financial_facts (
                    id bigint generated always as identity primary key,
                    unified_id text not null references staging.kg_entities(id),
                    metric text not null,
                    fiscal_period text not null,
                    valid_time daterange not null,
                    transaction_time timestamptz not null,
                    value numeric,
                    confidence numeric not null,
                    source text not null,
                    raw_ref text,
                    is_restatement boolean not null default false
                );
                insert into staging.financial_facts (
                    unified_id, metric, fiscal_period, valid_time, transaction_time,
                    value, confidence, source, raw_ref
                ) values (
                    'issuer:test:keep', 'revenue', '2026Q1', daterange('2026-01-01', '2026-03-31'),
                    now(), 1000, 1.0, 'sec', 'raw.fetches:1'
                );
                """
            )
            assert conn.execute("select count(*) from staging.financial_facts").fetchone()[0] == 1

            # Run migration; table must survive
            conn.execute(MIGRATION.read_text())
            assert conn.execute("select to_regclass('staging.financial_facts')").fetchone()[0] is not None
            assert conn.execute("select count(*) from staging.financial_facts").fetchone()[0] == 1
    finally:
        _drop(dbname)
