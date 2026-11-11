"""Market-price source adapter with corroboration and failover."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from truealpha_contracts import reconciliation_policies
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
from data_engine.datahub.production_topt.failover_drill import FailoverDrill
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION, PARSER_VERSION
from data_engine.datahub.production_topt.source_registrations import SOURCE_BY_PARSER, uses_twelve_data
from data_engine.sources.gateway import BudgetExhausted, CapacityExceeded

if TYPE_CHECKING:
    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
    from data_engine.sources.yahoo import PriceBar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketPriceTarget:
    symbol: str
    cutoff: date
    issuer_id: str
    instrument_id: str
    listing_id: str
    run_cutoff: datetime | None = None


_DEFAULT_QUOTE_CURRENCY = "USD"


@dataclass(frozen=True)
class MarketPriceQuote:
    raw_bytes: bytes
    close: Decimal
    as_of: date
    knowable_at: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    is_provisional: bool = False
    currency: str | None = _DEFAULT_QUOTE_CURRENCY


BAR_FIELDS: tuple[str, ...] = ("open", "high", "low", "volume")


def bar_payload(quote: MarketPriceQuote, *, close_key: str) -> dict[str, Any]:
    payload: dict[str, Any] = {close_key: str(quote.close)}
    for field in BAR_FIELDS:
        value = getattr(quote, field)
        payload[field] = None if value is None else str(value)
    if quote.is_provisional:
        payload["is_provisional"] = True
    return payload


MarketPriceFetcher = Callable[[str, date], MarketPriceQuote | None]


@dataclass(frozen=True)
class CorroboratingOrigin:
    origin: str
    parser_version: str
    mapping_version: str
    value_key: str
    fetch: MarketPriceFetcher
    confidence: Decimal | None = None
    raw_source: DataSource = DataSource.TWELVE_DATA


class SourceUnavailableError(Exception):
    pass


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
SERVED_VALUE_KEY = "close"


def failover_order(origins: Sequence[CorroboratingOrigin]) -> tuple[CorroboratingOrigin, ...]:
    priority = reconciliation_policies.RECONCILIATION_POLICY.source_priority
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
            return FetchFailure(ObligationReasonCode.CONTRACT_VIOLATION)
        try:
            quote = self._fetcher(target.symbol, target.cutoff)
        except (BudgetExhausted, CapacityExceeded):
            return FetchFailure(ObligationReasonCode.DEFERRED_CAPACITY)
        except SourceUnavailableError:
            return FetchFailure(ObligationReasonCode.TRANSIENT_NETWORK)
        except TimeoutError:
            return FetchFailure(ObligationReasonCode.TIMEOUT)
        if quote is None:
            return FetchFailure(ObligationReasonCode.FIELD_UNAVAILABLE)
        if quote.knowable_at.date() > target.cutoff:
            return FetchFailure(ObligationReasonCode.LOOK_AHEAD_VIOLATION)
        payload = {**self._identity(target, quote), **bar_payload(quote, close_key="close")}
        return FetchSuccess(
            raw=RawResponse(
                body=quote.raw_bytes,
                source=DataSource.YAHOO,
                record_id=f"chart:{target.symbol}:{quote.as_of.isoformat()}",
            ),
            normalized_sha256=canonical_sha256(payload),
            valid_from=quote.as_of,
            transaction_time=quote.knowable_at,
            record=NormalizedRecord(payload=payload, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION),
            corroborations=self._corroborate(target),
            failover_reason=ObligationReasonCode.LOW_CONFIDENCE if quote.as_of < target.cutoff else None,
        )

    def failover(self, work_item: CaptureWorkItem, primary_reason: ObligationReasonCode) -> FetchSuccess | None:
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
                **self._identity(target, quote),
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
        return tuple(
            corroboration
            for origin in self._corroborating_origins
            if (corroboration := self._corroboration(origin, target, self._origin_quote(origin, target))) is not None
        )

    @staticmethod
    def _origin_quote(origin: CorroboratingOrigin, target: MarketPriceTarget) -> MarketPriceQuote | None:
        try:
            return origin.fetch(target.symbol, target.cutoff)
        except Exception as error:  # noqa: BLE001
            record_lost_corroboration(origin.origin, FETCH, target.symbol, error)
            return None

    @staticmethod
    def _identity(target: MarketPriceTarget, quote: MarketPriceQuote) -> dict[str, Any]:
        return {
            "issuer_id": target.issuer_id,
            "instrument_id": target.instrument_id,
            "listing_id": target.listing_id,
            "currency": quote.currency,
        }

    @staticmethod
    def _raw(origin: CorroboratingOrigin, target: MarketPriceTarget, quote: MarketPriceQuote) -> RawResponse:
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
            **self._identity(target, quote),
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
    if quote.as_of != target.cutoff or quote.knowable_at.date() > target.cutoff:
        return False
    return target.run_cutoff is None or quote.knowable_at <= target.run_cutoff


def last_settled_session_date(cutoff: datetime) -> date:
    from truealpha_contracts.calendar import settled_session_for_cutoff

    return settled_session_for_cutoff(cutoff)


def yahoo_quote_fetcher(symbol: str, cutoff: date) -> MarketPriceQuote | None:
    import httpx

    from data_engine.sources import yahoo

    try:
        body, bars = yahoo.fetch_daily_chart(symbol.replace(".", "-"), end=cutoff)
    except httpx.HTTPError as error:
        raise SourceUnavailableError(str(error)) from error
    return quote_from_chart(body, bars, cutoff=cutoff)


def quote_from_chart(body: bytes, bars: Sequence[PriceBar], *, cutoff: date) -> MarketPriceQuote | None:
    from data_engine.datahub.production_topt.source_registrations import AsOfAxis, knowable_instant

    eligible = [bar for bar in bars if bar.date <= cutoff]
    if not eligible:
        return None
    bar = max(eligible, key=lambda item: item.date)
    currency = getattr(bar, "currency", None)
    if currency is None:
        try:
            parsed = json.loads(body.decode("utf-8", "ignore"))
            result = parsed.get("chart", {}).get("result") if isinstance(parsed, dict) else None
            meta = (
                result[0].get("meta") if isinstance(result, list) and result and isinstance(result[0], dict) else None
            )
            raw_currency = meta.get("currency") if isinstance(meta, dict) else None
            currency = raw_currency.strip() if isinstance(raw_currency, str) and raw_currency.strip() else None
        except ValueError:
            currency = None
    return MarketPriceQuote(
        raw_bytes=body,
        close=bar.close,
        as_of=bar.date,
        knowable_at=knowable_instant(AsOfAxis.SETTLED_SESSION, bar.date),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        volume=None if bar.volume is None else Decimal(bar.volume),
        currency=currency or _DEFAULT_QUOTE_CURRENCY,
    )


def build_route(
    context: RouteContext,
    cells: Sequence[RouteCell],
    drill: FailoverDrill | None = None,
) -> MarketPriceAdapter:
    from data_engine.config import settings
    from data_engine.datahub.production_topt.moomoo_origin import moomoo_kline_origin
    from data_engine.datahub.production_topt.twelve_data_origin import twelve_data_origin

    targets = {
        cell.work_item_id: MarketPriceTarget(
            symbol=cell.ticker,
            cutoff=context.price_cutoff_date,
            issuer_id=cell.issuer_id,
            instrument_id=cell.instrument_id,
            listing_id=cell.listing_id,
            run_cutoff=context.cutoff,
        )
        for cell in cells
    }
    td_origin: CorroboratingOrigin | None = None
    if uses_twelve_data(getattr(context, "universe_id", None)):
        try:
            td_origin = twelve_data_origin(connection=context.connection)
        except TypeError:
            td_origin = twelve_data_origin()
    origins = [origin for origin in (td_origin, moomoo_kline_origin()) if origin is not None]
    fetcher: MarketPriceFetcher = yahoo_quote_fetcher
    effective_drill = drill or getattr(context, "drill", None)
    if effective_drill is not None and hasattr(effective_drill, "arm"):
        fetcher, armed_origins = effective_drill.arm(settings.app_env, fetcher, origins)
        origins = list(armed_origins)
    return MarketPriceAdapter(targets, fetcher, corroborating_origins=tuple(origins))
