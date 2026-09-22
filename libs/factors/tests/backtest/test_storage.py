"""Tests for persistence layer of backtest results."""

from unittest.mock import MagicMock

import pandas as pd
import pytest
from factors.backtest.engine import BacktestResult
from factors.backtest.storage import persist_backtest_result


def _make_dummy_result(
    run_id: str = "backtest-run:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
) -> BacktestResult:
    val_m = pd.DataFrame(
        {
            "valuation_date": ["2023-01-31", "2023-02-28"],
            "cum_nav": [1.0, 1.05],
            "drawdown": [0.0, 0.0],
            "gross_exposure": [1.0, 1.0],
            "cash_weight": [0.0, 0.0],
        }
    )
    val_d = pd.DataFrame(
        {
            "valuation_date": ["2023-01-31", "2023-02-01"],
            "cum_nav": [1.0, 1.01],
            "drawdown": [0.0, 0.0],
            "gross_exposure": [1.0, 1.0],
            "cash_weight": [0.0, 0.0],
        }
    )
    trades = [
        {
            "trade_date": "2023-01-31",
            "symbol": "AAPL",
            "side": "BUY",
            "shares": 100.0,
            "execution_price": 150.0,
            "trade_value": 15000.0,
            "weight_before": 0.0,
            "weight_after": 0.5,
            "fee_paid": 15.0,
        }
    ]
    return BacktestResult(
        run_id=run_id,
        strategy_key="strat_test",
        strategy_version="v1.0",
        universe_id="u_test",
        start_date="2023-01-31",
        end_date="2023-02-28",
        status="succeeded",
        cagr_monthly=0.15,
        sharpe_daily=1.5,
        max_dd_daily=0.05,
        vol_daily=0.12,
        turnover_monthly=0.5,
        calmar_daily=3.0,
        metrics_payload={"cagr_monthly": 0.15},
        valuations_monthly=val_m,
        valuations_daily=val_d,
        trades=trades,
    )


def test_persist_backtest_result_executes_queries_and_commits():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    result = _make_dummy_result()
    persist_backtest_result(mock_conn, result)

    # 1. Verify runs upsert includes executed_at = now()
    calls = mock_cur.execute.call_args_list
    assert len(calls) >= 2  # backtest_runs insert + trades delete
    run_sql = calls[0][0][0]
    assert "mart.backtest_runs" in run_sql
    assert "executed_at = now()" in run_sql

    # 2. Verify trades deletion for idempotency
    trades_del_sql = calls[1][0][0]
    trades_del_params = calls[1][0][1]
    assert "delete from mart.backtest_trades where run_id = %s" in trades_del_sql
    assert trades_del_params == (result.run_id,)

    # 3. Verify executemany called for valuations (1M, 1D) and trades
    assert mock_cur.executemany.call_count == 3

    # 4. Verify commit was called and cursor closed
    mock_conn.commit.assert_called_once()
    mock_cur.close.assert_called_once()


def test_persist_backtest_result_rollback_on_error():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.execute.side_effect = RuntimeError("DB error")

    result = _make_dummy_result()
    with pytest.raises(RuntimeError, match="DB error"):
        persist_backtest_result(mock_conn, result)

    # Verify rollback called and cursor closed
    mock_conn.rollback.assert_called_once()
    mock_cur.close.assert_called_once()


def test_persist_backtest_result_with_bare_cursor():
    mock_cur = MagicMock(spec=["execute", "executemany"])
    result = _make_dummy_result()

    persist_backtest_result(mock_cur, result)
    assert mock_cur.execute.call_count >= 2
    assert mock_cur.executemany.call_count == 3
