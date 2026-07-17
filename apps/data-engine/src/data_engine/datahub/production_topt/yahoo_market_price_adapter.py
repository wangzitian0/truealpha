"""Yahoo market-price source adapter (Phase 3c, ADR A1 / #171).

Implements `SourceFetchPort` for the `market-price` semantic type. It resolves each work item
to its listing symbol and point-in-time cutoff, fetches the daily close through an injected
fetcher (the real Yahoo client by default; a fake in tests — no live HTTP in the suite),
hashes the immutable raw bytes, and returns a Decimal-safe `FetchSuccess` or a classified
`FetchFailure`. Prices are parsed as Decimal (never binary float) before persistence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem

from data_engine.datahub.production_topt.executor import FetchFailure, FetchOutcome, FetchSuccess


@dataclass(frozen=True)
class MarketPriceTarget:
    """The resolved fetch parameters for one market-price work item."""

    symbol: str
    cutoff: date


@dataclass(frozen=True)
class MarketPriceQuote:
    """One Decimal-safe daily close plus the immutable raw bytes it was parsed from."""

    raw_bytes: bytes
    close: Decimal
    as_of: date
    knowable_at: datetime


# (symbol, cutoff) -> the quote at//before cutoff, or None when the source has no datum.
MarketPriceFetcher = Callable[[str, date], MarketPriceQuote | None]


class SourceUnavailableError(Exception):
    """Raised by a fetcher for a transient failure the executor should retry."""


class YahooMarketPriceAdapter:
    """`SourceFetchPort` for market-price, backed by an injected quote fetcher."""

    def __init__(self, targets: dict[str, MarketPriceTarget], fetcher: MarketPriceFetcher) -> None:
        self._targets = targets
        self._fetcher = fetcher

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        target = self._targets.get(work_item.work_item_id)
        if target is None:
            # The plan bound a work item this adapter was not configured for.
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        try:
            quote = self._fetcher(target.symbol, target.cutoff)
        except SourceUnavailableError:
            return FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)
        except TimeoutError:
            return FetchFailure(ObligationReasonCode.TIMEOUT)
        if quote is None:
            return FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)
        if quote.knowable_at.date() > target.cutoff:
            # A datum knowable only after the cutoff would be look-ahead; the run must stop.
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)
        raw_sha256 = hashlib.sha256(quote.raw_bytes).hexdigest()
        normalized_sha256 = canonical_sha256(
            {
                "semantic_type": "market-price",
                "symbol": target.symbol,
                "as_of": quote.as_of.isoformat(),
                "close": str(quote.close),
            }
        )
        return FetchSuccess(
            raw_sha256=raw_sha256,
            object_uri=f"s3://truealpha-raw/yahoo/{target.symbol}/{quote.as_of.isoformat()}",
            normalized_sha256=normalized_sha256,
            confidence=Decimal("0.9"),
            valid_from=quote.as_of,
            transaction_time=quote.knowable_at,
        )


def yahoo_quote_fetcher(symbol: str, cutoff: date) -> MarketPriceQuote | None:
    """Default fetcher: the real Yahoo daily-bar client, parsed Decimal-safe up to `cutoff`.

    Imported lazily so the adapter and its tests carry no network dependency.
    """
    import httpx

    from data_engine.sources import yahoo

    try:
        bars = yahoo.fetch_daily_bars(symbol)
    except httpx.HTTPError as error:  # transient network/timeout classified by the adapter
        raise SourceUnavailableError(str(error)) from error
    eligible = [bar for bar in bars if bar.date <= cutoff]
    if not eligible:
        return None
    bar = max(eligible, key=lambda item: item.date)
    knowable_at = datetime.combine(bar.date, datetime.min.time(), tzinfo=UTC)
    return MarketPriceQuote(
        raw_bytes=f"{symbol}:{bar.date.isoformat()}:{bar.close}".encode(),
        close=Decimal(str(bar.close)),
        as_of=bar.date,
        knowable_at=knowable_at,
    )
