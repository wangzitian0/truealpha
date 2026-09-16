"""Twelve Data second price origin (#344 / #171 A2c, init.md rule 15).

A market-price cell is only independently reconciled when two origins assert it, so this
module supplies the second: a Decimal-safe close at/before the cutoff from Twelve Data,
shaped as a `CorroboratingOrigin` the market-price adapter attaches to its success. The
fusion engine (#343) then reconciles two real assertions under the declared tolerance
policy — counting origins never reconciles values. When the primary cannot serve a cell at
all, this origin is the first failover (#862): its settled close for the target's session
serves the cell under this origin's identity, one confidence grade down.

The key comes from `settings.twelve_data_api_key` (rendered into the runtime env from
Vault by infra2's `20.data_engine/secrets.ctmpl`), never `os.environ` in-line. With no key
configured, `twelve_data_origin()` returns None and every cell is honestly single-origin.

## The quantity, not just the value (#535)

Reconciliation compares two numbers, so both origins have to be asserting the same
*quantity*. The primary (Yahoo) asserts a session's regular-session close. v1 of this
origin took the newest `time_series` row at/before the cutoff — which, on the partition
date itself, is the session's IN-PROGRESS bar: Twelve Data keeps it moving with the last
trade, extended hours included. Every scheduled 22:15 UTC tick therefore compared a
settled close against a post-market last trade (AAPL 2026-07-29: 338.19 vs 340.079987,
0.56% apart against a 0.1% tolerance) and reconciliation reported `conflict_abstained` on
almost every cell. The origin looked present and was arithmetically useless.

v2 asks the `/eod` endpoint for the partition date's settled end-of-day close, and the
parser *asserts* that quantity rather than trusting it: a live-quote or intraday payload
raises `NotASessionCloseError` and the origin is absent, so a wrong quantity can never
again become a silent corroboration input. When the partition date has no end of day at
all — a weekend, a holiday, a tick that runs before the session — the last session that
has *settled* is used instead, which is the same session the primary resolves to.

## The whole bar, under the same discipline (v3)

`/eod` carries the close and nothing else (vendor contract); the session's open, high,
low and volume live only on a `time_series` row. v3 fetches that window after a settled
`/eod` and attaches the bar from the row for the settled session — but only when that
row closes AT the `/eod` close. On the partition date the row is the in-progress bar
until the day rolls over (the #535 defect), and a row still absorbing post-close prints
has a close other than the settled one: equal closes are the falsifier that says the
row's open/high/low/volume are the regular session's. A moved row, a missing row or an
error body leaves the cell exactly what v2 made it — a corroborated close, four honest
nulls — so the fusion engine grades those fields `insufficient_independent_origins`
rather than comparing a settled Yahoo bar against an extended-hours one within
tolerance. The no-end-of-day fallback already fetched the series; its settled row now
carries its bar at no extra credit. A settled weekday therefore costs two credits per
listing where v2 spent one; the weekend/holiday path still spends two.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from data_engine.config import settings
from data_engine.datahub.production_topt.corroboration_audit import FETCH, record_lost_corroboration
from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin, MarketPriceQuote
from data_engine.datahub.production_topt.source_registrations import (
    TWELVE_DATA_MAPPING_VERSION,
    TWELVE_DATA_ORIGIN,
    TWELVE_DATA_PARSER_VERSION,
    TWELVE_DATA_VALUE_KEY,
)
from data_engine.sources import gateway

ORIGIN = TWELVE_DATA_ORIGIN
# v1 -> v2 (#535): the asserted quantity changed from Twelve Data's last trade (the
# in-progress daily bar, extended hours included) to the partition date's settled
# end-of-day close, which is what the primary asserts. A different quantity under the
# same parser identity would make the two indistinguishable in the warehouse.
PARSER_VERSION = TWELVE_DATA_PARSER_VERSION
MAPPING_VERSION = TWELVE_DATA_MAPPING_VERSION
# The payload key this origin writes its number under. It is also the key
# `quality_report._SOURCE_BY_PARSER` reads back for this parser vintage; the two are
# asserted equal in the tests, because a silent drift between them drops the second
# origin out of fusion without any error.
VALUE_KEY = TWELVE_DATA_VALUE_KEY

_EOD_URL = "https://api.twelvedata.com/eod"
_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
# The free tier allows 8 requests per minute; one full TOPT tick is 21 listings.
_THROTTLE_SECONDS = 8
_LOOKBACK_DAYS = 10
# Keys that only ever appear on a live quote (`/price`) or a real-time snapshot
# (`/quote`, whose `extended_*` fields are exactly the post-market trade that made v1
# disagree). Their presence means the payload is not an end-of-day figure.
_LIVE_QUOTE_KEYS = ("price", "extended_price", "extended_change", "extended_percent_change", "extended_timestamp")
# A settled close is stamped with its session's date and nothing finer; a live quote or an
# intraday bar carries an instant ("2026-07-29 15:59:00").
_SESSION_DATE_LENGTH = 10


class NotASessionCloseError(ValueError):
    """Twelve Data answered with a quantity that is not a settled session close.

    Distinct from "the vendor had nothing": nothing is honest and leaves the cell
    single-origin, whereas a live quote silently corroborating a session close is the
    #535 defect. The parser refuses it here rather than passing the number on.
    """


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
    """A bar field the row may legitimately omit (`volume` is documented optional): an
    absent or empty figure is an absent assertion, never zero."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise NotASessionCloseError(f"Twelve Data {field} {value!r} is not a number") from error


