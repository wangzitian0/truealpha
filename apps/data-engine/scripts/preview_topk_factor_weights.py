#!/usr/bin/env python3
"""CLI operator script: compile a factor expression over staging market data,
compute Top-K Dropout target weights, and print a preview (#102).

Chains `factors.expressions` (AST -> polars.Expr) and `factors.backtest.adapter`
(factor panel -> Top-K Dropout weights -> VectorBT-shaped matrices) against real
`staging.market_prices_monthly` / `staging.universe_mask` rows
(`data_engine.datahub.market_prices` / `.universe_mask`) — the deployed path
that makes this layer's modules reachable, not just imported.

No simulation or persistence yet: `factors.backtest.engine` (dual-resolution
VectorBT simulation) and `.storage` (mart persistence) land with the next
layer and get their own operator script
(`apps/data-engine/scripts/run_vectorbt_backtest.py`) that extends this same
pipeline through to `mart.backtest_runs`.

Usage: uv run --package truealpha-data-engine python \\
    apps/data-engine/scripts/preview_topk_factor_weights.py \\
    --symbols AAPL MSFT GOOGL AMZN NVDA \\
    [--factor-column close] [--top-k 5] [--dropout-k 2]
"""

from __future__ import annotations

import argparse

import polars as pl
import psycopg
from data_engine.config import settings
from factors.backtest.adapter import compile_factor_panel, compute_topk_dropout_weights, pivot_to_vbt_matrices
from factors.expressions.dsl import col


def _load_prices(connection: psycopg.Connection, symbols: list[str]) -> pl.DataFrame:
    query = """
        select symbol, date, close
        from staging.market_prices_monthly
        where symbol = any(%s)
        order by date asc
    """
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


def preview_topk_weights(
    connection: psycopg.Connection,
    symbols: list[str],
    factor_column: str,
    top_k: int,
    dropout_k: int,
) -> pl.DataFrame:
    """Compile the factor, compute Top-K Dropout weights, and return the weights panel."""
    prices = _load_prices(connection, symbols)
    if prices.is_empty():
        raise SystemExit(
            f"no rows in staging.market_prices_monthly for {symbols}; run the "
            "market_data_refresh_pipeline lane (or ingest_twelve_data_market_prices) first"
        )
    mask = _load_mask(connection, symbols)

    factor_expr = col(factor_column).rank()
    panel = compile_factor_panel(factor_expr, prices, mask_df=mask if not mask.is_empty() else None)
    weights = compute_topk_dropout_weights(
        panel, top_k=top_k, dropout_k=dropout_k, mask_df=mask if not mask.is_empty() else None
    )
    # Exercised for shape validation the same way the eventual VectorBT engine
    # consumes it (#103's script does this for real); a mismatch here means the
    # panel is not simulation-ready before a run is ever attempted.
    close_matrix, weights_matrix = pivot_to_vbt_matrices(weights, prices)
    assert (close_matrix.columns == weights_matrix.columns).all()
    return weights


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--factor-column", default="close")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--dropout-k", type=int, default=2)
    args = parser.parse_args()

    with psycopg.connect(settings.database_url) as connection:
        weights = preview_topk_weights(
            connection,
            symbols=args.symbols,
            factor_column=args.factor_column,
            top_k=args.top_k,
            dropout_k=args.dropout_k,
        )

    latest_cutoff = weights["cutoff"].max()
    held = weights.filter((pl.col("cutoff") == latest_cutoff) & (pl.col("target_weight") > 0))
    print(f"OK: {weights['cutoff'].n_unique()} cutoffs; latest {latest_cutoff} holds {held.height} symbol(s):")
    for row in held.sort("symbol").iter_rows(named=True):
        print(f"  {row['symbol']}: weight={row['target_weight']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
