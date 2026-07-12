"""Yahoo Finance daily price bars — the FALLBACK price source (no SLA, init.md
Section 9; Twelve Data is the primary once an API key exists).

Hits the chart endpoint directly with a plain httpx client, rather than the
`yfinance` PyPI package: the VPS's IP is rate-limited by Yahoo (HTTP 429) when
using yfinance's default session, but a plain request with a non-default
User-Agent succeeds. Same approach finance_report runs in production from the
same VPS (apps/backend/src/pricing/extension/market_data/_providers.py) —
Yahoo's blocking appears to key off session/TLS fingerprinting, not the UA
string's exact content, but a non-default UA is what's actually verified to work.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "TrueAlpha research wangzitian.ai@icloud.com"


class PriceBar:
    def __init__(
        self,
        day: date,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        adj_close: Decimal,
        volume: int,
    ):
        self.date = day
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.adj_close = adj_close
        self.volume = volume


def _epoch(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), tzinfo=UTC).timestamp())


def fetch_chart_response(symbol: str, *, period_days: int = 365) -> httpx.Response:
    """Fetch raw chart bytes including dividends and splits for one bounded range."""
    end = date.today()
    start = end - timedelta(days=period_days)
    params = {
        "period1": str(_epoch(start)),
        "period2": str(_epoch(end + timedelta(days=1))),
        "interval": "1d",
        "events": "div,splits",
    }
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
        resp = client.get(CHART_URL.format(symbol=symbol), params=params)
        resp.raise_for_status()
    return resp


def fetch_daily_bars(symbol: str, *, period_days: int = 365) -> list[PriceBar]:
    """Fetch ~period_days of daily OHLCV bars in chronological order."""
    return _parse_chart_response(fetch_chart_response(symbol, period_days=period_days).json())


def _parse_chart_response(payload: dict[str, Any]) -> list[PriceBar]:
    result = payload["chart"]["result"]
    if not result:
        return []
    r = result[0]
    timestamps = r.get("timestamp") or []
    quote = r["indicators"]["quote"][0]
    adjclose = r["indicators"].get("adjclose", [{}])[0].get("adjclose", quote["close"])

    bars = []
    for i, ts in enumerate(timestamps):
        o, h, low, c, v = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i], quote["volume"][i]
        if c is None:  # non-trading gaps inside the range come back null
            continue
        bars.append(
            PriceBar(
                day=datetime.fromtimestamp(ts, tz=UTC).date(),
                open_=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(c)),
                adj_close=Decimal(str(adjclose[i])),
                volume=v,
            )
        )
    return bars
