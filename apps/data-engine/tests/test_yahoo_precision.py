"""Yahoo parse-boundary coverage: float32 recovery and the cutoff window (#490).

The payloads here are real bytes off the wire, not invented ones. That matters: the
premise on #490 was that Yahoo sends `353.21` and Python's parser mangles it. Inspecting
the response shows the opposite — Yahoo sends `326.5899963378906` itself — and a fixture
written from the assumption would have encoded the wrong problem and "passed".
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.sources.yahoo import (
    EXACT_CENT_RECOVERY_BELOW,
    _parse_chart_response,
    recover_quoted_price,
)

# Verbatim from query1.finance.yahoo.com/v8/finance/chart/AAPL — the float32 artifact is
# in the vendor's own bytes.
_WIRE_CLOSES = "[326.5899963378906,327.739990234375,325.8900146484375,321.6600036621094,333.0199890136719]"


def _chart(closes: str, timestamps: list[int]) -> dict:
    body = f"""
    {{"chart": {{"result": [{{"timestamp": {timestamps},
      "indicators": {{"quote": [{{"open": {closes}, "high": {closes}, "low": {closes},
      "close": {closes}, "volume": [1,2,3,4,5]}}]}}}}]}}}}
    """
    return json.loads(body, parse_float=Decimal)


def test_the_vendor_sends_float32_artifacts_not_clean_prices() -> None:
    """Guards the premise the rest of this module rests on."""
    values = json.loads(_WIRE_CLOSES, parse_float=Decimal)
    assert values[0] == Decimal("326.5899963378906")
    assert values[0] != Decimal("326.59")


def test_recovery_returns_the_price_the_exchange_quoted() -> None:
    assert recover_quoted_price(Decimal("326.5899963378906")) == Decimal("326.59")
    assert recover_quoted_price(Decimal("333.0199890136719")) == Decimal("333.02")
    assert recover_quoted_price(Decimal("1014.9600219726562")) == Decimal("1014.96")


def test_recovery_is_exact_because_float32_spacing_stays_under_half_a_cent() -> None:
    """Round-trip every recovered price back through float32: it must land on the same
    bits the vendor sent. That is what makes this recovery rather than rounding."""
    import struct

    for raw in json.loads(_WIRE_CLOSES, parse_float=Decimal):
        recovered = recover_quoted_price(raw)
        assert recovered is not None
        round_tripped = struct.unpack("f", struct.pack("f", float(recovered)))[0]
        assert Decimal(repr(round_tripped)) == raw


def test_a_price_too_large_to_recover_is_left_alone() -> None:
    """Above 2**16 the float32 gap exceeds half a cent, so no 2-decimal figure is
    provably the original — BRK.A territory. Better an honest artifact than an invented
    price."""
    big = EXACT_CENT_RECOVERY_BELOW + Decimal("0.0625")
    assert recover_quoted_price(big) == big


def test_no_bar_field_is_ever_a_binary_float() -> None:
    bars = _parse_chart_response(_chart(_WIRE_CLOSES, [1750000000, 1750086400, 1750172800, 1750259200, 1750345600]))
    assert bars
    for bar in bars:
        for field in (bar.open, bar.high, bar.low, bar.close, bar.adj_close):
            assert not isinstance(field, float)
            assert isinstance(field, Decimal)


def test_null_closes_are_still_skipped() -> None:
    payload = _chart("[326.5899963378906,null]", [1750000000, 1750086400])
    bars = _parse_chart_response(payload)
    assert len(bars) == 1
    assert bars[0].close == Decimal("326.59")


def test_the_requested_window_is_anchored_on_the_caller_cutoff(monkeypatch) -> None:
    """A cutoff two years back must produce a request around THAT date. Anchoring on the
    wall clock returns bars that are all newer than the cutoff, and the adapter then
    reports FIELD_UNAVAILABLE instead of a price that exists."""
    from data_engine.sources import yahoo

    seen: dict[str, str] = {}

    class _Response:
        text = '{"chart": {"result": []}}'

        def raise_for_status(self) -> None: ...

    class _Client:
        def __init__(self, **_: object) -> None: ...
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None: ...
        def get(self, _url: str, params: dict[str, str]) -> _Response:
            seen.update(params)
            return _Response()

    monkeypatch.setattr(yahoo.httpx, "Client", _Client)
    cutoff = date(2024, 3, 15)
    yahoo.fetch_daily_bars("AAPL", end=cutoff)
    requested_end = datetime.fromtimestamp(int(seen["period2"]), tz=UTC).date()
    assert requested_end == date(2024, 3, 16)  # cutoff + 1 day, exclusive upper bound
