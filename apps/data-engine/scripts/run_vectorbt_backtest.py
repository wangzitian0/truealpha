#!/usr/bin/env python3
"""CLI operator script: run a single-track portfolio backtest from staging
market data and the PIT universe mask, and persist the result to `mart.backtest_*`
(#101 datahub staging tables, #102 factor expression compiler + adapter, #103, #938
backtest engine + mart persistence).

Chains every layer this stack added, end to end, against real database rows:

    staging.market_prices_{daily,monthly} + staging.universe_mask   (ingested by
        the `data_engine.lanes.market_data` schedule)
      -> factors.expressions.dsl / .compiler   (factor expression -> polars.Expr)
      -> factors.backtest.adapter              (Top-K Dropout target weights,
                                                 long-format panels pivoted to the
                                                 wide matrices)
      -> factors.backtest.engine               (single-track daily simulation)
      -> factors.backtest.storage              (mart.backtest_runs / _valuations /
                                                 _trades)

Usage: uv run --package truealpha-data-engine python \\
    apps/data-engine/scripts/run_vectorbt_backtest.py \\
    --strategy-key demo_topk_rank --strategy-version v1 --universe-id topt20 \\
    --symbols AAPL MSFT GOOGL AMZN NVDA \\
    [--factor-column close] [--top-k 5] [--dropout-k 2]
"""

from __future__ import annotations

import argparse

import pandas as pd
import polars as pl
import psycopg
from data_engine.config import settings
from factors.backtest.adapter import compile_factor_panel, compute_topk_dropout_weights, pivot_to_vbt_matrices
from factors.backtest.engine import BacktestEngineConfig, BacktestResult, VectorBTBacktestEngine
from factors.backtest.storage import persist_backtest_result
from factors.expressions.dsl import col


def _load_prices(connection: psycopg.Connection, table: str, symbols: list[str]) -> pl.DataFrame:
    query = f"select symbol, date, close from {table} where symbol = any(%s) order by date asc"  # noqa: S608 - table is one of two fixed literals below
    with connection.cursor() as cur:
        cur.execute(query, (symbols,))
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    return pl.DataFrame(rows, schema=["symbol", "date", "close"], orient="row")


def _load_mask(connection: psycopg.Connection, symbols: list[str]) -> pl.DataFrame:
    query = """
        select symbol, cutoff_date, eligible
        from staging.universe_mask
        where symbol = any(%s)
        order by cutoff_date asc
    """
    with connection.cursor() as cur:
        cur.execute(query, (symbols,))
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "cutoff_date": pl.Date, "eligible": pl.Boolean})
    return pl.DataFrame(rows, schema=["symbol", "cutoff_date", "eligible"], orient="row")


def run_backtest(
    connection: psycopg.Connection,
    strategy_key: str,
    strategy_version: str,
    universe_id: str,
    symbols: list[str],
    factor_column: str,
    top_k: int,
    dropout_k: int,
) -> BacktestResult:
    """Load staging market data, compile the factor, rebalance, simulate, persist."""
    monthly_prices = _load_prices(connection, "staging.market_prices_monthly", symbols)
    if monthly_prices.is_empty():
        raise SystemExit(
            f"no rows in staging.market_prices_monthly for {symbols}; run the "
            "market_data_refresh_pipeline lane (or ingest_twelve_data_market_prices) first"
        )
    daily_prices = _load_prices(connection, "staging.market_prices_daily", symbols)
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

    engine = VectorBTBacktestEngine(BacktestEngineConfig(top_k=top_k, dropout_k=dropout_k))
    result = engine.run(
        strategy_key=strategy_key,
        strategy_version=strategy_version,
        universe_id=universe_id,
        close_monthly=close_monthly,
        weights_monthly=weights_monthly,
        close_daily=close_daily,
        top_k=top_k,
        dropout_k=dropout_k,
    )
    persist_backtest_result(connection, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy-key", required=True)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--factor-column", default="close")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--dropout-k", type=int, default=2)
    args = parser.parse_args()

    with psycopg.connect(settings.database_url) as connection:
        result = run_backtest(
            connection,
            strategy_key=args.strategy_key,
            strategy_version=args.strategy_version,
            universe_id=args.universe_id,
            symbols=args.symbols,
            factor_column=args.factor_column,
            top_k=args.top_k,
            dropout_k=args.dropout_k,
        )
        connection.commit()

    print(
        f"OK: {result.run_id} ({result.status}) cagr_monthly={result.cagr_monthly:.4f} "
        f"sharpe_daily={result.sharpe_daily:.4f} max_dd_daily={result.max_dd_daily:.4f}"
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
