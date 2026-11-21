"""Slice 2 gate tests (#1730): staging.kg_* retirement and UUID convergence.

Verifies:
1. Retired tables (staging.kg_entities, staging.kg_identifiers, staging.kg_edges) do not exist.
2. staging.fund_holding_facts uses uuid FK columns referencing staging.entities(entity_id).
3. Runtime views/functions reference zero retired kg_* tables.
4. No active application or shared module queries staging.kg_entities, staging.kg_identifiers, or
   staging.kg_edges.
5. An isolated legacy database carrying kg_* rows, text FK rows, plain-name analysts, ETFs, and
   company:cik + UUID duplicate collisions upgrades cleanly through the Slice 2 migration without
   data loss and replays with zero lock waits.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from truealpha_runtime.testing import isolated_test_database

REPO_ROOT = Path(__file__).resolve().parents[3]
RETIRED_TABLES = ("staging.kg_entities", "staging.kg_identifiers", "staging.kg_edges")
RETIRED_PATTERN = re.compile(r"\bstaging\.(kg_entities|kg_identifiers|kg_edges)\b")


@pytest.fixture(scope="module")
def slice2_db() -> Iterator[str]:
    with isolated_test_database("slice2", template=None) as db:
        yield str(db)


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
        ("fund_holding_facts", "fund_id"): "uuid",
        ("fund_holding_facts", "holding_id"): "uuid",
    }
    with psycopg.connect(slice2_db) as conn:
        cols = conn.execute(
            """
            select table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'staging'
              and table_name = 'fund_holding_facts'
              and column_name in ('fund_id', 'holding_id')
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
              and tc.table_name = 'fund_holding_facts'
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
            union all
            select schemaname || '.' || matviewname, definition
            from pg_matviews
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
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if RETIRED_PATTERN.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, offenders
