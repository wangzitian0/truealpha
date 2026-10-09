"""Datahub backtest executor and market data loaders.

This module loads market prices and universe masks from staging tables.
It compiles factor panels and computes target weights.
It simulates portfolio performance with the backtest engine and writes results to mart tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import polars as pl
import psycopg
from factors.backtest.adapter import compile_factor_panel, compute_topk_dropout_weights, pivot_to_vbt_matrices
from factors.backtest.engine import BacktestEngineConfig, BacktestResult, VectorBTBacktestEngine
from factors.backtest.storage import persist_backtest_result
from factors.expressions.dsl import col

from data_engine.datahub.market_prices import SPLIT_ADJUSTED

if TYPE_CHECKING:
    from collections.abc import Sequence


def _load_prices(connection: psycopg.Connection, table: str, symbols: Sequence[str], *, adjust: str) -> pl.DataFrame:
    """Load price rows from a staging market price table for given symbols and adjustment mode.

    The query resolves multiple vintages to the latest vintage by sorting on recorded_at descending.
    """
    query = f"""
        select distinct on (symbol, trading_date) symbol, trading_date as date, close
        from {table}
        where symbol = any(%s) and adjust = %s
        order by symbol, trading_date, recorded_at desc
    """  # noqa: S608 - table is one of two fixed literals
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), adjust))
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    return pl.DataFrame(rows, schema=["symbol", "date", "close"], orient="row")


def _load_mask(connection: psycopg.Connection, symbols: Sequence[str]) -> pl.DataFrame:
    """Load PIT universe eligibility masks for given symbols."""
    query = """
        select symbol, cutoff_date, eligible
        from staging.universe_mask
        where symbol = any(%s)
        order by cutoff_date asc
    """
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols),))
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "cutoff_date": pl.Date, "eligible": pl.Boolean})
    return pl.DataFrame(rows, schema=["symbol", "cutoff_date", "eligible"], orient="row")


def run_backtest(
    connection: psycopg.Connection,
    strategy_key: str,
    strategy_version: str,
    universe_id: str,
    symbols: Sequence[str],
    factor_column: str,
    top_k: int,
    dropout_k: int,
    data_snapshot_hash: str | None = None,
) -> BacktestResult:
    """Load staging market data, compile factor panel, simulate portfolio, and persist mart rows."""
    monthly_prices = _load_prices(connection, "staging.market_prices_monthly", symbols, adjust=SPLIT_ADJUSTED)
    if monthly_prices.is_empty():
        raise ValueError(
            f"no rows in staging.market_prices_monthly for {list(symbols)}; run market_data_refresh_pipeline first"
        )
    daily_prices = _load_prices(connection, "staging.market_prices_daily", symbols, adjust=SPLIT_ADJUSTED)
    mask = _load_mask(connection, symbols)

    factor_expr = col(factor_column).rank()
    panel = compile_factor_panel(factor_expr, monthly_prices, mask_df=mask if not mask.is_empty() else None)
    weights = compute_topk_dropout_weights(
        panel, top_k=top_k, dropout_k=dropout_k, mask_df=mask if not mask.is_empty() else None
    )
    close_monthly, weights_monthly = pivot_to_vbt_matrices(weights, monthly_prices)

    close_daily: pd.DataFrame | None = None
    if not daily_prices.is_empty():
        close_daily, _ = pivot_to_vbt_matrices(weights, daily_prices)

    engine = VectorBTBacktestEngine(
        BacktestEngineConfig(
            top_k=top_k,
            dropout_k=dropout_k,
            data_snapshot_hash=data_snapshot_hash or "default_snapshot",
        )
    )
    result = engine.run(
        strategy_key=strategy_key,
        strategy_version=strategy_version,
        universe_id=universe_id,
        close_monthly=close_monthly,
        weights_monthly=weights_monthly,
        close_daily=close_daily,
        top_k=top_k,
        dropout_k=dropout_k,
        data_snapshot_hash=data_snapshot_hash,
    )
    persist_backtest_result(connection, result)
    return result
