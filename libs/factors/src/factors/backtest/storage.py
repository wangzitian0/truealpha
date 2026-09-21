"""Persistence layer for backtest results into PostgreSQL mart tables."""

from __future__ import annotations

import json
from typing import Any

from factors.backtest.engine import BacktestResult


def persist_backtest_result(conn_or_cursor: Any, result: BacktestResult) -> None:
    """Write BacktestResult into mart.backtest_runs, mart.backtest_valuations, and mart.backtest_trades."""
    is_conn = hasattr(conn_or_cursor, "cursor")
    cur = conn_or_cursor.cursor() if is_conn else conn_or_cursor

    try:
        # 1. Insert mart.backtest_runs
        cur.execute(
            """
            insert into mart.backtest_runs (
                run_id, strategy_key, strategy_version, universe_id,
                start_date, end_date, status,
                cagr_monthly, sharpe_daily, max_dd_daily, vol_daily,
                turnover_monthly, calmar_daily, metrics_payload,
                error_message, executed_at
            ) values (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, now()
            )
            on conflict (run_id) do update set
                status = excluded.status,
                cagr_monthly = excluded.cagr_monthly,
                sharpe_daily = excluded.sharpe_daily,
                max_dd_daily = excluded.max_dd_daily,
                vol_daily = excluded.vol_daily,
                turnover_monthly = excluded.turnover_monthly,
                calmar_daily = excluded.calmar_daily,
                metrics_payload = excluded.metrics_payload,
                error_message = excluded.error_message;
            """,
            (
                result.run_id,
                result.strategy_key,
                result.strategy_version,
                result.universe_id,
                result.start_date,
                result.end_date,
                result.status,
                result.cagr_monthly,
                result.sharpe_daily,
                result.max_dd_daily,
                result.vol_daily,
                result.turnover_monthly,
                result.calmar_daily,
                json.dumps(result.metrics_payload),
                result.error_message,
            ),
        )

        # 2. Insert mart.backtest_valuations (Monthly)
        val_m_records = [
            (
                result.run_id,
                "1M",
                str(row["valuation_date"]),
                float(row["cum_nav"]),
                float(row["drawdown"]),
                float(row["gross_exposure"]),
                float(row["cash_weight"]),
            )
            for _, row in result.valuations_monthly.iterrows()
        ]
        if val_m_records:
            cur.executemany(
                """
                insert into mart.backtest_valuations (
                    run_id, resolution, valuation_date, cum_nav, drawdown, gross_exposure, cash_weight
                ) values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id, resolution, valuation_date) do update set
                    cum_nav = excluded.cum_nav,
                    drawdown = excluded.drawdown,
                    gross_exposure = excluded.gross_exposure,
                    cash_weight = excluded.cash_weight;
                """,
                val_m_records,
            )

        # 3. Insert mart.backtest_valuations (Daily)
        val_d_records = [
            (
                result.run_id,
                "1D",
                str(row["valuation_date"]),
                float(row["cum_nav"]),
                float(row["drawdown"]),
                float(row["gross_exposure"]),
                float(row["cash_weight"]),
            )
            for _, row in result.valuations_daily.iterrows()
        ]
        if val_d_records:
            cur.executemany(
                """
                insert into mart.backtest_valuations (
                    run_id, resolution, valuation_date, cum_nav, drawdown, gross_exposure, cash_weight
                ) values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id, resolution, valuation_date) do update set
                    cum_nav = excluded.cum_nav,
                    drawdown = excluded.drawdown,
                    gross_exposure = excluded.gross_exposure,
                    cash_weight = excluded.cash_weight;
                """,
                val_d_records,
            )

        # 4. Insert mart.backtest_trades
        trade_records = [
            (
                result.run_id,
                t["trade_date"],
                t["symbol"],
                t["side"],
                t["shares"],
                t["execution_price"],
                t["trade_value"],
                t["weight_before"],
                t["weight_after"],
                t.get("fee_paid", 0.0),
            )
            for t in result.trades
        ]
        if trade_records:
            cur.executemany(
                """
                insert into mart.backtest_trades (
                    run_id, trade_date, symbol, side, shares, execution_price,
                    trade_value, weight_before, weight_after, fee_paid
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                trade_records,
            )

        if is_conn:
            conn_or_cursor.commit()
    finally:
        if is_conn:
            cur.close()
