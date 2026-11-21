"""Slice 5 gate: retire write-only staging table and verify mart materialized views + concurrent refresh (#1730)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from truealpha_runtime.mart_refresh import MART_MV_REFRESH_ORDER, refresh_mart_materialized_views
from truealpha_runtime.testing import apply_migration_chain, isolated_test_database

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_MVS = (
    ("entity_identity", "uq_mart_entity_identity"),
    ("fund_holdings", "uq_mart_fund_holdings"),
    ("fund_holdings_resolved", "uq_mart_fund_holdings_resolved"),
    ("fund_holdings_coverage", "uq_mart_fund_holdings_coverage"),
    ("fund_holdings_valuation", "uq_mart_fund_holdings_valuation"),
)


@pytest.fixture(scope="module")
def slice5_db() -> Iterator[str]:
    with isolated_test_database("slice5", template=None) as db:
        yield str(db)


def test_analyst_rating_events_retired_from_runtime_and_schema(slice5_db: str) -> None:
    pattern = re.compile(r"\banalyst_rating_events\b")
    exempt = {
        Path(__file__).resolve(),
        (REPO_ROOT / "common/runtime/tests/test_slice2_kg_retirement.py").resolve(),
        (REPO_ROOT / "apps/data-engine/tests/test_analyst_ratings.py").resolve(),
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


def test_squashed_baseline_replays_idempotently_and_preserves_mv_grants(slice5_db: str) -> None:
    apply_migration_chain(slice5_db)
    with psycopg.connect(slice5_db) as conn:
        assert conn.execute("select to_regclass('staging.analyst_rating_events')").fetchone() == (None,)
        for view_name, _ in EXPECTED_MVS:
            assert conn.execute(
                "select has_table_privilege('mart_readonly', %s, 'SELECT')", (f"mart.{view_name}",)
            ).fetchone() == (True,), view_name
            assert conn.execute(
                "select has_table_privilege('app_ops_reader', %s, 'SELECT')", (f"mart.{view_name}",)
            ).fetchone() == (True,), view_name


def test_squashed_baseline_upgrades_pre_mv_regular_views_and_retires_analyst_rating_events() -> None:
    """Replaying the 4-file baseline on a pre-Slice-5 DB (v0.2.114 Production shape) converts
    regular views to materialized views cleanly without crashing on CREATE UNIQUE INDEX."""
    with isolated_test_database("slice5_upg", template=None) as db_url:
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute("create table staging.analyst_rating_events (id bigint primary key)")
            for rel in (
                "mart.entity_identity",
                "mart.fund_holdings_valuation",
                "mart.fund_holdings_coverage",
                "mart.fund_holdings_resolved",
                "mart.fund_holdings",
            ):
                conn.execute(f"drop materialized view {rel}")
            conn.execute("create view mart.entity_identity as select null::text as entity_id, null::text as listing_id")
            conn.execute(
                "create view mart.fund_holdings as select null::text as fund_id, null::date as report_period, "
                "null::timestamptz as transaction_time, null::text as isin, null::text as cusip, null::text as holding_name"
            )
            conn.execute("create view mart.fund_holdings_resolved as select * from mart.fund_holdings")
            conn.execute(
                "create view mart.fund_holdings_coverage as select fund_id, report_period, transaction_time from mart.fund_holdings_resolved"
            )
            conn.execute("create view mart.fund_holdings_valuation as select * from mart.fund_holdings_resolved")

        # Upgrade via the production 4-file chain (previously failed at CREATE UNIQUE INDEX ON mart.entity_identity).
        apply_migration_chain(db_url)
        # Replay once more to confirm zero-change idempotency.
        apply_migration_chain(db_url)

        with psycopg.connect(db_url) as conn:
            assert conn.execute("select to_regclass('staging.analyst_rating_events')").fetchone() == (None,)
            rels = dict(
                conn.execute(
                    """
                    select c.relname, c.relkind
                    from pg_class c
                    join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = 'mart' and c.relname = any(%s)
                    """,
                    ([rel for rel, _ in EXPECTED_MVS],),
                ).fetchall()
            )
            assert rels == {rel: "m" for rel, _ in EXPECTED_MVS}, rels
            idxs = {
                idx
                for (idx,) in conn.execute(
                    "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'mart' and c.relname = any(%s)",
                    ([idx for _, idx in EXPECTED_MVS],),
                ).fetchall()
            }
            assert idxs == {idx for _, idx in EXPECTED_MVS}, idxs


class _FakeInfo:
    def __init__(self, status: TransactionStatus) -> None:
        self.transaction_status = status


class _FakeResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, str]]:
        return self._rows


class _FakeMvConnection:
    def __init__(self, status: TransactionStatus, *, autocommit: bool = False) -> None:
        self.info = _FakeInfo(status)
        self.autocommit = autocommit
        self.executed: list[str] = []
        self.committed = 0

    def commit(self) -> None:
        self.committed += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def execute(self, statement: Any, _params: Any = None) -> _FakeResult:
        rendered = statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        self.executed.append(rendered)
        if "from pg_class" in rendered:
            return _FakeResult([("mart", "entity_identity")])
        return _FakeResult([])


@pytest.mark.parametrize(
    ("status", "commit_before", "expect_concurrently", "expect_commit"),
    [
        (TransactionStatus.IDLE, False, True, 0),
        (TransactionStatus.INTRANS, False, False, 0),
        (TransactionStatus.INTRANS, True, True, 1),
        (TransactionStatus.INERROR, False, False, 0),
        (TransactionStatus.ACTIVE, False, False, 0),
        (TransactionStatus.UNKNOWN, False, False, 0),
    ],
)
def test_refresh_mart_materialized_views_respects_transaction_status(
    status: TransactionStatus,
    commit_before: bool,
    expect_concurrently: bool,
    expect_commit: int,
) -> None:
    """Only TransactionStatus.IDLE may run REFRESH MATERIALIZED VIEW CONCURRENTLY (#1730, #1774)."""
    conn = _FakeMvConnection(status, autocommit=False)
    refreshed = refresh_mart_materialized_views(
        conn,
        ("mart.entity_identity",),
        concurrently=True,
        commit_before=commit_before,
    )
    assert refreshed == ("mart.entity_identity",)
    assert conn.committed == expect_commit
    refresh_sql = [stmt for stmt in conn.executed if "refresh materialized view" in stmt.lower()]
    assert len(refresh_sql) == 1
    assert ("concurrently" in refresh_sql[0].lower()) is expect_concurrently
    assert conn.autocommit is False


def test_composition_run_capture_pipeline_commits_before_concurrent_mv_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """composition.run_capture_pipeline must commit before refreshing entity_identity concurrently (#1774)."""
    from decimal import Decimal
    from types import SimpleNamespace

    from data_engine.datahub.capture_core import composition

    conn = _FakeMvConnection(TransactionStatus.INTRANS, autocommit=False)
    plan = SimpleNamespace(
        run_id=f"capture-run:{'0' * 64}",
        source_label="src:1",
        timeline=None,
        retry=None,
        freshness_windows={},
        default_freshness_max_age=None,
        coordinates={"listing:xnas:aapl": ("i", "ins", "l", "AAPL")},
        raw_coordinates={},
        bindings={},
        work_items=[],
        release_manifest_id="rm:1",
        forced_fetch=False,
    )
    status = SimpleNamespace(terminal_count=0, complete=True, success_count=1, unchanged_count=0, obligation_count=1)
    monkeypatch.setattr(composition, "plan_and_persist", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        composition,
        "PostgresCaptureControlRepository",
        lambda *_a, **_k: SimpleNamespace(status=lambda _run_id: status),
    )
    monkeypatch.setattr(composition, "PostgresCaptureControlSink", lambda *_a, **_k: None)
    monkeypatch.setattr(
        composition,
        "PostgresEvidenceGraphRepository",
        lambda *_a, **_k: SimpleNamespace(append=lambda *_a2, **_k2: None),
    )
    monkeypatch.setattr(composition, "risk_free_rate_at", lambda *_a, **_k: SimpleNamespace(value=Decimal("0.05")))
    monkeypatch.setattr(
        composition,
        "PostgresCoreRepository",
        lambda *_a, **_k: SimpleNamespace(
            freeze_snapshot=lambda **_k2: SimpleNamespace(),
            materialize=lambda *_a2, **_k2: [1],
        ),
    )
    monkeypatch.setattr(composition.quality_report, "build_report", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(composition.quality_report, "persist", lambda *_a, **_k: "qr:1")

    composition.run_capture_pipeline(
        conn,  # type: ignore[arg-type]
        cutoff=datetime(2026, 6, 1, tzinfo=UTC),
        version="v1",
    )

    assert conn.committed == 1
    refresh_sql = [stmt for stmt in conn.executed if "refresh materialized view" in stmt.lower()]
    assert len(refresh_sql) == 1
    assert "concurrently" in refresh_sql[0].lower()
