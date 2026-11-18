"""Slice 5 gate: retire write-only staging table and verify mart materialized views + concurrent refresh (#1730)."""

from __future__ import annotations

import os
import re
import subprocess
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
SLICE5_MIGRATION = REPO_ROOT / "db/migrations/20261118T1300_datahub_slice5_converge_staging_and_mart_mvs.sql"
ROLES_SQL = REPO_ROOT / "db/roles.sql"
EXPECTED_MVS = (
    ("entity_identity", "uq_mart_entity_identity"),
    ("fund_holdings", "uq_mart_fund_holdings"),
    ("fund_holdings_resolved", "uq_mart_fund_holdings_resolved"),
    ("fund_holdings_coverage", "uq_mart_fund_holdings_coverage"),
    ("fund_holdings_valuation", "uq_mart_fund_holdings_valuation"),
)


def _run_psql(db_url: str, sql_file: Path) -> None:
    proc = subprocess.run(
        ["psql", "--no-password", db_url, "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"{sql_file.name} failed:\n{proc.stdout}\n{proc.stderr}"


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
            ops_priv = conn.execute(
                "select has_table_privilege('app_ops_reader', %s, 'SELECT')",
                (f"mart.{view_name}",),
            ).fetchone()
            assert ops_priv == (True,), view_name


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

        # First CONCURRENTLY refresh (initial data)
        refreshed = refresh_mart_materialized_views(conn, MART_MV_REFRESH_ORDER, concurrently=True)
        assert refreshed == MART_MV_REFRESH_ORDER

        rows = conn.execute(
            "select cusip from mart.fund_holdings_valuation where fund_id = %s order by cusip",
            (str(fund_id),),
        ).fetchall()
        assert rows == [("CUSIP0001",), ("CUSIP0002",)]

        # Second CONCURRENTLY diff refresh over non-empty data
        conn.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_name, report_period, transaction_time, isin, cusip,
                 value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, 'Same Name', '2026-03-31', %s, null, 'CUSIP0003', 200, 2.0, 0.9, 'raw:mv')
            """,
            (fund_id, t0),
        )
        conn.commit()

        refreshed_second = refresh_mart_materialized_views(conn, MART_MV_REFRESH_ORDER, concurrently=True)
        assert refreshed_second == MART_MV_REFRESH_ORDER

        with conn.transaction():
            conn.execute("set local role mart_readonly")
            ro_rows = conn.execute(
                "select cusip from mart.fund_holdings_valuation where fund_id = %s order by cusip",
                (str(fund_id),),
            ).fetchall()
        assert ro_rows == [("CUSIP0001",), ("CUSIP0002",), ("CUSIP0003",)]


def test_slice5_migration_upgrades_pre_slice5_database_and_preserves_parity(tmp_path: Path) -> None:
    import shutil

    admin_url = _admin_url()
    name = f"truealpha_s5_upg_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    params = conninfo_to_dict(admin_url)
    db_url = make_conninfo(**(params | {"dbname": name}))
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs required DB tests")

    try:
        pre_db_dir = tmp_path / "db"
        shutil.copytree(
            REPO_ROOT / "db",
            pre_db_dir,
            ignore=lambda directory, names: (
                [n for n in names if n >= SLICE5_MIGRATION.name and n.endswith(".sql")]
                if Path(directory).name == "migrations"
                else []
            ),
        )
        pre_proc = subprocess.run(
            ["bash", str(REPO_ROOT / "db/apply_migrations.sh")],
            env={**os.environ, "DATABASE_URL": db_url, "TRUEALPHA_DB_DIR": str(pre_db_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert pre_proc.returncode == 0, (
            f"pre-Slice-5 apply_migrations.sh failed:\n{pre_proc.stdout}\n{pre_proc.stderr}"
        )

        t0 = datetime(2026, 6, 1, tzinfo=UTC)
        with psycopg.connect(db_url) as conn:
            # Recreate legacy staging.analyst_rating_events present on live pre-Slice-5 databases.
            conn.execute(
                """
                create table if not exists staging.analyst_rating_events (
                    id bigint generated always as identity primary key,
                    analyst_id uuid not null,
                    as_of date not null
                )
                """
            )
            pre_kinds = dict(
                conn.execute(
                    """
                    select c.relname, c.relkind
                    from pg_class c
                    join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = 'mart' and c.relname = any(%s)
                    """,
                    ([view_name for view_name, _ in EXPECTED_MVS],),
                ).fetchall()
            )
            assert pre_kinds == {view_name: "v" for view_name, _ in EXPECTED_MVS}, pre_kinds

            fund_id = conn.execute(
                "select staging.entity_mint('fund', 'name', 'Upgrade Fund', 'test:s5upg')"
            ).fetchone()[0]
            issuer_id = conn.execute(
                "select staging.entity_mint('issuer', 'cik', '0000320193', 'test:s5upg')"
            ).fetchone()[0]
            instrument_id = conn.execute(
                "select staging.entity_mint('instrument', 'isin', 'US0378331005', 'test:s5upg')"
            ).fetchone()[0]
            listing_id = conn.execute(
                "select staging.entity_mint('listing', 'mic-ticker', 'XNAS:AAPL', 'test:s5upg')"
            ).fetchone()[0]
            for eid, scheme, value in (
                (fund_id, "name", "Upgrade Fund"),
                (issuer_id, "cik", "0000320193"),
                (issuer_id, "name", "Apple Inc."),
                (instrument_id, "isin", "US0378331005"),
                (listing_id, "mic-ticker", "XNAS:AAPL"),
                (listing_id, "ticker", "AAPL"),
            ):
                conn.execute(
                    """
                    insert into staging.entity_aliases
                        (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
                    values (%s, %s, %s, '-infinity', %s, 'test', 'raw:s5upg', 'asserted', 0.9, 'v1')
                    on conflict do nothing
                    """,
                    (eid, scheme, value, t0),
                )
            for rel_type, from_id, to_id in (
                ("issues", issuer_id, instrument_id),
                ("listed_as", instrument_id, listing_id),
            ):
                conn.execute(
                    """
                    insert into staging.entity_relations
                        (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
                    values (
                        staging.entity_relation_uuid(%s, %s, %s, '-infinity', %s, 'test', 'asserted'),
                        %s, %s, %s, '-infinity', %s, 'test', 'raw:s5upg', 'asserted', 0.9, 'v1'
                    )
                    """,
                    (rel_type, from_id, to_id, t0, rel_type, from_id, to_id, t0),
                )
            conn.execute(
                """
                insert into staging.fund_holding_facts
                    (fund_id, holding_id, holding_name, report_period, transaction_time, isin, cusip,
                     value_usd, percent_of_net_assets, confidence, raw_ref)
                values
                    (%s, %s, 'Apple Inc.', '2026-03-31', %s, 'US0378331005', '037833100', 500, 5.0, 0.95, 'raw:1'),
                    (%s, null, 'Bond A', '2026-03-31', %s, null, 'BOND00001', 100, 1.0, 0.8, 'raw:2'),
                    (%s, null, 'Bond A', '2026-03-31', %s, null, 'BOND00002', 200, 2.0, 0.8, 'raw:3')
                """,
                (fund_id, instrument_id, t0, fund_id, t0, fund_id, t0),
            )
            baseline_identity = conn.execute(
                "select entity_id, kind, current_ticker, name, cik, lei, listing_id, legacy_id "
                "from mart.entity_identity order by entity_id"
            ).fetchall()
            baseline_valuation = conn.execute(
                "select fund_id, fund_name, report_period, transaction_time, holding_name, isin, "
                "percent_of_net_assets, value_usd, issuer_entity, ticker, listing_id "
                "from mart.fund_holdings_valuation order by fund_id, holding_name, value_usd"
            ).fetchall()
            baseline_coverage = conn.execute(
                "select fund_id, report_period, transaction_time, lines, resolved_lines, "
                "total_weight_pct, resolved_weight_pct "
                "from mart.fund_holdings_coverage order by fund_id"
            ).fetchall()
            conn.commit()

        # Run Slice 5 migration BEFORE db/roles.sql and check both immediate grants and replay idempotency.
        _run_psql(db_url, SLICE5_MIGRATION)
        with psycopg.connect(db_url) as conn:
            assert conn.execute("select to_regclass('staging.analyst_rating_events')").fetchone() == (None,)
            for view_name, _ in EXPECTED_MVS:
                assert conn.execute(
                    "select has_table_privilege('mart_readonly', %s, 'SELECT')", (f"mart.{view_name}",)
                ).fetchone() == (True,), view_name
                assert conn.execute(
                    "select has_table_privilege('app_ops_reader', %s, 'SELECT')", (f"mart.{view_name}",)
                ).fetchone() == (True,), view_name

            upgraded_identity = conn.execute(
                "select entity_id, kind, current_ticker, name, cik, lei, listing_id, legacy_id "
                "from mart.entity_identity order by entity_id"
            ).fetchall()
            upgraded_valuation = conn.execute(
                "select fund_id, fund_name, report_period, transaction_time, holding_name, isin, "
                "percent_of_net_assets, value_usd, issuer_entity, ticker, listing_id "
                "from mart.fund_holdings_valuation order by fund_id, holding_name, value_usd"
            ).fetchall()
            upgraded_coverage = conn.execute(
                "select fund_id, report_period, transaction_time, lines, resolved_lines, "
                "total_weight_pct, resolved_weight_pct "
                "from mart.fund_holdings_coverage order by fund_id"
            ).fetchall()
            assert upgraded_identity == baseline_identity
            assert upgraded_valuation == baseline_valuation
            assert upgraded_coverage == baseline_coverage

        # Replay migration idempotently + apply roles.sql
        _run_psql(db_url, SLICE5_MIGRATION)
        _run_psql(db_url, ROLES_SQL)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
