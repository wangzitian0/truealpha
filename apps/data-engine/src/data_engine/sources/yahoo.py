# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
import json; from datetime import UTC, date, datetime, timedelta; from decimal import Decimal; from typing import Any; import httpx; from data_engine.datahub.fetch_key import FetchKey; from data_engine.sources import gateway; from data_engine.sources.accept_rules import accept_rule; CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"; USER_AGENT = "TrueAlpha research wangzitian.ai@icloud.com"; EXACT_CENT_RECOVERY_BELOW = Decimal(2**16); _CENT = Decimal("0.01")
class PriceBar:
    def __init__( self, day: date, open_: Decimal | None, high: Decimal | None, low: Decimal | None, close: Decimal, adj_close: Decimal | None, volume: int | None, currency: str | None = None, ): self.date = day; self.open = open_; self.high = high; self.low = low; self.close = close; self.adj_close = adj_close; self.volume = volume; self.currency = currency
def recover_quoted_price(value: Decimal | None) -> Decimal | None:
    if value is None: return None
    if abs(value) >= EXACT_CENT_RECOVERY_BELOW: return value
    return value.quantize(_CENT)
def _epoch(d: date) -> int: return int(datetime.combine(d, datetime.min.time(), tzinfo=UTC).timestamp())
def fetch_daily_chart(symbol: str, *, end: date | None = None, period_days: int = 365) -> tuple[bytes, list[PriceBar]]:
    settled_end = end is not None; end = end or date.today(); start = end - timedelta(days=period_days); params = {"period1": str(_epoch(start)), "period2": str(_epoch(end + timedelta(days=1))), "interval": "1d"}; key = FetchKey.for_request( "yahoo", "chart", {"symbol": symbol, "interval": "1d", "period_days": str(period_days), "end": end.isoformat()}, entity_param="symbol", as_of_params=("end",), default_as_of=gateway.utc_day(), )
    def fetch(identity: FetchKey) -> bytes:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client: resp = gateway.http_get( client, "yahoo", "chart", CHART_URL.format(symbol=symbol), identity=identity, caller="yahoo.fetch_daily_chart", params=params, ); resp.raise_for_status(); return bytes(resp.content)
    body = gateway.fetch_once(key, fetch, keep=settled_end); payload = json.loads(body.decode(), parse_float=Decimal); return body, _parse_chart_response(payload)
@accept_rule("yahoo", "chart")
def _chart_settles_its_session(key: FetchKey, body: bytes) -> bool: return _session_is_final(body, date.fromisoformat(key.as_of))
def _session_is_final(body: bytes, end: date) -> bool:
    from data_engine.datahub.market_prices import is_xnys_trading_day, xnys_session_close_utc
    if end > gateway._utcnow().date(): return False
    if not is_xnys_trading_day(end): return True
    if gateway._utcnow() < xnys_session_close_utc(end): return False
    return any(bar.date == end for bar in _parse_chart_response(json.loads(body.decode(), parse_float=Decimal)))
def fetch_daily_bars(symbol: str, *, end: date | None = None, period_days: int = 365) -> list[PriceBar]: return fetch_daily_chart(symbol, end=end, period_days=period_days)[1]
def _chart_currency(meta: Any) -> str | None:
    if not isinstance(meta, dict): return None
    raw_currency = meta.get("currency"); return raw_currency.strip() if isinstance(raw_currency, str) and raw_currency.strip() else None
def _parse_chart_response(payload: dict[str, Any]) -> list[PriceBar]:
    result = payload.get("chart", {}).get("result")
    if not result: return []
    r = result[0]; currency = _chart_currency(r.get("meta")); timestamps = r.get("timestamp") or []
    if not timestamps: return []
    quotes = r.get("indicators", {}).get("quote", [])
    if not quotes: return []
    quote = quotes[0]; closes = quote.get("close")
    if not closes: return []
    adjclose_list = r.get("indicators", {}).get("adjclose", [{}]); adjclose = (adjclose_list[0].get("adjclose") if adjclose_list else None) or closes; bars = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes): break
        c = closes[i]; close = recover_quoted_price(c)
        if close is None: continue
        o = quote.get("open", [])[i] if "open" in quote and i < len(quote["open"]) else None; h = quote.get("high", [])[i] if "high" in quote and i < len(quote["high"]) else None; low = quote.get("low", [])[i] if "low" in quote and i < len(quote["low"]) else None; v = quote.get("volume", [])[i] if "volume" in quote and i < len(quote["volume"]) else None; ac = adjclose[i] if i < len(adjclose) else None; bars.append( PriceBar( day=datetime.fromtimestamp(ts, tz=UTC).date(), open_=recover_quoted_price(o), high=recover_quoted_price(h), low=recover_quoted_price(low), close=close, adj_close=recover_quoted_price(ac), volume=v, currency=currency, ) )
    return bars
