from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from data_engine.strategy_backtest_gateway import StrategyBacktestGateway


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def test_strategy_backtest_gateway_scalar_recency_latest_period() -> None:
    mock_conn = MagicMock()
    gateway = StrategyBacktestGateway(mock_conn)

    # Return rows for same issuer and same input_key across FY2021, FY2022, FY2023 in ascending order
    rows = [
        ("issuer:aapl", "net_income", 100, 0.9, "FY2021", _at(2022, 2, 1)),
        ("issuer:aapl", "net_income", 200, 0.9, "FY2022", _at(2023, 2, 1)),
        ("issuer:aapl", "net_income", 300, 0.9, "FY2023", _at(2024, 2, 1)),
    ]
    gateway.rows_at = MagicMock(return_value=rows)

    inputs = gateway.issuer_inputs("2023-12-31")
    assert len(inputs) == 1
    records = inputs[0].records

    # Scalars must reflect the latest period (FY2023 -> 300), not locked to FY2021 (100)
    assert records["net_income"][0] == Decimal("300")
    # The scalar and its instant come from the same row (#1255).
    assert inputs[0].knowable_at["net_income"] == _at(2024, 2, 1)


def test_strategy_backtest_gateway_scalar_recency_with_restatement_tag() -> None:
    mock_conn = MagicMock()
    gateway = StrategyBacktestGateway(mock_conn)

    rows = [
        ("issuer:aapl", "net_income", 400, 0.9, "FY2024:FY:2024-01-01:2024-12-31", _at(2025, 2, 1)),
        ("issuer:aapl", "net_income", 250, 0.9, "FY2026:FY:2022-01-01:2022-12-31", _at(2026, 2, 1)),
    ]
    gateway.rows_at = MagicMock(return_value=rows)

    inputs = gateway.issuer_inputs("2024-12-31")
    assert len(inputs) == 1
    records = inputs[0].records

    assert records["net_income"][0] == Decimal("400")
    assert inputs[0].knowable_at["net_income"] == _at(2025, 2, 1)


def test_strategy_backtest_gateway_scalar_recency_with_mixed_and_malformed_tags() -> None:
    mock_conn = MagicMock()
    gateway = StrategyBacktestGateway(mock_conn)

    rows = [
        ("issuer:aapl", "net_income", 500, 0.9, "FY2024:FY:2024-01-01:2024-12-31", _at(2025, 2, 1)),
        ("issuer:aapl", "net_income", 300, 0.9, "FY2023", _at(2024, 2, 1)),
        ("issuer:aapl", "net_income", 100, 0.9, "MALFORMED_TAG_9999", _at(2023, 2, 1)),
        ("issuer:aapl", "net_income", 200, 0.9, "FY2025:FY:invalid-date:not-a-date", _at(2022, 2, 1)),
    ]
    gateway.rows_at = MagicMock(return_value=rows)

    inputs = gateway.issuer_inputs("2024-12-31")
    assert len(inputs) == 1
    records = inputs[0].records

    assert records["net_income"][0] == Decimal("500")
    assert inputs[0].knowable_at["net_income"] == _at(2025, 2, 1)
