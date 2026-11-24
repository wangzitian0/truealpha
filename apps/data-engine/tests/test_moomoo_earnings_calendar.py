"""The moomoo earnings calendar keeps every estimate of an event (#1791).

The SDK table of `get_earnings_calendar` keeps one EPS pair per event and drops `periodType` and
`currency`. The consensus-growth rule needs both, so the wrapper unpacks the vendor proto itself.
These tests drive the real SDK `pack_req` and a real `Qot_GetEarningsCalendar` response.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
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


def _answer(ret_type: int = 0, ret_msg: str = "", err_code: int | None = None) -> pb.Response:
    response = pb.Response(retType=ret_type, retMsg=ret_msg)
    if err_code is not None:
        response.errCode = err_code
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
    item.estimateList.add(estimateType=pb.EarningsCalendarEstimateType_EBIT, actualValue=0.0, periodType=_QUARTERLY)
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
        {"estimateType": pb.EarningsCalendarEstimateType_EBIT, "actualValue": 0.0, "periodType": _QUARTERLY},
    ], "an estimate the vendor sent is missing, merged or changed (a stated zero is a value)"
    (row,) = call_ledger
    assert (row.endpoint, row.fetch_key, row.ok) == ("get_earnings_calendar", _IDENTITY.ledger_key, True)
    assert row.payload_sha256 == hashlib.sha256(mm.canonical_moomoo_bytes(items)).hexdigest()


def test_a_refused_request_raises_and_is_one_failed_ledger_row(monkeypatch, call_ledger) -> None:
    opend = _OpenD(_answer(ret_type=-1, ret_msg="no permission", err_code=1201), monkeypatch)

    with pytest.raises(mm.MoomooConnectionError, match="retType -1 errCode 1201: no permission"):
        mm.get_earnings_calendar(
            opend, "US", begin_date="2026-11-24", end_date="2026-11-30", caller="test", identity=_IDENTITY
        )

    (row,) = call_ledger
    assert (row.endpoint, row.ok, row.error) == (
        "get_earnings_calendar",
        False,
        "retType -1 errCode 1201: no permission",
    )


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


def test_the_deployed_client_asks_opend_for_the_range_and_the_ledger_names_the_landed_key(
    monkeypatch, call_ledger, landed_keys
) -> None:
    monkeypatch.setattr(gateway, "utc_day", lambda: "2026-11-24")
    opend = _OpenD(_answer(), monkeypatch)

    @contextmanager
    def connect() -> Iterator[_OpenD]:
        yield opend

    monkeypatch.setattr(mm, "connect", connect)

    body = moomoo_origin.capture_earnings_calendar("US", date(2026, 11, 20), date(2026, 11, 26))

    ((_proto_id, request),) = opend.requests
    assert (request.c2s.beginDate, request.c2s.endDate) == ("2026-11-20", "2026-11-26")
    key = moomoo_origin.earnings_calendar_key("US", date(2026, 11, 20), date(2026, 11, 26))
    (row,) = call_ledger
    assert (row.fetch_key, row.caller, row.payload_sha256) == (
        key.ledger_key,
        moomoo_origin.CALLER,
        hashlib.sha256(body).hexdigest(),
    )
    assert landed_keys.lookup(key) == body, "the answer the ledger hashed is not the landed answer"


def test_a_range_of_exactly_seven_days_is_asked() -> None:
    client = _Client()

    moomoo_origin.capture_earnings_calendar("US", date(2026, 11, 24), date(2026, 12, 1), client=client)

    assert [(begin, end) for _market, begin, end, _key in client.calls] == [(date(2026, 11, 24), date(2026, 12, 1))]


@pytest.mark.parametrize(
    ("begin", "end"),
    [(date(2026, 11, 24), date(2026, 12, 2)), (date(2026, 11, 24), date(2026, 11, 23))],
)
def test_a_range_moomoo_refuses_is_refused_before_any_call(begin: date, end: date) -> None:
    client = _Client()

    with pytest.raises(ValueError, match="at most 7 days"):
        moomoo_origin.capture_earnings_calendar("US", begin, end, client=client)

    assert client.calls == []


# Stored staging answers of 2026-11-24, byte-pinned: `raw.fetches` keys
# `get_earnings_calendar:market:US:<begin>:<end>:2026-11-24`, fetched 07:12:48 and 07:42:01 UTC.
_SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "moomoo"
_SAMPLE_SHA256 = {
    "earnings_calendar_US_2026-11-20_2026-11-26.json": "e1abb01685316b7f8498d4ca534c53ca589b34219c82c7d87faf6b8a10ec6b98",
    "earnings_calendar_US_2026-02-02_2026-02-08.json": "4e5a42edabe3084e500cb880fdd2bb124913731b9ddcb80f3e526398120db0cb",
}
_NOVEMBER = "earnings_calendar_US_2026-11-20_2026-11-26.json"
_FEBRUARY = "earnings_calendar_US_2026-02-02_2026-02-08.json"
_SEMI_ANNUAL = pb.EarningsCalendarPeriodType_SemiAnnual
# GAAP diluted EPS of the quarter, from the earnings release (8-K exhibit 99.1) of each company, 2026-11-23.
_GAAP_DILUTED_EPS_OF_THE_RELEASE = {
    "CRWD": 0.02,  # sec.gov/Archives/edgar/data/1535527/000153552726000035/crwd-20261123xex991.htm (non-GAAP 0.34)
    "A": 1.59,  # sec.gov/Archives/edgar/data/0001090872/000109087226000082/exhibit991-q426pressrelease.htm (1.86)
    "HPE": 1.26,  # sec.gov/Archives/edgar/data/1645590/000164559026000097/ex-991x11232026x8k.htm (1.43)
    "ZM": 1.05,  # sec.gov/Archives/edgar/data/0001585521/000158552126000170/zm-20261031ex991.htm (1.59)
}


def _sample_items(name: str) -> list[dict[str, Any]]:
    body = (_SAMPLES / name).read_bytes()
    assert hashlib.sha256(body).hexdigest() == _SAMPLE_SHA256[name], f"the captured answer {name} changed"
    return json.loads(body)


def _eps_by_code(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {item["security"]["code"]: [e for e in item["estimateList"] if e["estimateType"] == _EPS] for item in items}


@pytest.mark.parametrize(
    ("name", "events", "fiscal_q4", "eps_periods"),
    [
        (_NOVEMBER, 91, 20, {_QUARTERLY: 69, _SEMI_ANNUAL: 1, None: 21}),
        (_FEBRUARY, 474, 272, {_QUARTERLY: 445, None: 29}),
    ],
)
def test_a_captured_week_has_no_annual_eps_estimate(
    name: str, events: int, fiscal_q4: int, eps_periods: dict[int | None, int]
) -> None:
    """#1791: each report has one EPS estimate, of its own quarter or empty; no report has an annual one.

    The February week holds the fiscal-year-end reports of most calendar-year filers.
    """
    items = _sample_items(name)
    eps = _eps_by_code(items)

    assert len(items) == events and all(len(estimates) == 1 for estimates in eps.values())
    assert sum(item["periodText"].endswith("Q4") for item in items) == fiscal_q4
    assert Counter(e.get("periodType") for (e,) in eps.values()) == Counter(eps_periods)


def test_fiscal_year_end_reports_carry_a_quarterly_eps_estimate() -> None:
    eps = {**_eps_by_code(_sample_items(_FEBRUARY)), **_eps_by_code(_sample_items(_NOVEMBER))}

    assert {
        code: eps[code][0]["periodType"] for code in ("GOOGL", "AMZN", "LLY", "HPQ", "ADI", "DE", "A")
    } == dict.fromkeys(("GOOGL", "AMZN", "LLY", "HPQ", "ADI", "DE", "A"), _QUARTERLY)


def test_the_reported_eps_of_the_calendar_is_the_gaap_eps_of_the_company_release() -> None:
    """The vendor labels the EPS type GAAP; four reported actuals match the GAAP line of the release.

    The basis of the estimates is not proved: ABBV in February has an estimate of 2.8741 against 1.02 reported.
    """
    eps = _eps_by_code(_sample_items(_NOVEMBER))

    assert {code: eps[code][0]["actualValue"] for code in _GAAP_DILUTED_EPS_OF_THE_RELEASE} == (
        _GAAP_DILUTED_EPS_OF_THE_RELEASE
    )


def test_an_actual_value_can_precede_the_release() -> None:
    """DKS and NXGL release at 14:30 UTC on 2026-11-24; the answer fetched at 07:12:48 UTC already has an actual.

    A rule that reads `actualValue` as the result of the event must check the release instant first.
    """
    fetched_at = datetime(2026, 11, 24, 7, 12, 48, tzinfo=UTC).timestamp()
    items = _sample_items(_NOVEMBER)

    early = sorted(
        item["security"]["code"]
        for item in items
        if item["earningsTimestamp"] > fetched_at
        and any(e["estimateType"] == _EPS and "actualValue" in e for e in item["estimateList"])
    )
    assert early == ["DKS", "NXGL"]
