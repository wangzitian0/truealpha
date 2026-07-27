"""Twelve Data second price origin (#344 / #171 A2c, init.md rule 15).

A market-price cell is only independently reconciled when two origins assert it, so this
module supplies the second: a Decimal-safe close at/before the cutoff from Twelve Data,
shaped as a `CorroboratingOrigin` the market-price adapter attaches to its success. The
fusion engine (#343) then reconciles two real assertions under the declared tolerance
policy — counting origins never reconciles values.

The key comes from `settings.twelve_data_api_key` (rendered into the runtime env from
Vault by infra2's `20.data_engine/secrets.ctmpl`), never `os.environ` in-line. With no key
configured, `twelve_data_origin()` returns None and every cell is honestly single-origin.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from data_engine.config import settings
from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin, MarketPriceQuote

ORIGIN = "twelve-data"
PARSER_VERSION = "twelve-data-parser:v1"
MAPPING_VERSION = "twelve-data-map:v1"
_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
# The free tier allows 8 requests per minute; one full TOPT tick is 21 listings.
_THROTTLE_SECONDS = 8
_LOOKBACK_DAYS = 10


class TwelveDataQuoteFetcher:
    """`MarketPriceFetcher` over Twelve Data's daily time series.

    Memoized per symbol for the life of one run and throttled to the free tier's rate,
    so a tick issues at most one request per listing.
    """

    def __init__(self, api_key: str, *, throttle_seconds: int = _THROTTLE_SECONDS) -> None:
        if not api_key:
            raise ValueError("Twelve Data origin requires an API key")
        self._api_key = api_key
        self._throttle_seconds = throttle_seconds
        self._cache: dict[tuple[str, date], MarketPriceQuote | None] = {}

    def __call__(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        key = (symbol, cutoff)
        if key in self._cache:
            return self._cache[key]
        quote = self._fetch(symbol, cutoff)
        self._cache[key] = quote
        if self._throttle_seconds:
            time.sleep(self._throttle_seconds)
        return quote

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        query = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": "1day",
                "start_date": str(cutoff - timedelta(days=_LOOKBACK_DAYS)),
                "end_date": str(cutoff),
                "outputsize": "12",
                "apikey": self._api_key,
            }
        )
        with urllib.request.urlopen(f"{_TIME_SERIES_URL}?{query}", timeout=20) as response:  # noqa: S310
            raw_bytes = response.read()
        body = json.loads(raw_bytes.decode())
        for row in body.get("values") or []:  # newest first; take the first close at/before the cutoff
            stamp, close = row.get("datetime", "")[:10], row.get("close")
            if not stamp or close is None or stamp > str(cutoff):
                continue
            try:
                as_of = date.fromisoformat(stamp)
                value = Decimal(str(close))
            except (InvalidOperation, ValueError):
                return None
            return MarketPriceQuote(
                raw_bytes=raw_bytes,
                close=value,
                as_of=as_of,
                knowable_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
            )
        return None


def twelve_data_origin() -> CorroboratingOrigin | None:
    """The configured second price origin, or None when no key is provisioned."""
    if not settings.twelve_data_api_key:
        return None
    return CorroboratingOrigin(
        origin=ORIGIN,
        parser_version=PARSER_VERSION,
        mapping_version=MAPPING_VERSION,
        value_key="price",
        confidence=Decimal("0.85"),
        fetch=TwelveDataQuoteFetcher(settings.twelve_data_api_key),
    )
