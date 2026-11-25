"""Unit tests for TwelveDataClient SDK retry behavior."""

from __future__ import annotations

import json

import pytest
from data_engine.datahub.market_prices import (
    TwelveDataApiError,
    TwelveDataClient,
    TwelveDataRateLimitError,
)


def test_twelve_data_client_retries_http_200_rate_limit_via_sdk() -> None:
    sleeps: list[float] = []
    responses = [
        (200, json.dumps({"code": 429, "message": "You have run out of API credits"}).encode()),
        (200, json.dumps({"status": "ok", "values": [{"datetime": "2026-11-24", "close": "100.0"}]}).encode()),
    ]
    client = TwelveDataClient(
        api_key="test-key",
        transport_fn=lambda _url: responses.pop(0),
        sleep_fn=sleeps.append,
        max_retries=2,
        backoff_base=2.0,
    )
    payload = client.fetch_time_series("AAPL", "1day")
    assert payload["status"] == "ok"
    assert client.call_count == 2
    assert sleeps == [2.0]


def test_twelve_data_client_raises_after_exhausting_retries() -> None:
    sleeps: list[float] = []
    client = TwelveDataClient(
        api_key="test-key",
        transport_fn=lambda _url: (429, b'{"code": 429, "message": "rate limit"}'),
        sleep_fn=sleeps.append,
        max_retries=2,
        backoff_base=2.0,
    )
    with pytest.raises(TwelveDataRateLimitError, match="after 2 retries"):
        client.fetch_time_series("AAPL", "1day")
    assert client.call_count == 3
    assert sleeps == [2.0, 4.0]


def test_twelve_data_client_does_not_retry_http_400() -> None:
    sleeps: list[float] = []
    client = TwelveDataClient(
        api_key="test-key",
        transport_fn=lambda _url: (400, json.dumps({"status": "error", "code": 400, "message": "bad symbol"}).encode()),
        sleep_fn=sleeps.append,
        max_retries=3,
    )
    with pytest.raises(TwelveDataApiError, match="bad symbol"):
        client.fetch_time_series("BAD", "1day")
    assert client.call_count == 1
    assert sleeps == []
