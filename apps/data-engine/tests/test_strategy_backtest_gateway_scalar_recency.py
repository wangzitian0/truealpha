from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from data_engine.strategy_backtest_gateway import StrategyBacktestGateway


def test_strategy_backtest_gateway_scalar_recency_latest_period() -> None:
    mock_conn = MagicMock()
    gateway = StrategyBacktestGateway(mock_conn)

    # Return rows for same issuer and same input_key across FY2021, FY2022, FY2023 in ascending order
    rows = [
        ("issuer:aapl", "net_income", 100, 0.9, "FY2021"),
        ("issuer:aapl", "net_income", 200, 0.9, "FY2022"),
        ("issuer:aapl", "net_income", 300, 0.9, "FY2023"),
    ]
    gateway._rows_for_cutoff = MagicMock(return_value=rows)

    inputs = gateway.issuer_inputs("2023-12-31")
    assert len(inputs) == 1
    records = inputs[0].records

    # Scalars must reflect the latest period (FY2023 -> 300), not locked to FY2021 (100)
    assert records["net_income"][0] == Decimal("300")


def test_strategy_backtest_gateway_scalar_recency_with_restatement_tag() -> None:
    mock_conn = MagicMock()
    gateway = StrategyBacktestGateway(mock_conn)

    rows = [
        ("issuer:aapl", "net_income", 400, 0.9, "FY2024:FY:2024-01-01:2024-12-31"),
        ("issuer:aapl", "net_income", 250, 0.9, "FY2026:FY:2022-01-01:2022-12-31"),
    ]
    gateway._rows_for_cutoff = MagicMock(return_value=rows)

    inputs = gateway.issuer_inputs("2024-12-31")
    assert len(inputs) == 1
    records = inputs[0].records

    assert records["net_income"][0] == Decimal("400")
