from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.production_topt.failover_drill import (
    DrillKind,
    DrillRefused,
    FailoverDrill,
    is_production,
    verdict,
)
from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin, MarketPriceQuote


def _quote(symbol: str, as_of: date) -> MarketPriceQuote:
    return MarketPriceQuote(
        raw_bytes=b"{}",
        close=Decimal("150.00"),
        as_of=as_of,
        knowable_at=datetime.combine(as_of, datetime.min.time()),
    )


def test_is_production() -> None:
    assert is_production("production") is True
    assert is_production("prod") is True
    assert is_production("Production") is True
    assert is_production("staging") is False
    assert is_production("dev") is False


def test_for_launch_refusals() -> None:
    # Production refused
    with pytest.raises(DrillRefused, match="never runs in production"):
        FailoverDrill.for_launch(app_env="production", force_fetch=True, tickers=["AAPL"])

    # Without force_fetch refused
    with pytest.raises(DrillRefused, match="force a fetch"):
        FailoverDrill.for_launch(app_env="staging", force_fetch=False, tickers=["AAPL"])

    # Too many tickers
    with pytest.raises(DrillRefused, match="at most 3 tickers"):
        FailoverDrill.for_launch(app_env="staging", force_fetch=True, tickers=["AAPL", "MSFT", "GOOG", "AMZN"])

    # Conflicting options
    with pytest.raises(DrillRefused, match="cannot combine primary_lagging with twelve_data_unavailable"):
        FailoverDrill.for_launch(
            app_env="staging",
            force_fetch=True,
            tickers=["AAPL"],
            twelve_data_unavailable=True,
            primary_lagging=True,
        )


def test_for_launch_kinds() -> None:
    assert FailoverDrill.for_launch(app_env="staging", force_fetch=True, tickers=[]) is None

    drill1 = FailoverDrill.for_launch(app_env="staging", force_fetch=True, tickers=["AAPL"])
    assert drill1 is not None and drill1.kind == DrillKind.PRIMARY_UNAVAILABLE
    assert drill1.tickers == ("AAPL",)

    drill2 = FailoverDrill.for_launch(
        app_env="staging", force_fetch=True, tickers=["MSFT"], twelve_data_unavailable=True
    )
    assert drill2 is not None and drill2.kind == DrillKind.PRIMARY_AND_TWELVE_DATA_UNAVAILABLE

    drill3 = FailoverDrill.for_launch(
        app_env="staging", force_fetch=True, tickers=["NVDA"], primary_lagging=True
    )
    assert drill3 is not None and drill3.kind == DrillKind.PRIMARY_LAGGING


def test_arm_primary_lagging() -> None:
    drill = FailoverDrill(kind=DrillKind.PRIMARY_LAGGING, tickers=("AAPL",))
    cutoff = date(2026, 9, 15)  # Tuesday

    def base_fetcher(symbol: str, d: date) -> MarketPriceQuote | None:
        return _quote(symbol, d)

    origin = CorroboratingOrigin(
        origin="twelve-data",
        parser_version="twelve-data-parser:v3",
        mapping_version="twelve-data-mapping:v1",
        value_key="close",
        confidence=Decimal("0.8"),
        fetch=base_fetcher,
    )

    armed_fetcher, armed_origins = drill.arm("staging", base_fetcher, (origin,))
    assert len(armed_origins) == 1

    # AAPL is drilled -> returns quote with as_of = prior session
    aapl_quote = armed_fetcher("AAPL", cutoff)
    assert aapl_quote is not None
    assert aapl_quote.as_of == date(2026, 9, 14)  # Prior weekday

    # MSFT is not drilled -> returns normal cutoff
    msft_quote = armed_fetcher("MSFT", cutoff)
    assert msft_quote is not None
    assert msft_quote.as_of == cutoff


def test_verdict_evaluation() -> None:
    stamp = {
        "kind": DrillKind.PRIMARY_LAGGING.value,
        "tickers": ["AAPL"],
        "listing_ids": ["listing:xnys:aapl"],
        "origins_down": [],
        "cells": 1,
    }
    # Successful failover
    cells_success = {"listing:xnys:aapl": {"served_by_failover": "twelve-data"}}
    v_pass = verdict(stamp, cells_success, served_by_failover=1)
    assert v_pass["passed"] is True

    # Failed failover (not served)
    cells_fail = {"listing:xnys:aapl": {"served_by_failover": None}}
    v_fail = verdict(stamp, cells_fail, served_by_failover=0)
    assert v_fail["passed"] is False
