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

## Failover (#862)

Yahoo has no SLA and must not be the sole dependency on a critical path (init.md §5, §9).
When it cannot serve a cell — `SourceUnavailableError`, `TimeoutError`, or no bar at all —
the executor, once the primary's retries are spent, asks `failover`: the same registered
origins, in `RECONCILIATION_POLICY.source_priority` order, for the close of the TARGET's
settled session. The first that has it serves the cell as ITSELF — its parser identity, its
bytes under its own vendor prefix, its record id — with `served_by_failover` in the payload
and its confidence one grade (`PRICE_GRADE_STEP`) below its normal one. The remaining
origins still corroborate. An origin whose close is another session's, or knowable after
the run's cutoff, cannot serve; when no origin can, the cell fails exactly as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

from data_engine.datahub.production_topt.corroboration_audit import FETCH, record_lost_corroboration
from data_engine.datahub.production_topt.executor import (
    SERVED_BY_FAILOVER,
    Corroboration,
    FetchFailure,
    FetchOutcome,
    FetchSuccess,
    NormalizedRecord,
    RawResponse,
)
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION
from data_engine.datahub.production_topt.source_registrations import SOURCE_BY_PARSER
from data_engine.sources.gateway import BudgetExhausted

if TYPE_CHECKING:
    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
    from data_engine.sources.yahoo import PriceBar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketPriceTarget:
    """The resolved fetch parameters and identity coordinates for one work item.

    `cutoff` is the target's settled session (#637). `run_cutoff` is the run's cutoff
    INSTANT: a failover observation is what the snapshot binds, and the snapshot admits
    only what was knowable by then (#862), so a failover close is held to it exactly.
    """

    symbol: str
    cutoff: date
    issuer_id: str
    instrument_id: str
    listing_id: str
    currency: str = "USD"
    run_cutoff: datetime | None = None


@dataclass(frozen=True)
class MarketPriceQuote:
    """One Decimal-safe daily bar plus the immutable raw bytes it was parsed from.

    `close` is the served value and is always present. The other four bar fields are
    what the vendor asserted alongside it and are None when it asserted nothing — a
    Twelve Data bar refused because a post-close print had moved it corroborates close
    alone. Volume is a Decimal like the prices (an exact share count, never a binary
    float) so one Decimal comparison rule serves every field in fusion.
    """

    raw_bytes: bytes
    close: Decimal
    as_of: date
    knowable_at: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    is_provisional: bool = False


# The bar fields both origins assert besides the close. They travel in the payload
# under their own names; the close travels under the origin's declared value key. All
# four are ALWAYS written, so a null under this vintage means "the source asserted
# nothing" and not "nobody asked" — the v6/v7 distinction `parser_identity` records.
BAR_FIELDS: tuple[str, ...] = ("open", "high", "low", "volume")


def bar_payload(quote: MarketPriceQuote, *, close_key: str) -> dict[str, Any]:
    """The five bar values as base-10 strings (null where absent), never binary floats."""
    payload: dict[str, Any] = {close_key: str(quote.close)}
    for field in BAR_FIELDS:
        value = getattr(quote, field)
        payload[field] = None if value is None else str(value)
    if quote.is_provisional:
        payload["is_provisional"] = True
    return payload


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


# The primary failures a further origin may answer (#862): the primary had nothing to say
# — unreachable, too slow, throttled, erroring, no bar, or its daily budget spent (#729: the
# next origin is a different seat, admitted by its own budget). A STOP (look-ahead,
# contract) is a broken run and never failed over; "not yet knowable" is the primary
# asserting the datum does not exist yet, which another origin must not contradict.
FAILOVER_REASONS: frozenset[ObligationReasonCode] = frozenset(
    {
        ObligationReasonCode.TRANSIENT_NETWORK,
        ObligationReasonCode.TIMEOUT,
        ObligationReasonCode.RATE_LIMITED,
        ObligationReasonCode.SERVER_ERROR,
        ObligationReasonCode.FIELD_UNAVAILABLE,
        ObligationReasonCode.DEFERRED_CAPACITY,
        ObligationReasonCode.LOW_CONFIDENCE,
    }
)
# The payload key the mart reads a served close from (`materialization.MarketPricePayload`).
# An origin that writes its close under another key (Twelve Data v1's `price`) cannot serve.
SERVED_VALUE_KEY = "close"


