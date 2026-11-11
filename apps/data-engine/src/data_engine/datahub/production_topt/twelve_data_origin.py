"""Twelve Data second price origin."""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from data_engine.config import settings
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.corroboration_audit import cached_corroboration_fetch
from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin, MarketPriceQuote
from data_engine.datahub.production_topt.source_registrations import (
    TWELVE_DATA_MAPPING_VERSION,
    TWELVE_DATA_ORIGIN,
    TWELVE_DATA_PARSER_VERSION,
    TWELVE_DATA_VALUE_KEY,
    AsOfAxis,
    knowable_instant,
)
from data_engine.sources import gateway
from data_engine.sources.accept_rules import accept_rule

log = logging.getLogger(__name__)

ORIGIN = TWELVE_DATA_ORIGIN
PARSER_VERSION = TWELVE_DATA_PARSER_VERSION
MAPPING_VERSION = TWELVE_DATA_MAPPING_VERSION
VALUE_KEY = TWELVE_DATA_VALUE_KEY

_EOD_URL = "https://api.twelvedata.com/eod"
_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
_LOOKBACK_DAYS = 10
_LIVE_QUOTE_KEYS = ("price", "extended_price", "extended_change", "extended_percent_change", "extended_timestamp")
_SESSION_DATE_LENGTH = 10
_NO_DATA_PREFIX = "no data is available"
_SETTLED_SEPARATOR = ":"


