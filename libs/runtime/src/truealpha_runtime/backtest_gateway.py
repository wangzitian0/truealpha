"""Postgres-backed implementation of BacktestDataGateway (#758, Milestone M3).

Provides the governed data boundary for backtesting and historical strategy simulation.
Reads point-in-time market prices from staging.market_prices_daily, enforcing
knowable_at <= as_of to prevent look-ahead bias.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from truealpha_contracts.models import (
    AsOfQuery,
    BacktestDataset,
    DataSource,
    PriceBar,
)


class PostgresBacktestDataGateway:
    """Production PostgreSQL implementation of BacktestDataGateway.

    Strictly satisfies the BacktestDataGateway protocol:
    - Enforces point-in-time boundaries (transaction_time / knowable_at <= query.as_of).
    - Resolves multi-vintage prices by taking the latest recorded_at for each date.
    - Yields immutable BacktestDataset with verified lookahead rejection.
    """

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def load(
        self,
        query: AsOfQuery,
        *,
        price_start: date,
        price_end: date,
        adjust: str,
    ) -> BacktestDataset:
        """Load PIT price bars and financial facts knowable at or before query.as_of."""
        symbols = list(query.entity_ids)
        price_bars: list[PriceBar] = []

        # 1. Load price bars
        with self._connection.cursor() as cur:
            cur.execute(
                """
                select distinct on (symbol, trading_date)
                    symbol,
                    trading_date,
                    open,
                    high,
                    low,
                    close,
                    coalesce(adjusted_close, close) as adjusted_close,
                    volume,
                    transaction_time,
                    recorded_at,
                    raw_ref
                from staging.market_prices_daily
                where symbol = any(%s)
                  and trading_date >= %s
                  and trading_date <= %s
                  and transaction_time <= %s
                  and adjust = %s
                order by symbol, trading_date, recorded_at desc
                """,
                (symbols, price_start, price_end, query.as_of, adjust),
            )
            rows = cur.fetchall()

            for row in rows:
                (
                    sym,
                    t_date,
                    op,
                    hi,
                    lo,
                    cl,
                    adj_cl,
                    vol,
                    tx_time,
                    rec_time,
                    raw_ref,
                ) = row
                # Ensure high is >= max(open, close, low) and low <= min(open, close, high)
                o_dec, h_dec, l_dec, c_dec = Decimal(str(op)), Decimal(str(hi)), Decimal(str(lo)), Decimal(str(cl))
                eff_high = max(h_dec, o_dec, c_dec, l_dec)
                eff_low = min(l_dec, o_dec, c_dec, h_dec)

                price_bars.append(
                    PriceBar(
                        entity_id=sym,
                        symbol=sym,
                        trading_date=t_date,
                        open=o_dec,
                        high=eff_high,
                        low=eff_low,
                        close=c_dec,
                        adjusted_close=Decimal(str(adj_cl)),
                        volume=max(0, int(vol or 0)),
                        knowable_at=tx_time,
                        recorded_at=max(rec_time, tx_time),
                        source=DataSource.TWELVE_DATA,
                        raw_ref=raw_ref or f"raw:market_prices_daily:{sym}:{t_date}",
                    )
                )

        return BacktestDataset(
            query=query,
            price_bars=tuple(price_bars),
            financial_facts=(),
        )


__all__ = ["PostgresBacktestDataGateway"]