def failover_order(origins: Sequence[CorroboratingOrigin]) -> tuple[CorroboratingOrigin, ...]:
    """The origins that may serve a cell the primary could not, in the fusion policy's
    `source_priority` order (#862).

    An origin qualifies only when the registry recognises its parser vintage, the policy
    ranks the source that vintage belongs to, and it writes its close under the key the
    mart reads. Anything else would be `unregistered` to the fusion engine — its cell
    dropping out of the report — or unreadable by the snapshot.
    """
    # Imported here: the policy lives with the report that applies it, and this module is
    # imported by the origins the report never needs.
    from data_engine.datahub.quality_report import RECONCILIATION_POLICY

    priority = RECONCILIATION_POLICY.source_priority
    ranked: list[tuple[int, int, CorroboratingOrigin]] = []
    for position, origin in enumerate(origins):
        coordinate = SOURCE_BY_PARSER.get(origin.parser_version)
        if coordinate is None or coordinate[0] not in priority[1:]:
            continue
        if origin.value_key != SERVED_VALUE_KEY or coordinate[2] != SERVED_VALUE_KEY:
            continue
        ranked.append((priority.index(coordinate[0]), position, origin))
    return tuple(origin for _rank, _position, origin in sorted(ranked, key=lambda item: item[:2]))


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
        self._failover_origins = failover_order(self._corroborating_origins)

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        target = self._targets.get(work_item.work_item_id)
        if target is None:
            # The plan bound a work item this adapter was not configured for.
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        try:
            quote = self._fetcher(target.symbol, target.cutoff)
        except BudgetExhausted:
            # The gate spent nothing: the seat's daily budget is gone for this environment.
            # Named, never read as "no bar" (rule 6, #729); the gate's listener counted it.
            return FetchFailure(ObligationReasonCode.DEFERRED_CAPACITY)
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
            **bar_payload(quote, close_key="close"),
        }
        return FetchSuccess(
            raw=RawResponse(
                body=quote.raw_bytes,
                source=DataSource.YAHOO,
                record_id=f"chart:{target.symbol}:{quote.as_of.isoformat()}",
            ),
            normalized_sha256=canonical_sha256(payload),
            # A single public feed with no SLA (init.md's yfinance note) — the limitation
            # is represented as confidence, never as a provenance branch downstream;
            # graded per cell against the target's settled session (#641 D6).
            confidence=graded_price_confidence(as_of=quote.as_of, expected_session=target.cutoff),
            valid_from=quote.as_of,
            transaction_time=quote.knowable_at,
            record=NormalizedRecord(payload=payload, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION),
            corroborations=self._corroborate(target),
            failover_reason=ObligationReasonCode.LOW_CONFIDENCE if quote.as_of < target.cutoff else None,
        )

    def failover(self, work_item: CaptureWorkItem, primary_reason: ObligationReasonCode) -> FetchSuccess | None:
        """The next registered origin's close for a cell the primary could not serve (#862).

        Origins are asked in fusion-policy order, each at most once; the first whose close
        is the target's settled session and knowable by the run's cutoff serves the cell
        under its own identity, one confidence grade down. Every other origin still
        corroborates it — the ones already asked from the answer they gave. None when no
        origin can serve, and the executor resolves the cell as it always has.
        """
        target = self._targets.get(work_item.work_item_id)
        if target is None or primary_reason not in FAILOVER_REASONS:
            return None
        asked: dict[int, MarketPriceQuote | None] = {}
        for origin in self._failover_origins:
            quote = self._origin_quote(origin, target)
            asked[id(origin)] = quote
            if quote is None or not _serves_session(quote, target):
                continue
            payload = {
                **self._identity(target),
                **bar_payload(quote, close_key=origin.value_key),
                SERVED_BY_FAILOVER: origin.origin,
            }
            log.warning(
                "primary price source could not serve %s (%s); served by failover origin %s for session %s",
                target.symbol,
                primary_reason.value,
                origin.origin,
                quote.as_of.isoformat(),
            )
            others = [
                (other, asked[id(other)] if id(other) in asked else self._origin_quote(other, target))
                for other in self._corroborating_origins
                if other is not origin
            ]
            return FetchSuccess(
                raw=self._raw(origin, target, quote),
                normalized_sha256=canonical_sha256(payload),
                confidence=failover_confidence(origin.confidence),
                valid_from=quote.as_of,
                transaction_time=quote.knowable_at,
                record=NormalizedRecord(
                    payload=payload, parser_version=origin.parser_version, mapping_version=origin.mapping_version
                ),
                corroborations=tuple(
                    corroboration
                    for other, answer in others
                    if (corroboration := self._corroboration(other, target, answer)) is not None
                ),
                served_by_failover=origin.origin,
            )
        return None

    def _corroborate(self, target: MarketPriceTarget) -> tuple[Corroboration, ...]:
        found: list[Corroboration] = []
        for origin in self._corroborating_origins:
            corroboration = self._corroboration(origin, target, self._origin_quote(origin, target))
            if corroboration is not None:
                found.append(corroboration)
        return tuple(found)

    @staticmethod
    def _origin_quote(origin: CorroboratingOrigin, target: MarketPriceTarget) -> MarketPriceQuote | None:
        try:
            return origin.fetch(target.symbol, target.cutoff)
        except Exception as error:  # noqa: BLE001 - a second origin never fails the primary capture
            # ...but it is never silent either (#885): logged with its type, counted.
            record_lost_corroboration(origin.origin, FETCH, target.symbol, error)
            return None

    @staticmethod
    def _identity(target: MarketPriceTarget) -> dict[str, Any]:
        return {
            "issuer_id": target.issuer_id,
            "instrument_id": target.instrument_id,
            "listing_id": target.listing_id,
            "currency": target.currency,
        }

    @staticmethod
    def _raw(origin: CorroboratingOrigin, target: MarketPriceTarget, quote: MarketPriceQuote) -> RawResponse:
        # The origin's own vendor prefix and record id: its bytes, never the primary's.
        return RawResponse(
            body=quote.raw_bytes,
            source=origin.raw_source,
            record_id=f"{origin.origin}:{target.symbol}:{quote.as_of.isoformat()}",
        )

    def _corroboration(
        self, origin: CorroboratingOrigin, target: MarketPriceTarget, quote: MarketPriceQuote | None
    ) -> Corroboration | None:
        if quote is None or quote.knowable_at.date() > target.cutoff:
            return None
        payload = {
            **self._identity(target),
            **bar_payload(quote, close_key=origin.value_key),
            "origin": origin.origin,
        }
        return Corroboration(
            origin=origin.origin,
            transaction_time=quote.knowable_at,
            record=NormalizedRecord(
                payload=payload,
                parser_version=origin.parser_version,
                mapping_version=origin.mapping_version,
            ),
            confidence=origin.confidence,
            raw=self._raw(origin, target, quote),
            normalized_sha256=canonical_sha256(payload),
        )


