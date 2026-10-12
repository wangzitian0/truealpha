"""Integration tests for backtest execution and persistence (#758, Milestone M3).

This module tests multi-asset price panels, dual-resolution masks, and fundamental factor joins.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.backtest import _load_mask, _resolve_snapshot_hash, run_backtest
from data_engine.datahub.market_prices import SPLIT_ADJUSTED
from data_engine.lanes.backtest import NightlyBacktestConfig, run_nightly_backtest_op
from data_engine.quality.nightly_verdicts import TICK_TAG


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch):
    """Yield an isolated transaction connection for testing."""
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    # Intercept commit so the test transaction stays strictly uncommitted and rolls back cleanly at teardown
    monkeypatch.setattr(active, "commit", lambda: None)
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _insert_fixture_data(conn: psycopg.Connection, symbols: list[str], dates: list[date]) -> None:
    """Insert monthly price bars and dual-resolution masks."""
    with conn.cursor() as cur:
        for sym in symbols:
            for d in dates:
                cur.execute(
                    """
                    insert into staging.market_prices_monthly (
                        symbol, trading_date, open, high, low, close, volume,
                        source, adjust, transaction_time, confidence, raw_ref
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sym,
                        d,
                        Decimal("100.0"),
                        Decimal("110.0"),
                        Decimal("90.0"),
                        Decimal("100.0"),
                        Decimal("50000"),
                        "twelvedata",
                        SPLIT_ADJUSTED,
                        datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC),
                        Decimal("0.85"),
                        "test_ref",
                    ),
                )
                # Insert 1D and 1M masks to verify dual-resolution filtering
                cur.execute(
                    """
                    insert into staging.universe_mask (
                        symbol, cutoff_date, resolution, eligible, reason_code
                    ) values (%s, %s, %s, %s, %s)
                    on conflict (symbol, cutoff_date, resolution) do update
                    set eligible = excluded.eligible, reason_code = excluded.reason_code
                    """,
                    (sym, d, "1M", True, "ok"),
                )
                cur.execute(
                    """
                    insert into staging.universe_mask (
                        symbol, cutoff_date, resolution, eligible, reason_code
                    ) values (%s, %s, %s, %s, %s)
                    on conflict (symbol, cutoff_date, resolution) do update
                    set eligible = excluded.eligible, reason_code = excluded.reason_code
                    """,
                    (sym, d, "1D", True, "ok"),
                )


def test_load_mask_filters_by_resolution(connection: psycopg.Connection) -> None:
    """_load_mask filters rows by resolution to prevent duplicate entries."""
    symbols = ["MASK_SYM_A", "MASK_SYM_B"]
    dates = [date(2023, 1, 31), date(2023, 2, 28)]
    _insert_fixture_data(connection, symbols, dates)

    # 1M mask returns exactly one row per symbol and date
    mask_1m = _load_mask(connection, symbols, resolution="1M")
    assert len(mask_1m) == len(symbols) * len(dates)

    # 1D mask also returns exactly one row per symbol and date
    mask_1d = _load_mask(connection, symbols, resolution="1D")
    assert len(mask_1d) == len(symbols) * len(dates)


def test_backtest_with_dual_resolution_masks(connection: psycopg.Connection) -> None:
    """run_backtest executes cleanly without duplicate index errors when dual masks exist."""
    symbols = ["AAPL", "MSFT", "NVDA"]
    dates = [date(2023, 1, 31), date(2023, 2, 28), date(2023, 3, 31)]
    _insert_fixture_data(connection, symbols, dates)

    result = run_backtest(
        connection,
        strategy_key="demo_topk_rank",
        strategy_version="v1",
        universe_id="topt",
        symbols=symbols,
        factor_column="close",
        top_k=2,
        dropout_k=1,
    )

    assert result.status == "succeeded"
    assert result.strategy_key == "demo_topk_rank"
    assert len(result.valuations_monthly) == len(dates)
    assert result.metrics_payload["initial_cash"] == 10_000_000.0


