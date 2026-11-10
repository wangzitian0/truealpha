# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; from datetime import UTC, date, datetime; from decimal import Decimal; from data_engine.sources.yahoo import ( EXACT_CENT_RECOVERY_BELOW, _parse_chart_response, recover_quoted_price, ); _WIRE_CLOSES = "[326.5899963378906,327.739990234375,325.8900146484375,321.6600036621094,333.0199890136719]"
def _chart(closes: str, timestamps: list[int]) -> dict: body = f'\n    {{"chart": {{"result": [{{"timestamp": {timestamps},\n      "indicators": {{"quote": [{{"open": {closes}, "high": {closes}, "low": {closes},\n      "close": {closes}, "volume": [1,2,3,4,5]}}]}}}}]}}}}\n    '; return json.loads(body, parse_float=Decimal)
def test_the_vendor_sends_float32_artifacts_not_clean_prices() -> None: values = json.loads(_WIRE_CLOSES, parse_float=Decimal); assert values[0] == Decimal("326.5899963378906"); assert values[0] != Decimal("326.59")
def test_recovery_returns_the_price_the_exchange_quoted() -> None: assert recover_quoted_price(Decimal("326.5899963378906")) == Decimal("326.59"); assert recover_quoted_price(Decimal("333.0199890136719")) == Decimal("333.02"); assert recover_quoted_price(Decimal("1014.9600219726562")) == Decimal("1014.96")
def test_recovery_is_exact_because_float32_spacing_stays_under_half_a_cent() -> None:
    import struct
    for raw in json.loads(_WIRE_CLOSES, parse_float=Decimal): recovered = recover_quoted_price(raw); assert recovered is not None; round_tripped = struct.unpack("f", struct.pack("f", float(recovered)))[0]; assert Decimal(repr(round_tripped)) == raw
def test_a_price_too_large_to_recover_is_left_alone() -> None: big = EXACT_CENT_RECOVERY_BELOW + Decimal("0.0625"); assert recover_quoted_price(big) == big
def test_no_bar_field_is_ever_a_binary_float() -> None:
    bars = _parse_chart_response(_chart(_WIRE_CLOSES, [1750000000, 1750086400, 1750172800, 1750259200, 1750345600])); assert bars
    for bar in bars:
        for field in (bar.open, bar.high, bar.low, bar.close, bar.adj_close): assert not isinstance(field, float); assert isinstance(field, Decimal)
def test_null_closes_are_still_skipped() -> None: payload = _chart("[326.5899963378906,null]", [1750000000, 1750086400]); bars = _parse_chart_response(payload); assert len(bars) == 1; assert bars[0].close == Decimal("326.59")
def test_the_requested_window_is_anchored_on_the_caller_cutoff(monkeypatch) -> None:
    from data_engine.sources import yahoo; seen: dict[str, str] = {}
    class _Response:
        content = b'{"chart": {"result": []}}'; status_code = 200
        def raise_for_status(self) -> None: ...
    class _Client:
        def __init__(self, **_: object) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *_: object) -> None: ...
        def get(self, _url: str, params: dict[str, str]) -> _Response: seen.update(params); return _Response()
    monkeypatch.setattr(yahoo.httpx, "Client", _Client); cutoff = date(2024, 3, 15); yahoo.fetch_daily_bars("AAPL", end=cutoff); requested_end = datetime.fromtimestamp(int(seen["period2"]), tz=UTC).date(); assert requested_end == date(2024, 3, 16)
def test_empty_chart_response_returns_empty_bars() -> None: assert _parse_chart_response({"chart": {"result": None}}) == []; assert _parse_chart_response({"chart": {"result": []}}) == []
def test_missing_or_empty_timestamps_returns_empty_bars() -> None: assert _parse_chart_response({"chart": {"result": [{}]}}) == []; assert _parse_chart_response({"chart": {"result": [{"timestamp": []}]}}) == []
def test_missing_quotes_or_empty_close_returns_empty_bars() -> None: payload_empty_quotes = { "chart": { "result": [ { "timestamp": [1750000000], "indicators": {"quote": []}, } ] } }; assert _parse_chart_response(payload_empty_quotes) == []; payload_empty_dict = { "chart": { "result": [ { "timestamp": [1750000000], "indicators": { "quote": [{}], "adjclose": [{}], }, } ] } }; assert _parse_chart_response(payload_empty_dict) == []; payload_none_close = { "chart": { "result": [ { "timestamp": [1750000000], "indicators": { "quote": [{"close": None}], }, } ] } }; assert _parse_chart_response(payload_none_close) == []