def _serves_session(quote: MarketPriceQuote, target: MarketPriceTarget) -> bool:
    """A failover close must be THE target's settled session — another day's close is a
    different datum, not a substitute — and knowable by the run's cutoff: the date rule the
    primary obeys, and the instant rule the snapshot applies to what it binds (#862)."""
    if quote.as_of != target.cutoff or quote.knowable_at.date() > target.cutoff:
        return False
    return target.run_cutoff is None or quote.knowable_at <= target.run_cutoff


# The price confidence scale (#641 D6): the no-SLA primary's fresh-close grade, the step one
# grade costs, and the floor no price cell falls below.
PRIMARY_PRICE_CONFIDENCE = Decimal("0.85")
PRICE_GRADE_STEP = Decimal("0.10")
PRICE_CONFIDENCE_FLOOR = Decimal("0.50")


def graded_price_confidence(*, as_of: date, expected_session: date) -> Decimal:
    """Per-cell price confidence (#641 D6) — a grade, not a constant.

    Rule 15 mandates per-cell confidence; financial facts honor it (0.50-0.92
    on the current heads) while every price cell asserted a flat 0.85. The
    grade starts at that same 0.85 (the no-SLA primary, init.md's yfinance
    note) and drops 0.10 per SESSION the served bar lags the last settled
    session, floored at 0.50 — a capture that had to fall back (the vendor's
    overnight null-close window #622, holidays) now says so in its confidence
    instead of asserting the fresh-close grade for stale data.
    """
    lag_sessions = sum(
        1
        for offset in range(1, max((expected_session - as_of).days, 0) + 1)
        if (as_of + timedelta(days=offset)).weekday() < 5
    )
    graded = PRIMARY_PRICE_CONFIDENCE - PRICE_GRADE_STEP * lag_sessions
    return max(graded, PRICE_CONFIDENCE_FLOOR)


