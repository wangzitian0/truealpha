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

import psycopg
from data_engine.config import settings
from data_engine.datahub.backtest import _load_mask, _load_prices, run_backtest

__all__ = ["_load_mask", "_load_prices", "run_backtest"]


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
