"""Yahoo Finance daily bars with cent recovery."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import httpx
from factors.backtest.corporate_actions import MAX_FACTOR_DENOMINATOR, SplitEvent, split_factor_between

from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources.accept_rules import accept_rule

if TYPE_CHECKING:
    from data_engine.datahub.market_prices import PriceBarRecord

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "TrueAlpha research wangzitian.ai@icloud.com"
EXACT_CENT_RECOVERY_BELOW = Decimal(2**16)
DEFAULT_CHART_PERIOD_DAYS = 1125
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
        currency: str | None = None,
    ):
        self.date, self.open, self.high, self.low = day, open_, high, low
        self.close, self.adj_close, self.volume, self.currency = close, adj_close, volume, currency


def recover_quoted_price(value: Decimal | None) -> Decimal | None:
    if value is None or abs(value) >= EXACT_CENT_RECOVERY_BELOW:
        return value
    return value.quantize(_CENT)


def _epoch(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), tzinfo=UTC).timestamp())


def fetch_daily_chart(
    symbol: str, *, end: date | None = None, period_days: int = DEFAULT_CHART_PERIOD_DAYS
) -> tuple[bytes, list[PriceBar]]:
    settled_end = end is not None
    end = end or date.today()
    start = end - timedelta(days=period_days)
    params = {
        "period1": str(_epoch(start)),
        "period2": str(_epoch(end + timedelta(days=1))),
        "interval": "1d",
        "events": "splits",
    }
    key = FetchKey.for_request(
        "yahoo",
        "chart",
        {
            "symbol": symbol,
            "interval": "1d",
            "period_days": str(period_days),
            "end": end.isoformat(),
            "events": "splits",
        },
        entity_param="symbol",
        as_of_params=("end",),
        default_as_of=gateway.utc_day(),
    )

    def fetch(identity: FetchKey) -> bytes:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
            resp = gateway.http_get(
                client,
                "yahoo",
                "chart",
                CHART_URL.format(symbol=symbol),
                identity=identity,
                caller="yahoo.fetch_daily_chart",
                params=params,
            )
            resp.raise_for_status()
            return bytes(resp.content)

    body = gateway.fetch_once(key, fetch, keep=settled_end)
    payload = json.loads(body.decode(), parse_float=Decimal)
    return body, _parse_chart_response(payload)


@accept_rule("yahoo", "chart")
def _chart_settles_its_session(key: FetchKey, body: bytes) -> bool:
    return _session_is_final(body, date.fromisoformat(key.as_of))


def _session_is_final(body: bytes, end: date) -> bool:
    from data_engine.datahub.market_prices import is_xnys_trading_day, xnys_session_close_utc

    if end > gateway._utcnow().date():
        return False
    if not is_xnys_trading_day(end):
        return True
    if gateway._utcnow() < xnys_session_close_utc(end):
        return False
    return any(bar.date == end for bar in _parse_chart_response(json.loads(body.decode(), parse_float=Decimal)))


def fetch_daily_bars(
    symbol: str, *, end: date | None = None, period_days: int = DEFAULT_CHART_PERIOD_DAYS
) -> list[PriceBar]:
    return fetch_daily_chart(symbol, end=end, period_days=period_days)[1]


def _chart_currency(meta: Any) -> str | None:
    if not isinstance(meta, dict):
        return None
    raw_currency = meta.get("currency")
    return raw_currency.strip() if isinstance(raw_currency, str) and raw_currency.strip() else None


def _parse_chart_response(payload: dict[str, Any]) -> list[PriceBar]:
    result = payload.get("chart", {}).get("result")
    if not result:
        return []
    r = result[0]
    timestamps = r.get("timestamp") or []
    quotes = r.get("indicators", {}).get("quote") or []
    if not timestamps or not quotes:
        return []
    quote = quotes[0]
    closes = quote.get("close")
    if not closes:
        return []
    currency = _chart_currency(r.get("meta"))
    adjclose_list = r.get("indicators", {}).get("adjclose") or [{}]
    adjclose = (adjclose_list[0].get("adjclose") if adjclose_list else None) or closes

    def _at(seq: Any, idx: int) -> Any:
        return seq[idx] if isinstance(seq, list) and idx < len(seq) else None

    bars = []
    for i, ts in enumerate(timestamps[: len(closes)]):
        close = recover_quoted_price(closes[i])
        if close is None:
            continue
        bars.append(
            PriceBar(
                day=datetime.fromtimestamp(ts, tz=UTC).date(),
                open_=recover_quoted_price(_at(quote.get("open"), i)),
                high=recover_quoted_price(_at(quote.get("high"), i)),
                low=recover_quoted_price(_at(quote.get("low"), i)),
                close=close,
                adj_close=recover_quoted_price(_at(adjclose, i)),
                volume=_at(quote.get("volume"), i),
                currency=currency,
            )
        )
    return bars


def _parse_split_events(symbol: str, payload: dict[str, Any]) -> tuple[SplitEvent, ...]:
    result = payload.get("chart", {}).get("result")
    if not result or not isinstance(result[0], dict):
        return ()
    raw_splits = (result[0].get("events") or {}).get("splits")
    if not isinstance(raw_splits, dict):
        return ()
    parsed: list[SplitEvent] = []
    for item in raw_splits.values():
        if not isinstance(item, dict):
            continue
        raw_ts, raw_num, raw_den = item.get("date"), item.get("numerator"), item.get("denominator")
        if raw_ts is None or raw_num is None or raw_den is None:
            continue
        num_dec, den_dec = Decimal(str(raw_num)), Decimal(str(raw_den))
        if num_dec <= 0 or den_dec <= 0:
            continue
        factor = (Fraction(num_dec) / Fraction(den_dec)).limit_denominator(MAX_FACTOR_DENOMINATOR)
        if factor == 1 or factor.denominator > MAX_FACTOR_DENOMINATOR:
            continue
        ex_date = datetime.fromtimestamp(int(raw_ts), tz=UTC).date()
        parsed.append(SplitEvent(symbol, ex_date, factor.numerator, factor.denominator))
    return tuple(sorted(parsed, key=lambda event: event.ex_date))


def _recovered_pair(raw_value: Any, *, factor: Fraction) -> tuple[Decimal | None, Decimal | None]:
    if raw_value is None:
        return None, None
    dec = raw_value if isinstance(raw_value, Decimal) else Decimal(str(raw_value))
    if factor == 1:
        val = recover_quoted_price(dec)
        return val, val
    scale = Decimal(factor.numerator) / Decimal(factor.denominator)
    unadjusted = recover_quoted_price(dec * scale)
    if unadjusted is None:
        return None, None
    split_adjusted = unadjusted * Decimal(factor.denominator) / Decimal(factor.numerator)
    return split_adjusted, unadjusted


def parse_yahoo_daily_series(
    symbol: str,
    payload: dict[str, Any],
    min_date: date | None = None,
    *,
    now: datetime,
) -> tuple[list[PriceBarRecord], list[PriceBarRecord]]:
    """Decode settled Yahoo bars into `(split_adjusted, unadjusted)` `PriceBarRecord` lists."""
    from data_engine.datahub.market_prices import PriceBarRecord, xnys_session_close_utc

    result = payload.get("chart", {}).get("result")
    if not result or not isinstance(result[0], dict):
        return [], []
    r = result[0]
    timestamps = r.get("timestamp") or []
    quotes = r.get("indicators", {}).get("quote") or []
    if not timestamps or not quotes or not isinstance(quotes[0], dict):
        return [], []
    quote = quotes[0]
    closes = quote.get("close") or []
    if not closes:
        return [], []

    def _at(seq: Any, idx: int) -> Any:
        return seq[idx] if isinstance(seq, list) and idx < len(seq) else None

    splits = _parse_split_events(symbol, payload)
    settled_rows: list[tuple[date, Any, Any, Any, Any, Any]] = []
    for i, ts in enumerate(timestamps[: len(closes)]):
        close_raw = closes[i]
        if close_raw is None:
            continue
        bar_date = datetime.fromtimestamp(ts, tz=UTC).date()
        if now < xnys_session_close_utc(bar_date) or (min_date is not None and bar_date < min_date):
            continue
        settled_rows.append(
            (
                bar_date,
                _at(quote.get("open"), i),
                _at(quote.get("high"), i),
                _at(quote.get("low"), i),
                close_raw,
                _at(quote.get("volume"), i),
            )
        )
    if not settled_rows:
        return [], []
    through = max(item[0] for item in settled_rows)
    split_adjusted_records: list[PriceBarRecord] = []
    unadjusted_records: list[PriceBarRecord] = []
    for bar_date, raw_open, raw_high, raw_low, raw_close, raw_vol in settled_rows:
        factor = split_factor_between(splits, after=bar_date, through=through)
        adj_open, unadj_open = _recovered_pair(raw_open, factor=factor)
        adj_high, unadj_high = _recovered_pair(raw_high, factor=factor)
        adj_low, unadj_low = _recovered_pair(raw_low, factor=factor)
        adj_close, unadj_close = _recovered_pair(raw_close, factor=factor)
        vol = Decimal(int(raw_vol)) if raw_vol is not None else None
        split_adjusted_records.append(
            PriceBarRecord(symbol, bar_date, adj_open, adj_high, adj_low, adj_close, vol, "yahoo", "1D")
        )
        unadjusted_records.append(
            PriceBarRecord(symbol, bar_date, unadj_open, unadj_high, unadj_low, unadj_close, vol, "yahoo", "1D")
        )
    return split_adjusted_records, unadjusted_records