def _bar(row: Mapping[str, Any]) -> dict[str, Decimal | None]:
    """The series row's open/high/low/volume, Decimal each, None where the row has none."""
    return {field: _decimal_or_absent(row.get(field), field=field) for field in ("open", "high", "low", "volume")}


def _quote(
    *, raw_bytes: bytes, as_of: date, close: Decimal, bar: Mapping[str, Decimal | None] | None = None
) -> MarketPriceQuote:
    return MarketPriceQuote(
        raw_bytes=raw_bytes,
        close=close,
        as_of=as_of,
        knowable_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
        **(bar or {}),
    )


def parse_session_close(raw_bytes: bytes, *, partition: date) -> MarketPriceQuote | None:
    """The settled end-of-day close Twelve Data reports for `partition`.

    Returns None when the vendor honestly has no end of day for that date — a weekend, a
    holiday, a session that has not run yet, or an error body. Raises
    `NotASessionCloseError` when the payload asserts a *different quantity*: a live or
    extended-hours quote, an intraday bar, or a session after the partition date.
    """
    payload = _decode(raw_bytes)
    _reject_live_quote(payload)
    if payload.get("status") == "error":
        return None
    if "datetime" not in payload and "close" not in payload:
        return None
    as_of = _session_date(payload.get("datetime"))
    if as_of > partition:
        # A close from after the partition date is look-ahead, not corroboration.
        raise NotASessionCloseError(f"Twelve Data returned session {as_of}, after the {partition} partition")
    return _quote(raw_bytes=raw_bytes, as_of=as_of, close=_decimal_close(payload.get("close")))


def parse_last_settled_close(raw_bytes: bytes, *, partition: date) -> MarketPriceQuote | None:
    """The newest daily close from a session that ended STRICTLY BEFORE `partition`.

    Used only when the partition date has no end of day of its own. The strict inequality
    is the whole point: a tick is stamped inside its partition date, and every US session
    before that date closed before that date began in UTC, so any row selected here is
    settled. Reading the partition date's own row — v1's rule — is what admitted the
    in-progress bar (#535).
    """
    payload = _decode(raw_bytes)
    _reject_live_quote(payload)
    rows = payload.get("values")
    if not isinstance(rows, list):
        return None
    for row in rows:  # newest first
        if not isinstance(row, Mapping):
            continue
        _reject_live_quote(row)
        as_of = _session_date(row.get("datetime"))
        if as_of >= partition:
            continue
        return _quote(raw_bytes=raw_bytes, as_of=as_of, close=_decimal_close(row.get("close")), bar=_bar(row))
    return None


def attach_settled_bar(raw_bytes: bytes, *, settled: MarketPriceQuote) -> MarketPriceQuote:
    """`settled` (the `/eod` close) with its session's bar from a `time_series` window,
    when that window has a row for the session that closes AT the settled close.

    `/eod` carries only the close. The bar lives on the series row, and on the partition
    date that row is the in-progress bar until the day rolls over (#535). A row still
    moving has a close other than the settled one — that is the falsifier: equal closes
    mean no post-close print has entered the bar, so its open/high/low/volume are the
    regular session's. Anything else — a moved row, no row for the session, an error
    body, a live quote — returns `settled` untouched and the cell corroborates close
    alone. The bar is additive; it can never make the close-only cell worse.

    When the bar IS attached the landed bytes become the series body: every number the
    corroboration then asserts is in it, the close included, verbatim.
    """
    try:
        payload = _decode(raw_bytes)
        _reject_live_quote(payload)
    except NotASessionCloseError:
        return settled
    rows = payload.get("values")
    if not isinstance(rows, list):
        return settled
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            _reject_live_quote(row)
            if _session_date(row.get("datetime")) != settled.as_of:
                continue
            if _decimal_close(row.get("close")) != settled.close:
                return settled  # still moving: not the session's settled bar
            bar = _bar(row)
        except NotASessionCloseError:
            return settled
        return _quote(raw_bytes=raw_bytes, as_of=settled.as_of, close=settled.close, bar=bar)
    return settled