def failover_confidence(origin_confidence: Decimal) -> Decimal:
    """A failover-served close's confidence: its origin's normal grade, one step down (#862).

    The step is the one a session of lag costs (`graded_price_confidence`): the cell was
    served, but not by the source the policy prefers, and the no-SLA primary it replaced
    is exactly the dependency init.md §9 says a critical path must not rest on alone.
    """
    return max(origin_confidence - PRICE_GRADE_STEP, PRICE_CONFIDENCE_FLOOR)


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
    candidate = at_market.date() if at_market.time() >= time(16, 0) else at_market.date() - timedelta(days=1)
    # Clamp to a weekday so the returned value IS a session date as named — a
    # Saturday-evening cutoff must answer Friday, not Saturday (review on #638).
    # Market holidays stay uncorrected without a calendar; the fetcher's `<=`
    # max-pick falls back to the last real bar for those.
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


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
    return quote_from_chart(body, bars, cutoff=cutoff)


def quote_from_chart(body: bytes, bars: Sequence[PriceBar], *, cutoff: date) -> MarketPriceQuote | None:
    """The newest bar at/before `cutoff` as the quote the adapter asserts: the vendor's
    verbatim bytes and the WHOLE recovered bar, not only its close.

    Separated from the HTTP call so the cassette suite drives exactly this selection
    over the bytes production captured (`tests/production_topt/test_real_vendor_bytes.py`).
    """
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
        # would be a no-op that invites someone to reintroduce a float upstream. The
        # same float32 recovery already ran on open/high/low at the parse boundary.
        close=bar.close,
        as_of=bar.date,
        knowable_at=knowable_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        # The chart sends volume as a JSON integer; Decimal(int) is exact.
        volume=None if bar.volume is None else Decimal(bar.volume),
    )


# -- registry route (#72) -----------------------------------------------------------------


def build_route(context: RouteContext, cells: Sequence[RouteCell]) -> MarketPriceAdapter:
    """The market-price source's own routing: one target per planned cell, the Yahoo
    primary, the Twelve Data second origin and the moomoo K-line third origin — which are
    also, in that order, the failovers for a cell Yahoo cannot serve (#862). Named by
    the `yahoo-chart` registration in `source_registrations.py`; the composition root
    never sees these types."""
    from data_engine.datahub.production_topt.moomoo_origin import moomoo_kline_origin
    from data_engine.datahub.production_topt.twelve_data_origin import twelve_data_origin

    targets = {
        cell.work_item_id: MarketPriceTarget(
            symbol=cell.ticker,
            # Price targets get the last SETTLED session, not the calendar date: a
            # mid-session run must not treat the in-progress bar as a close (#637).
            cutoff=context.price_cutoff_date,
            issuer_id=cell.issuer_id,
            instrument_id=cell.instrument_id,
            listing_id=cell.listing_id,
            # The run's cutoff instant: what a failover close must be knowable by (#862).
            run_cutoff=context.cutoff,
        )
        for cell in cells
    }
    # Priority order is the fusion policy's (`quality_report.RECONCILIATION_POLICY`); an
    # origin that is not configured for this environment is simply not asked. The same
    # origins serve, in that order, a cell the primary cannot (#862, `failover_order`).
    origins = [origin for origin in (twelve_data_origin(), moomoo_kline_origin()) if origin is not None]
    return MarketPriceAdapter(targets, yahoo_quote_fetcher, corroborating_origins=tuple(origins))
