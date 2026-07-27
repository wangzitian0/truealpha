"""The second origin throttles even when it fails (#344 / #171).

A rate-limited request that skipped its wait would fire the next symbol immediately and
rate-limit the rest of the tick with it, so one refused request becomes many. The throttle
belongs to every outcome, not just the successful one.
"""

from __future__ import annotations

from datetime import date

import pytest
from data_engine.datahub.production_topt.market_price_adapter import MarketPriceQuote
from data_engine.datahub.production_topt.twelve_data_origin import TwelveDataQuoteFetcher

_CUTOFF = date(2026, 3, 31)


class _RecordingFetcher(TwelveDataQuoteFetcher):
    """The real fetcher with its HTTP call and its sleep replaced by records."""

    def __init__(self, outcomes: dict[str, object]) -> None:
        super().__init__("test-key", throttle_seconds=1)
        self._outcomes = outcomes
        self.slept: list[int] = []
        self.requested: list[str] = []

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        self.requested.append(symbol)
        outcome = self._outcomes[symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("data_engine.datahub.production_topt.twelve_data_origin.time.sleep", slept.append)
    return slept


def test_a_refused_request_still_throttles_and_is_absent(_no_real_sleep) -> None:
    fetcher = _RecordingFetcher({"AAPL": RuntimeError("429 rate limited")})
    assert fetcher("AAPL", _CUTOFF) is None
    assert _no_real_sleep == [1], "a failed second-origin request must still wait its turn"


def test_api_key_is_required() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        TwelveDataQuoteFetcher("")


def test_one_request_per_symbol_per_run(_no_real_sleep) -> None:
    """Repeat lookups come from the cache — including a cached failure."""
    fetcher = _RecordingFetcher({"AAPL": RuntimeError("boom")})
    assert fetcher("AAPL", _CUTOFF) is None
    assert fetcher("AAPL", _CUTOFF) is None
    assert fetcher.requested == ["AAPL"], "a cached outcome must not re-hit the provider"
    assert _no_real_sleep == [1], "the cached call must not throttle again"