class TwelveDataQuoteFetcher:
    """`MarketPriceFetcher` over Twelve Data's end-of-day close and its session's bar.

    Memoized per symbol for the life of one run and throttled to the free tier's rate.
    A tick issues two requests per listing: `/eod` for the settled close, then the
    `time_series` window that carries the bar — or, on a partition date with no end of
    day, resolves the last settled session (bar included) instead.
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
        try:
            quote = self._fetch(symbol, cutoff)
        except Exception as error:  # noqa: BLE001 - a second origin that errors is simply absent
            # Absorbed here rather than raised, so the throttle below still runs: a
            # rate-limited request that skipped its wait would fire the next symbol
            # immediately and rate-limit the rest of the tick with it. A
            # `NotASessionCloseError` lands here too — a refused quantity leaves the cell
            # honestly single-origin instead of corroborating it with the wrong number.
            # Absent, but not silent (#885): a revoked key and a refused quantity are
            # logged with their type and counted in the tick's summary.
            record_lost_corroboration(ORIGIN, FETCH, symbol, error)
            quote = None
        self._cache[key] = quote
        if self._throttle_seconds:
            time.sleep(self._throttle_seconds)
        return quote

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        settled = parse_session_close(
            self._get(_EOD_URL, {"symbol": symbol, "date": str(cutoff)}),
            partition=cutoff,
        )
        if self._throttle_seconds:
            time.sleep(self._throttle_seconds)
        series = self._get(
            _TIME_SERIES_URL,
            {
                "symbol": symbol,
                "interval": "1day",
                "start_date": str(cutoff - timedelta(days=_LOOKBACK_DAYS)),
                "end_date": str(cutoff),
                "outputsize": "12",
            },
        )
        if settled is not None:
            # The close is settled by `/eod`; the bar comes from the series window and is
            # attached only when its row closes at that settled close (v3, header).
            return attach_settled_bar(series, settled=settled)
        # No end of day for the partition date itself. Resolve the last session that HAS
        # settled — the same session the primary resolves to on a weekend or holiday.
        return parse_last_settled_close(series, partition=cutoff)

    def _get(self, url: str, params: dict[str, str]) -> bytes:
        query = urllib.parse.urlencode({**params, "apikey": self._api_key})
        # Twelve Data answers "no end of day for this date" with HTTP 400 and a JSON
        # error body — and urlopen RAISES on any non-2xx. Letting that raise propagate
        # skipped the parser's error-body path and with it the last-settled-session
        # fallback, so every tick inside an unsettled session lost the whole second
        # origin (staging 2026-08-14 06:08: 21/21 insufficient_independent_origins, zero
        # twelvedata fetch rows, while the key was provisioned and the quota untouched).
        # The body IS the vendor's answer: the gateway returns it (status-honest) and the
        # parser refuses it, so the fallback gets its turn. Since #729 that 400 is also a
        # ledger row (`ok = false`, the vendor's message as `error`) — every weekend tick
        # spends two credits per listing this way, and until the ledger existed nothing
        # recorded it (raw.fetches only ever saw the landed successes). Since v3 every
        # tick spends two: the second request is the bar's on a settled day.
        _status, body = gateway.urlopen(
            "twelvedata", url.rsplit("/", 1)[-1], f"{url}?{query}", caller="twelve_data_origin", timeout=20
        )
        return body


def twelve_data_origin() -> CorroboratingOrigin | None:
    """The configured second price origin, or None when no key is provisioned."""
    if not settings.twelve_data_api_key:
        return None
    return CorroboratingOrigin(
        origin=ORIGIN,
        parser_version=PARSER_VERSION,
        mapping_version=MAPPING_VERSION,
        value_key=VALUE_KEY,
        confidence=Decimal("0.85"),
        fetch=TwelveDataQuoteFetcher(settings.twelve_data_api_key),
    )
