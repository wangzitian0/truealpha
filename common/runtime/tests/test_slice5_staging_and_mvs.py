"""Slice 5 gate: retire write-only staging table and verify mart materialized views + concurrent refresh (#1730)."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_runtime.mart_refresh import MART_MV_REFRESH_ORDER, refresh_mart_materialized_views
from truealpha_runtime.testing import apply_migration_chain, guard_db_write

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_MVS = (
    ("entity_identity", "uq_mart_entity_identity"),
    ("fund_holdings", "uq_mart_fund_holdings"),
    ("fund_holdings_resolved", "uq_mart_fund_holdings_resolved"),
    ("fund_holdings_coverage", "uq_mart_fund_holdings_coverage"),
    ("fund_holdings_valuation", "uq_mart_fund_holdings_valuation"),
)


def _admin_url() -> str:
    params = conninfo_to_dict(guard_db_write())
    return make_conninfo(**(params | {"dbname": "postgres"}))


@pytest.fixture(scope="module")
def slice5_db() -> Generator[str]:
    admin_url = _admin_url()
    name = f"truealpha_slice5_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def test_analyst_rating_events_retired_from_runtime_and_schema(slice5_db: str) -> None:
    pattern = re.compile(r"\banalyst_rating_events\b")
    exempt = {
        Path(__file__).resolve(),
        (REPO_ROOT / "common/runtime/tests/test_served_head_guard.py").resolve(),
        (REPO_ROOT / "common/runtime/tests/test_slice2_kg_retirement.py").resolve(),
        (REPO_ROOT / "apps/data-engine/tests/test_analyst_ratings.py").resolve(),
        (REPO_ROOT / "apps/data-engine/scripts/backfill_slice2_kg_to_uuid.py").resolve(),
    }
    offenders: list[str] = []
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common"):
        for path in sorted(root.rglob("*.py")):
            if path.resolve() in exempt or path.name == "__init__.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == [], f"retired staging.analyst_rating_events still referenced: {offenders}"

    with psycopg.connect(slice5_db) as conn:
        assert conn.execute("select to_regclass('staging.analyst_rating_events')").fetchone() == (None,)


def test_five_mart_projections_are_materialized_with_nulls_not_distinct_unique_indexes(slice5_db: str) -> None:
    with psycopg.connect(slice5_db) as conn:
        rels = dict(
            conn.execute(
                """
                select c.relname, c.relkind
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'mart' and c.relname = any(%s)
                """,
                ([name for name, _ in EXPECTED_MVS],),
            ).fetchall()
        )
        assert rels == {name: "m" for name, _ in EXPECTED_MVS}, rels

        idxs = {
            name: (is_unique, nulls_not_distinct, is_valid)
            for name, is_unique, nulls_not_distinct, is_valid in conn.execute(
                """
                select ic.relname, i.indisunique, i.indnullsnotdistinct, i.indisvalid
                from pg_index i
                join pg_class ic on ic.oid = i.indexrelid
                join pg_namespace n on n.oid = ic.relnamespace
                where n.nspname = 'mart' and ic.relname = any(%s)
                """,
                ([idx for _, idx in EXPECTED_MVS],),
            ).fetchall()
        }
        assert idxs == {idx: (True, True, True) for _, idx in EXPECTED_MVS}, idxs

        for view_name, _ in EXPECTED_MVS:
            priv = conn.execute(
                "select has_table_privilege('mart_readonly', %s, 'SELECT')",
                (f"mart.{view_name}",),
            ).fetchone()
            assert priv == (True,), view_name


def test_concurrent_refresh_and_multi_cusip_uniqueness(slice5_db: str) -> None:
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    with psycopg.connect(slice5_db) as conn:
        fund_id = conn.execute("select staging.entity_mint('fund', 'name', 'Test MV Fund', 'test:mv')").fetchone()[0]
        conn.execute(
            """
            insert into staging.entity_aliases
                (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
            values (%s, 'name', 'Test MV Fund', '-infinity', %s, 'test', 'raw:1', 'asserted', 0.9, 'v1')
            on conflict do nothing
            """,
            (fund_id, t0),
        )
        for cusip in ("CUSIP0001", "CUSIP0002"):
            conn.execute(
                """
                insert into staging.fund_holding_facts
                    (fund_id, holding_name, report_period, transaction_time, isin, cusip,
                     value_usd, percent_of_net_assets, confidence, raw_ref)
                values (%s, 'Same Name', '2026-03-31', %s, null, %s, 100, 1.0, 0.9, 'raw:mv')
                """,
                (fund_id, t0, cusip),
            )
        conn.commit()

        refreshed = refresh_mart_materialized_views(conn, MART_MV_REFRESH_ORDER, concurrently=True, commit_before=True)
        assert refreshed == MART_MV_REFRESH_ORDER

        rows = conn.execute(
            "select cusip from mart.fund_holdings_valuation where fund_id = %s order by cusip",
            (str(fund_id),),
        ).fetchall()
        assert rows == [("CUSIP0001",), ("CUSIP0002",)]
