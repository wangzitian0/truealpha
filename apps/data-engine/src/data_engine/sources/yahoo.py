"""Yahoo Finance daily price bars — the FALLBACK price source (no SLA, init.md
Section 9; Twelve Data is the primary once an API key exists).

Hits the chart endpoint directly with a plain httpx client, rather than the
`yfinance` PyPI package: the VPS's IP is rate-limited by Yahoo (HTTP 429) when
using yfinance's default session, but a plain request with a non-default
User-Agent succeeds. Same approach finance_report runs in production from the
same VPS (apps/backend/src/pricing/extension/market_data/_providers.py) —
Yahoo's blocking appears to key off session/TLS fingerprinting, not the UA
string's exact content, but a non-default UA is what's actually verified to work.

## Prices are Decimal, and the vendor's float32 is undone here

Yahoo serializes prices through float32 and sends the artifact on the wire —
inspected directly, `close` arrives as `326.5899963378906`, not `326.59`. So this
is not something Python's JSON parser introduces and not something a downstream
`Decimal(...)` cast can avoid: by the time any consumer sees it the noise is
already in the bytes. It has to be undone at the parse boundary or not at all,
which is why the recovery lives here rather than in the adapter.

Recovery is exact, not a rounding convenience. float32 spacing below 2**16 is at
most 2**-8 = 0.0039, under half a cent, so exactly one 2-decimal price maps onto
each float32 value and quantizing returns the original. Above that the spacing
exceeds half a cent (at $700k it is 0.0625) and the original is genuinely
unrecoverable — those values are left untouched rather than rounded to a figure
we cannot justify.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "TrueAlpha research wangzitian.ai@icloud.com"

# Below this magnitude, one 2-decimal price maps to exactly one float32, so quantizing
# recovers the vendor's original figure. At or above it, do not pretend to know.
EXACT_CENT_RECOVERY_BELOW = Decimal(2**16)
_CENT = Decimal("0.01")


class PriceBar:
    def __init__(
        self,
        day: date,
        open_: Decimal | None,
        high: Decimal | None,
        low: Decimal | None,
        close: Decimal,
        adj_close: Decimal | None,
        volume: int | None,
    ):
        self.date = day
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.adj_close = adj_close
        self.volume = volume


def recover_quoted_price(value: Decimal | None) -> Decimal | None:
    """Undo Yahoo's float32 serialization when the original is provably recoverable."""
    if value is None:
        return None
    if abs(value) >= EXACT_CENT_RECOVERY_BELOW:
        return value
    return value.quantize(_CENT)


def _epoch(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), tzinfo=UTC).timestamp())


def fetch_daily_bars(symbol: str, *, end: date | None = None, period_days: int = 365) -> list[PriceBar]:
    """Fetch ~period_days of daily OHLCV bars ending at `end` (chronological).

    `end` exists so a point-in-time caller gets a window around ITS cutoff. Defaulting
    the window to the wall clock and filtering afterwards silently returns nothing for
    any cutoff more than `period_days` in the past — a backfill would see every price
    cell resolve `FIELD_UNAVAILABLE` rather than an error naming the cause.
    """
    end = end or date.today()
    start = end - timedelta(days=period_days)
    params = {"period1": str(_epoch(start)), "period2": str(_epoch(end + timedelta(days=1))), "interval": "1d"}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
        resp = client.get(CHART_URL.format(symbol=symbol), params=params)
        resp.raise_for_status()
        # Decoded with parse_float=Decimal so the response is never routed through a
        # Python float — that would add a second rounding on top of the vendor's.
        payload = json.loads(resp.text, parse_float=Decimal)
    return _parse_chart_response(payload)


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
        close = recover_quoted_price(c)
        if close is None:  # non-trading gaps inside the range come back null
            continue
        bars.append(
            PriceBar(
                day=datetime.fromtimestamp(ts, tz=UTC).date(),
                open_=recover_quoted_price(o),
                high=recover_quoted_price(h),
                low=recover_quoted_price(low),
                close=close,
                adj_close=recover_quoted_price(adjclose[i]),
                volume=v,
            )
        )
    return bars