def test_backtest_snapshot_hash_forbids_default_snapshot(connection: psycopg.Connection) -> None:
    """Snapshot hash resolution never returns default_snapshot per ADR A6 Decision 7."""
    symbols = ["AAPL"]
    dates = [date(2023, 1, 31)]
    _insert_fixture_data(connection, symbols, dates)

    result = run_backtest(
        connection,
        strategy_key="demo_topk_rank",
        strategy_version="v1",
        universe_id="topt",
        symbols=symbols,
        factor_column="close",
        top_k=1,
        dropout_k=1,
    )

    assert "default_snapshot" not in result.run_id
    assert not result.run_id.endswith("default_snapshot")

    # Explicit test of _resolve_snapshot_hash helper
    import polars as pl

    df = pl.DataFrame({"symbol": ["AAPL"], "date": [date(2023, 1, 31)], "close": [150.0]})
    resolved = _resolve_snapshot_hash(connection, "topt", df)
    assert resolved != "default_snapshot"
    assert len(resolved) > 0


def test_backtest_with_fundamental_factors(connection: psycopg.Connection) -> None:
    """run_backtest integrates fundamental factors and handles backward alignment."""
    symbols = ["FUND_A", "FUND_B"]
    dates = [date(2023, 3, 31), date(2023, 4, 28), date(2023, 5, 31)]
    _insert_fixture_data(connection, symbols, dates)

    # Insert fundamental inputs at quarter-end (2023-03-31)
    with connection.cursor() as cur:
        cur.execute("delete from staging.strategy_backtest_inputs where issuer_id = any(%s)", (symbols,))
        for sym, gppe_val in [("FUND_A", 0.45), ("FUND_B", 0.35)]:
            cur.execute(
                """
                insert into staging.strategy_backtest_inputs (
                    input_key, issuer_id, cutoff_at, knowable_at, recorded_at,
                    value, confidence, fiscal_period
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "gppe",
                    sym,
                    datetime(2023, 3, 31, 23, 59, tzinfo=UTC),
                    datetime(2023, 3, 31, 23, 59, tzinfo=UTC),
                    datetime(2023, 3, 31, 23, 59, tzinfo=UTC),
                    Decimal(str(gppe_val)),
                    Decimal("0.9"),
                    "2023Q1",
                ),
            )

    result = run_backtest(
        connection,
        strategy_key="topt_gppe_rank",
        strategy_version="v1",
        universe_id="topt",
        symbols=symbols,
        factor_column="gppe",
        top_k=1,
        dropout_k=1,
    )

    assert result.status == "succeeded"
    assert result.strategy_key == "topt_gppe_rank"
    assert len(result.valuations_monthly) == len(dates)


def test_run_nightly_backtest_op_against_real_database(
    connection: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Dagster op executes against the physical database without errors."""
    symbols = ["AAPL", "MSFT"]
    dates = [date(2023, 1, 31), date(2023, 2, 28)]
    _insert_fixture_data(connection, symbols, dates)

    class NonClosingConnection:
        def __init__(self, conn: psycopg.Connection) -> None:
            self._conn = conn

        def __getattr__(self, name: str):
            return getattr(self._conn, name)

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    import data_engine.lanes.backtest as backtest_lane

    monkeypatch.setattr(backtest_lane, "DEFAULT_TOPT_SYMBOLS", tuple(symbols))
    proxy = NonClosingConnection(connection)
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: proxy)

    context = dg.build_op_context(run_tags={TICK_TAG: "2026-10-12T21:30:00+00:00"})
    config = NightlyBacktestConfig(
        strategy_key="demo_topk_rank",
        strategy_version="v1",
        universe_id="topt",
        factor_column="close",
        top_k=2,
        dropout_k=1,
    )

    # Execute op without monkeypatching run_backtest
    run_nightly_backtest_op(context, config)

    # Verify verdict row in database within the same transaction
    with connection.cursor() as cur:
        cur.execute(
            """
            select ok, summary from mart.nightly_verdicts
            where check_name = 'nightly_backtest'
            order by ran_at desc, recorded_at desc
            limit 1
            """
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True
        assert "succeeded:" in row[1]
