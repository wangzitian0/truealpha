"""Tests for PostgresBacktestDataGateway (#758, Milestone M3)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from truealpha_contracts.models import AsOfQuery, BacktestDataset, DataSource
from truealpha_contracts.ports import BacktestDataGateway
from truealpha_runtime.backtest_gateway import PostgresBacktestDataGateway


def test_postgres_backtest_data_gateway_implements_protocol() -> None:
    mock_conn = MagicMock()
    gateway = PostgresBacktestDataGateway(mock_conn)
    assert isinstance(gateway, BacktestDataGateway)


def test_postgres_backtest_data_gateway_load_empty() -> None:
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = []

    gateway = PostgresBacktestDataGateway(mock_conn)
    query = AsOfQuery(
        entity_ids=("AAPL", "MSFT"),
        as_of=datetime(2026, 6, 30, 21, 0, tzinfo=UTC),
    )
    dataset = gateway.load(query, price_start=date(2026, 1, 1), price_end=date(2026, 6, 30), adjust="splits")

    assert isinstance(dataset, BacktestDataset)
    assert dataset.query == query
    assert dataset.price_bars == ()
    assert dataset.financial_facts == ()


def test_postgres_backtest_data_gateway_load_populates_price_bars() -> None:
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    tx_time = datetime(2026, 3, 31, 20, 0, tzinfo=UTC)
    rec_time = datetime(2026, 3, 31, 20, 5, tzinfo=UTC)

    mock_cur.fetchall.return_value = [
        (
            "AAPL",
            date(2026, 3, 31),
            150.0,
            155.0,
            149.0,
            154.0,
            154.0,
            1000000,
            tx_time,
            rec_time,
            "raw:aapl:20260331",
        )
    ]

    gateway = PostgresBacktestDataGateway(mock_conn)
    query = AsOfQuery(
        entity_ids=("AAPL",),
        as_of=datetime(2026, 6, 30, 21, 0, tzinfo=UTC),
    )
    dataset = gateway.load(query, price_start=date(2026, 1, 1), price_end=date(2026, 6, 30), adjust="splits")

    assert len(dataset.price_bars) == 1
    bar = dataset.price_bars[0]
    assert bar.symbol == "AAPL"
    assert bar.trading_date == date(2026, 3, 31)
    assert bar.close == Decimal("154.0")
    assert bar.source == DataSource.TWELVE_DATA
    assert bar.knowable_at == tx_time
    assert bar.knowable_at <= query.as_of
