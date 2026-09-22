from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock


def test_load_prices_query_uses_trading_date_alias() -> None:
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from run_vectorbt_backtest import _load_prices

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = []

    _load_prices(mock_conn, "staging.market_prices_daily", ["AAPL"])

    mock_cur.execute.assert_called_once()
    sql_executed = mock_cur.execute.call_args[0][0]

    # Must select trading_date as date instead of date
    assert "trading_date as date" in sql_executed
    assert "select symbol, date, close" not in sql_executed
