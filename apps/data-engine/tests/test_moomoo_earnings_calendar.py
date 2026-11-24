"""The moomoo earnings calendar keeps every estimate of an event (#1791).

The SDK table of `get_earnings_calendar` keeps one EPS pair per event and drops `periodType` and
`currency`. The consensus-growth rule needs both, so the wrapper unpacks the vendor proto itself.
These tests drive the real SDK `pack_req` and a real `Qot_GetEarningsCalendar` response.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from data_engine.datahub.capture_core import moomoo_origin
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources import moomoo as mm
from moomoo.common.constant import ProtoId
from moomoo.common.pb import Qot_GetEarningsCalendar_pb2 as pb
from moomoo.quote import quote_query

pytestmark = pytest.mark.usefixtures("idle_moomoo_gate")

_US_MARKET = 11
_EPS, _REVENUE = pb.EarningsCalendarEstimateType_EPS, pb.EarningsCalendarEstimateType_Revenue
_QUARTERLY, _ANNUAL = pb.EarningsCalendarPeriodType_Quarterly, pb.EarningsCalendarPeriodType_Annual
_IDENTITY = FetchKey("moomoo", "get_earnings_calendar", "market:US:2026-11-24:2026-11-30", "2026-11-24")


def _answer(ret_type: int = 0, ret_msg: str = "") -> pb.Response:
    response = pb.Response(retType=ret_type, retMsg=ret_msg)
    if ret_type != 0:
        return response
    item = response.s2c.itemList.add()
    item.security.market = _US_MARKET
    item.security.code = "DELL"
    item.earningsDate = "2026-11-25"
    item.periodText = "2027Q3"
    item.estimateList.add(estimateType=_EPS, predictValue=2.47, currency="USD", periodType=_QUARTERLY)
    item.estimateList.add(estimateType=_EPS, actualValue=6.38, predictValue=7.95, currency="USD", periodType=_ANNUAL)
    item.estimateList.add(estimateType=_REVENUE, predictValue=2.7e10, currency="USD", periodType=_QUARTERLY)
    return response


class _OpenD:
    """A quote context that packs each request with the real SDK and answers one scripted proto."""

    def __init__(self, response: pb.Response, monkeypatch: pytest.MonkeyPatch) -> None:
        self.response = response
        self.requests: list[tuple[int, pb.Request]] = []
        monkeypatch.setattr(
            quote_query, "pack_pb_req", lambda req, proto_id, conn_id: (0, "", req, proto_id, 1), raising=True
        )

    def get_sync_conn_id(self) -> int:
        return 7

    def _get_sync_query_processor(self, pack: Callable[..., Any], unpack: Callable[[pb.Response], Any]):
        def query(**kwargs: Any):
            ret, msg, request, proto_id, _serial = pack(**kwargs)
            assert ret == 0, msg
            self.requests.append((proto_id, request))
            return unpack(self.response)

        return query


def test_the_wrapper_keeps_the_period_and_currency_of_every_estimate(monkeypatch, call_ledger) -> None:
    opend = _OpenD(_answer(), monkeypatch)

    items = mm.get_earnings_calendar(
        opend, "US", begin_date="2026-11-24", end_date="2026-11-30", caller="test", identity=_IDENTITY
    )

    ((proto_id, request),) = opend.requests
    assert proto_id == ProtoId.Qot_GetEarningsCalendar
    assert (request.c2s.market, request.c2s.beginDate, request.c2s.endDate) == (_US_MARKET, "2026-11-24", "2026-11-30")
    (item,) = items
    assert (item["security"], item["periodText"]) == ({"market": _US_MARKET, "code": "DELL"}, "2027Q3")
    assert item["estimateList"] == [
        {"estimateType": _EPS, "predictValue": 2.47, "currency": "USD", "periodType": _QUARTERLY},
        {"estimateType": _EPS, "actualValue": 6.38, "predictValue": 7.95, "currency": "USD", "periodType": _ANNUAL},
        {"estimateType": _REVENUE, "predictValue": 2.7e10, "currency": "USD", "periodType": _QUARTERLY},
    ], "an estimate the vendor sent is missing, merged or changed"
    (row,) = call_ledger
    assert (row.endpoint, row.fetch_key, row.ok) == ("get_earnings_calendar", _IDENTITY.ledger_key, True)
    assert row.payload_sha256 == hashlib.sha256(mm.canonical_moomoo_bytes(items)).hexdigest()


def test_a_refused_request_raises_and_is_one_failed_ledger_row(monkeypatch, call_ledger) -> None:
    opend = _OpenD(_answer(ret_type=-1, ret_msg="no permission"), monkeypatch)

    with pytest.raises(mm.MoomooConnectionError, match="no permission"):
        mm.get_earnings_calendar(
            opend, "US", begin_date="2026-11-24", end_date="2026-11-30", caller="test", identity=_IDENTITY
        )

    (row,) = call_ledger
    assert (row.endpoint, row.ok, row.error) == ("get_earnings_calendar", False, "no permission")


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date, FetchKey]] = []

    def earnings_calendar(self, market: str, *, begin: date, end: date, identity: FetchKey) -> list[dict[str, Any]]:
        self.calls.append((market, begin, end, identity))
        return [{"security": {"market": _US_MARKET, "code": "DELL"}, "estimateList": []}]


def test_one_capture_per_utc_day_lands_the_answer_and_a_second_capture_reads_it(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "utc_day", lambda: "2026-11-24")
    client = _Client()

    first = moomoo_origin.capture_earnings_calendar("US", date(2026, 11, 24), date(2026, 11, 30), client=client)
    second = moomoo_origin.capture_earnings_calendar("US", date(2026, 11, 24), date(2026, 11, 30), client=client)

    assert len(client.calls) == 1, "a landed answer of the same day was fetched again"
    assert client.calls[0][3] == _IDENTITY
    assert first == second
    assert json.loads(first) == [{"estimateList": [], "security": {"code": "DELL", "market": _US_MARKET}}]


@pytest.mark.parametrize(
    ("begin", "end"),
    [(date(2026, 11, 24), date(2026, 12, 2)), (date(2026, 11, 24), date(2026, 11, 23))],
)
def test_a_range_moomoo_refuses_is_refused_before_any_call(begin: date, end: date) -> None:
    client = _Client()

    with pytest.raises(ValueError, match="at most 7 days"):
        moomoo_origin.capture_earnings_calendar("US", begin, end, client=client)

    assert client.calls == []
