"""Market-price source adapter (Phase 3c, ADR A1 / #171).

Implements `SourceFetchPort` for the `market-price` semantic type. It resolves each work
item to its listing symbol and point-in-time cutoff, fetches the daily close through an
injected fetcher (the real Yahoo client by default; a fake in tests — no live HTTP in the
suite), hashes the immutable raw bytes, and returns a Decimal-safe `FetchSuccess` or a
classified `FetchFailure`. Prices are parsed as Decimal (never binary float) before
persistence.

A cell reaches two independent origins here, not in generic capture code (init.md rules 15
and 22): the adapter also queries each configured `CorroboratingOrigin` and attaches its
assertion to the success. A second origin is best-effort — if it does not answer, the cell
is honestly single-origin and the fusion engine reports `insufficient_independent_origins`
rather than the run failing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from truealpha_contracts import ObligationReasonCode, canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource

from data_engine.datahub.production_topt.executor import (
    Corroboration,
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
)
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION


@dataclass(frozen=True)
class MarketPriceTarget:
    """The resolved fetch parameters and identity coordinates for one work item."""

    symbol: str
    cutoff: date
    issuer_id: str
    instrument_id: str
    listing_id: str
    currency: str = "USD"


@dataclass(frozen=True)
class MarketPriceQuote:
    """One Decimal-safe daily close plus the immutable raw bytes it was parsed from."""

    raw_bytes: bytes
    close: Decimal
    as_of: date
    knowable_at: datetime


# (symbol, cutoff) -> the quote at/before cutoff, or None when the source has no datum.
MarketPriceFetcher = Callable[[str, date], MarketPriceQuote | None]


@dataclass(frozen=True)
class CorroboratingOrigin:
    """An independent second price origin, with the parser identity it asserts under."""

    origin: str
    parser_version: str
    mapping_version: str
    value_key: str
    confidence: Decimal
    fetch: MarketPriceFetcher
    # Which vendor the corroborating bytes came from, so they land under that vendor's
    # content-addressed prefix rather than the primary's.
    raw_source: DataSource = DataSource.TWELVE_DATA


class SourceUnavailableError(Exception):
    """Raised by a fetcher for a transient failure the executor should retry."""


class MarketPriceAdapter:
    """`SourceFetchPort` for market-price, backed by an injected quote fetcher."""

    def __init__(
        self,
        targets: dict[str, MarketPriceTarget],
        fetcher: MarketPriceFetcher,
        *,
        corroborating_origins: Sequence[CorroboratingOrigin] = (),
    ) -> None:
        self._targets = targets
        self._fetcher = fetcher
        self._corroborating_origins = tuple(corroborating_origins)

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
        payload = {
            "issuer_id": target.issuer_id,
            "instrument_id": target.instrument_id,
            "listing_id": target.listing_id,
            "currency": target.currency,
            "close": str(quote.close),
        }
        return FetchSuccess(
            raw=RawResponse(
                body=quote.raw_bytes,
                source=DataSource.YAHOO,
                record_id=f"chart:{target.symbol}:{quote.as_of.isoformat()}",
            ),
            normalized_sha256=canonical_sha256(payload),
            # A single public feed with no SLA (init.md's yfinance note) — the limitation
            # is represented as lower confidence, never as a provenance branch downstream.
            confidence=Decimal("0.85"),
            valid_from=quote.as_of,
            transaction_time=quote.knowable_at,
            record=NormalizedRecord(payload=payload, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION),
            corroborations=self._corroborate(target),
        )

    def _corroborate(self, target: MarketPriceTarget) -> tuple[Corroboration, ...]:
        found: list[Corroboration] = []
        for origin in self._corroborating_origins:
            try:
                quote = origin.fetch(target.symbol, target.cutoff)
            except Exception:  # noqa: BLE001 - a second origin never fails the primary capture
                continue
            if quote is None or quote.knowable_at.date() > target.cutoff:
                continue
            payload = {
                "issuer_id": target.issuer_id,
                "instrument_id": target.instrument_id,
                "listing_id": target.listing_id,
                origin.value_key: str(quote.close),
                "currency": target.currency,
                "origin": origin.origin,
            }
            found.append(
                Corroboration(
                    origin=origin.origin,
                    transaction_time=quote.knowable_at,
                    record=NormalizedRecord(
                        payload=payload,
                        parser_version=origin.parser_version,
                        mapping_version=origin.mapping_version,
                    ),
                    confidence=origin.confidence,
                    raw=RawResponse(
                        body=quote.raw_bytes,
                        source=origin.raw_source,
                        record_id=f"{origin.origin}:{target.symbol}:{quote.as_of.isoformat()}",
                    ),
                    normalized_sha256=canonical_sha256(payload),
                )
            )
        return tuple(found)


def last_settled_session_date(cutoff: datetime) -> date:
    """The newest US-session date whose CLOSE exists at `cutoff` — the only date a
    daily close is honestly fetchable for.

    Yahoo's chart endpoint includes the CURRENT session's in-progress bar, so a
    mid-session capture that filters bars by calendar date asserts a not-yet-final
    price as `close` (#637: the 2026-08-18 07:51 ET staging smoke captured all 21
    cells this way, and every cell honestly degraded to single-origin because the
    second origin serves only settled closes). A day's close is knowable from
    16:00 America/New_York; before that, the newest settled session is the prior
    calendar day (weekends/holidays resolve naturally — no bar exists for them, so
    the fetcher's `<=` pick falls back to the last trading day). Derived from the
    run's CUTOFF, never the wall clock, so a replayed tick reproduces its window.
    """
    at_market = cutoff.astimezone(ZoneInfo("America/New_York"))
    if at_market.time() >= time(16, 0):
        return at_market.date()
    return at_market.date() - timedelta(days=1)


def yahoo_quote_fetcher(symbol: str, cutoff: date) -> MarketPriceQuote | None:
    """Default fetcher: the real Yahoo daily-bar client, parsed Decimal-safe up to `cutoff`.

    Targets carry the canonical ticker; a vendor's own symbol convention is the vendor
    client's business — Yahoo writes share classes with a hyphen (BRK.B -> BRK-B).

    The window is requested around the CUTOFF, not the wall clock. Asking for the last
    year and filtering afterwards returns nothing for any cutoff older than that, so a
    backfill would report every price cell `FIELD_UNAVAILABLE` instead of failing loudly
    — and a replayed tick would silently pull a different window than the original.

    Imported lazily so the adapter and its tests carry no network dependency.
    """
    import httpx

    from data_engine.sources import yahoo

    vendor_symbol = symbol.replace(".", "-")
    try:
        body, bars = yahoo.fetch_daily_chart(vendor_symbol, end=cutoff)
    except httpx.HTTPError as error:  # transient network/timeout classified by the adapter
        raise SourceUnavailableError(str(error)) from error
    eligible = [bar for bar in bars if bar.date <= cutoff]
    if not eligible:
        return None
    bar = max(eligible, key=lambda item: item.date)
    knowable_at = datetime.combine(bar.date, datetime.min.time(), tzinfo=UTC)
    return MarketPriceQuote(
        # Yahoo's own response, verbatim. This used to be a summary string this function
        # composed ("AAPL:2026-07-24:333.02"), whose digest attested to our formatting
        # rather than to anything the vendor sent, and which no corrected mapping could
        # ever be replayed against.
        raw_bytes=body,
        # `bar.close` is already the recovered Decimal; re-casting through str() here
        # would be a no-op that invites someone to reintroduce a float upstream.
        close=bar.close,
        as_of=bar.date,
        knowable_at=knowable_at,
    )