class TwelveDataError(RuntimeError):
    def __init__(self, code: int | None, message: str, *, http_status: int | None) -> None:
        super().__init__(f"Twelve Data {code if code is not None else 'error'}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


class TwelveDataAuthError(TwelveDataError):
    pass


class TwelveDataRateLimited(TwelveDataError):
    pass


class TwelveDataCreditsExhausted(TwelveDataRateLimited):
    pass


class TwelveDataVendorError(TwelveDataError):
    pass


def classify_error_body(http_status: int | None, body: bytes) -> TwelveDataError | None:
    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        payload = None
    if not isinstance(payload, Mapping):
        if http_status is not None and http_status >= 400:
            return TwelveDataVendorError(
                http_status, f"HTTP {http_status} with a non-JSON body", http_status=http_status
            )
        return None
    if not (payload.get("status") == "error" or (http_status is not None and http_status >= 400)):
        return None
    raw_code = payload.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else http_status
    message = str(payload.get("message") or "")
    if code in (400, 404) and message.strip().lower().startswith(_NO_DATA_PREFIX):
        return None
    if code in (401, 403):
        return TwelveDataAuthError(code, message, http_status=http_status)
    if code == 429:
        kind = TwelveDataCreditsExhausted if "for the day" in message.lower() else TwelveDataRateLimited
        return kind(code, message, http_status=http_status)
    return TwelveDataVendorError(code, message, http_status=http_status)


class NotASessionCloseError(ValueError):
    pass


def _decode(raw_bytes: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw_bytes.decode())
    except (UnicodeDecodeError, ValueError) as error:
        raise NotASessionCloseError("Twelve Data response is not JSON") from error
    if not isinstance(payload, Mapping):
        raise NotASessionCloseError("Twelve Data response is not an object")
    return payload


def _reject_live_quote(payload: Mapping[str, Any]) -> None:
    live = [key for key in _LIVE_QUOTE_KEYS if key in payload]
    if live:
        raise NotASessionCloseError(
            f"Twelve Data returned a live quote ({', '.join(live)}), not a settled session close"
        )


def _session_date(value: object) -> date:
    if not isinstance(value, str) or not value.strip():
        raise NotASessionCloseError("Twelve Data payload carries no session date")
    stamp = value.strip()
    if len(stamp) != _SESSION_DATE_LENGTH:
        raise NotASessionCloseError(f"Twelve Data stamped {stamp!r} with an instant, not a session date")
    try:
        return date.fromisoformat(stamp)
    except ValueError as error:
        raise NotASessionCloseError(f"Twelve Data session stamp {stamp!r} is not a date") from error


def _decimal_close(value: object) -> Decimal:
    if value is None:
        raise NotASessionCloseError("Twelve Data payload carries a session date but no close")
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise NotASessionCloseError(f"Twelve Data close {value!r} is not a number") from error


def _decimal_or_absent(value: object, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise NotASessionCloseError(f"Twelve Data {field} {value!r} is not a number") from error


def _bar(row: Mapping[str, Any]) -> dict[str, Decimal | None]:
    return {field: _decimal_or_absent(row.get(field), field=field) for field in ("open", "high", "low", "volume")}


def _payload_currency(payload: Mapping[str, Any]) -> str | None:
    meta = payload.get("meta")
    raw = payload.get("currency") or (meta.get("currency") if isinstance(meta, Mapping) else None)
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _quote(
    *,
    raw_bytes: bytes,
    as_of: date,
    close: Decimal,
    bar: Mapping[str, Decimal | None] | None = None,
    is_provisional: bool = False,
    currency: str | None = None,
) -> MarketPriceQuote:
    kwargs: dict[str, Any] = {
        "raw_bytes": raw_bytes,
        "close": close,
        "as_of": as_of,
        "knowable_at": knowable_instant(AsOfAxis.SETTLED_SESSION, as_of),
        "is_provisional": is_provisional,
        **(bar or {}),
    }
    if currency is not None:
        kwargs["currency"] = currency
    return MarketPriceQuote(**kwargs)


def parse_session_close(raw_bytes: bytes, *, partition: date) -> MarketPriceQuote | None:
    payload = _decode(raw_bytes)
    _reject_live_quote(payload)
    if payload.get("status") == "error":
        error = classify_error_body(None, raw_bytes)
        if error is not None:
            raise error
        return None
    if "datetime" not in payload and "close" not in payload:
        return None
    as_of = _session_date(payload.get("datetime"))
    if as_of > partition:
        raise NotASessionCloseError(f"Twelve Data returned session {as_of}, after the {partition} partition")
    return _quote(
        raw_bytes=raw_bytes,
        as_of=as_of,
        close=_decimal_close(payload.get("close")),
        currency=_payload_currency(payload),
    )


def parse_last_settled_close(raw_bytes: bytes, *, partition: date) -> MarketPriceQuote | None:
    payload = _decode(raw_bytes)
    _reject_live_quote(payload)
    error = classify_error_body(None, raw_bytes)
    if error is not None:
        raise error
    rows = payload.get("values")
    if not isinstance(rows, list):
        return None
    currency = _payload_currency(payload)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        _reject_live_quote(row)
        as_of = _session_date(row.get("datetime"))
        if as_of >= partition:
            continue
        return _quote(
            raw_bytes=raw_bytes,
            as_of=as_of,
            close=_decimal_close(row.get("close")),
            bar=_bar(row),
            currency=currency,
        )
    return None


def attach_settled_bar(
    raw_bytes: bytes, *, settled: MarketPriceQuote, is_provisional: bool = False
) -> MarketPriceQuote:
    try:
        payload = _decode(raw_bytes)
        _reject_live_quote(payload)
    except NotASessionCloseError:
        return settled
    rows = payload.get("values")
    if not isinstance(rows, list):
        return settled
    currency = _payload_currency(payload) or settled.currency
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            _reject_live_quote(row)
            if _session_date(row.get("datetime")) != settled.as_of:
                continue
            if _decimal_close(row.get("close")) != settled.close:
                return settled
            bar = _bar(row)
        except NotASessionCloseError:
            return settled
        return _quote(
            raw_bytes=raw_bytes,
            as_of=settled.as_of,
            close=settled.close,
            bar=bar,
            is_provisional=is_provisional,
            currency=currency,
        )
    return settled


class TwelveDataQuoteFetcher:
    def __init__(
        self,
        api_key: str,
        *,
        today: date | None = None,
        connection: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("Twelve Data origin requires an API key")
        self._api_key = api_key
        self._today = today
        self._connection = connection
        self._cache: dict[tuple[str, date], MarketPriceQuote | None] = {}
        self._requests = 0
        self._index_hits = 0

    def _from_price_store(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        if self._connection is None:
            return None
        try:
            row = self._connection.execute(
                """
                select trading_date, open, high, low, close, volume, transaction_time
                from staging.market_prices_daily
                where symbol = %s
                  and trading_date <= %s
                  and adjust = 'none'
                order by trading_date desc, recorded_at desc
                limit 1
                """,
                (symbol, cutoff),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        trading_date, open_, high, low, close, volume, transaction_time = row
        from truealpha_contracts.calendar import settled_session_for_cutoff

        settled_date = settled_session_for_cutoff(datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC))
        if trading_date < settled_date:
            return None
        payload = {
            "symbol": symbol,
            "datetime": trading_date.isoformat(),
            "open": None if open_ is None else str(open_),
            "high": None if high is None else str(high),
            "low": None if low is None else str(low),
            "close": str(close),
            "volume": None if volume is None else str(volume),
            "source": "staging.market_prices_daily",
        }
        raw_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        knowable = (
            transaction_time
            if isinstance(transaction_time, datetime)
            else knowable_instant(AsOfAxis.SETTLED_SESSION, trading_date)
        )
        return MarketPriceQuote(
            raw_bytes=raw_bytes,
            close=Decimal(str(close)),
            as_of=trading_date,
            knowable_at=knowable,
            open=None if open_ is None else Decimal(str(open_)),
            high=None if high is None else Decimal(str(high)),
            low=None if low is None else Decimal(str(low)),
            volume=None if volume is None else Decimal(str(volume)),
        )

    def __call__(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        return cached_corroboration_fetch(
            self._cache,
            (symbol, cutoff),
            ORIGIN,
            symbol,
            lambda: self._fetch(symbol, cutoff),
            preload_fn=lambda: self._from_price_store(symbol, cutoff),
        )

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        settled = parse_session_close(
            self._get(_EOD_URL, {"symbol": symbol, "date": str(cutoff)}),
            partition=cutoff,
        )
        series_params = {
            "symbol": symbol,
            "interval": "1day",
            "start_date": str(cutoff - timedelta(days=_LOOKBACK_DAYS)),
            "end_date": str(cutoff + timedelta(days=1)),
            "outputsize": "12",
        }
        today = self._today or datetime.now(UTC).date()
        if settled is not None:
            try:
                series = self._get(_TIME_SERIES_URL, series_params, final=today > cutoff, must_hold=settled.as_of)
            except (TwelveDataError, gateway.CapacityExceeded) as error:
                log.warning(
                    "twelve-data bar for %s not attached (%s: %s); the settled close corroborates alone",
                    symbol,
                    type(error).__name__,
                    error,
                )
                return settled
            return attach_settled_bar(
                series, settled=settled, is_provisional=settled.as_of == cutoff and today == cutoff
            )
        series = self._get(_TIME_SERIES_URL, series_params, final=False)
        return parse_last_settled_close(series, partition=cutoff)

    def _get(self, url: str, params: dict[str, str], *, final: bool = True, must_hold: date | None = None) -> bytes:
        query = urllib.parse.urlencode({**params, "apikey": self._api_key})
        endpoint = url.rsplit("/", 1)[-1]
        key = FetchKey.for_request(
            "twelvedata",
            endpoint,
            params,
            entity_param="symbol",
            as_of_params=("date", "end_date"),
            default_as_of=gateway.utc_day(),
            exclude=("apikey",),
        )
        if must_hold is not None:
            key = replace(key, as_of=f"{key.as_of}{_SETTLED_SEPARATOR}{must_hold.isoformat()}")

        def fetch(identity: FetchKey) -> bytes:
            self._requests += 1
            status, body = gateway.urlopen(
                "twelvedata", endpoint, f"{url}?{query}", identity=identity, caller="twelve_data_origin", timeout=20
            )
            error = classify_error_body(status, body)
            if error is not None:
                raise error
            return body

        requests_before = self._requests
        body = gateway.fetch_once(key, fetch, keep=final)
        if self._requests == requests_before:
            self._index_hits += 1
        return body


def holds_row(body: bytes, session: date) -> bool:
    try:
        values = json.loads(body.decode()).get("values")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return False
    if not isinstance(values, list):
        return False
    return any(isinstance(row, Mapping) and str(row.get("datetime", ""))[:10] == session.isoformat() for row in values)


def carries_data(http_status: int | None, body: bytes) -> bool:
    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return False
    return (
        isinstance(payload, Mapping)
        and payload.get("status") != "error"
        and not (http_status is not None and http_status >= 400)
    )


@accept_rule("twelvedata", "eod")
def _eod_carries_data(_key: FetchKey, body: bytes) -> bool:
    return carries_data(None, body)


@accept_rule("twelvedata", "time_series")
def _series_holds_the_settled_row(key: FetchKey, body: bytes) -> bool:
    settled = key.as_of.partition(_SETTLED_SEPARATOR)[2]
    return carries_data(None, body) and (not settled or holds_row(body, date.fromisoformat(settled)))


def twelve_data_origin(*, connection: Any = None) -> CorroboratingOrigin | None:
    if not settings.twelve_data_api_key:
        return None
    return CorroboratingOrigin(
        origin=ORIGIN,
        parser_version=PARSER_VERSION,
        mapping_version=MAPPING_VERSION,
        value_key=VALUE_KEY,
        fetch=TwelveDataQuoteFetcher(settings.twelve_data_api_key, connection=connection),
    )
