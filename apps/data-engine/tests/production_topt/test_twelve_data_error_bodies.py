"""Twelve Data error bodies are classified; only "no data" reads as absent (#885).

A revoked key answered `/eod` with a 401 error body, the parser's error-body branch read
it as "no end of day", the fallback asked `/time_series`, got the same 401, found no rows,
and the cell went single-origin with nothing but an `ok = false` ledger row to say why.
These tests drive the deployed fetcher through `gateway.urlopen` with the vendor's own
bytes (the cassettes below) and assert each error is raised, named and counted.

The cassettes are verbatim vendor answers:

- ``twelvedata_eod_401_invalid_key`` and ``twelvedata_time_series_400_no_data``: captured
  2026-09-16 from the live API (an invalid key; the public ``demo`` key on a Sunday).
- ``twelvedata_eod_400_no_data``: captured the same way; production's
  ``staging.api_call_ledger`` holds four rows with exactly this ``payload_sha256``.
- ``twelvedata_eod_429_minute``: the body production's ledger recorded on 2026-09-07
  23:48:33Z (``payload_sha256`` 1182e7e0…, ``byte_length`` 257), when both environments'
  canaries spent the shared key's minute together; the digest proves the bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import urllib.error
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.datahub.production_topt import twelve_data_origin as origin_module
from data_engine.datahub.production_topt.corroboration_audit import corroboration_tally
from data_engine.datahub.production_topt.market_price_adapter import MarketPriceQuote
from data_engine.datahub.production_topt.twelve_data_origin import (
    TwelveDataAuthError,
    TwelveDataCreditsExhausted,
    TwelveDataError,
    TwelveDataQuoteFetcher,
    TwelveDataRateLimited,
    TwelveDataVendorError,
    classify_error_body,
)
from data_engine.sources import gateway

_CASSETTES = Path(__file__).parent / "cassettes"
_PARTITION = date(2026, 9, 13)  # a Sunday: no end of day of its own

# (file, sha256, byte_length)
_AUTH = (
    "twelvedata_eod_401_invalid_key.dbf723dd.json",
    "dbf723dd78a4eeb146bbd08f0480ab3d7036579f56e732243a5f77708ea9bc15",
    291,
)
_NO_EOD = (
    "twelvedata_eod_400_no_data.7de9b97d.json",
    "7de9b97df3902d3f75e0d1cd75dd2a5026fa5fb9a98ec919b023dc71f0d01007",
    170,
)
_NO_SERIES = (
    "twelvedata_time_series_400_no_data.c00c7c22.json",
    "c00c7c224278df23bbadfb2757e80a622645586431cdb4bcfecedc1028907d07",
    182,
)
_MINUTE = (
    "twelvedata_eod_429_minute.1182e7e0.json",
    "1182e7e0b0345da7eb1e519d6626feb118f208d31f8c45c8b151f22f888dec1c",
    257,
)

_SETTLED_SERIES = json.dumps(
    {
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "values": [
            {"datetime": "2026-09-11", "open": "330", "high": "333", "low": "329", "close": "332.26999"},
        ],
        "status": "ok",
    }
).encode()
_SETTLED_EOD = json.dumps({"symbol": "AAPL", "datetime": "2026-09-11", "close": "332.26999"}).encode()


def _bytes(cassette: tuple[str, str, int]) -> bytes:
    name, sha, length = cassette
    body = (_CASSETTES / name).read_bytes()
    assert len(body) == length and hashlib.sha256(body).hexdigest() == sha, f"{name} is not the vendor's bytes"
    return body


@pytest.fixture
def vendor(monkeypatch):
    """Queued (status, body) answers behind `urllib.request.urlopen`; an error status is
    raised as `HTTPError`, the way the live API answers."""
    answers: list[tuple[int, bytes]] = []
    asked: list[str] = []

    class _Response(io.BytesIO):
        status = 200

    def fake_urlopen(url, timeout=None):
        asked.append(url)
        if not answers:
            raise AssertionError(f"unexpected extra Twelve Data request: {url}")
        status, body = answers.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "error", None, io.BytesIO(body))
        return _Response(body)

    monkeypatch.setattr(origin_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(origin_module.time, "sleep", lambda _seconds: None)
    return answers, asked


def test_the_error_cassettes_are_the_bytes_the_vendor_sent() -> None:
    for cassette in (_AUTH, _NO_EOD, _NO_SERIES, _MINUTE):
        _bytes(cassette)


def test_a_revoked_key_is_raised_and_counted_not_read_as_no_end_of_day(vendor, call_ledger, caplog) -> None:
    """Red on the pre-#885 fetcher: the 401 read as "no end of day", the fallback asked
    again, and the tally stayed at zero."""
    answers, asked = vendor
    answers.extend([(401, _bytes(_AUTH)), (401, _bytes(_AUTH))])
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        assert TwelveDataQuoteFetcher("revoked", throttle_seconds=0)("AAPL", _PARTITION) is None

    assert len(asked) == 1, "a key the vendor refused is not asked a second question"
    assert tally.summary() == "corroborations refused 1 (twelve-data fetch 1)"
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "TwelveDataAuthError" in warning and "**apikey** parameter is incorrect" in warning
    (row,) = call_ledger
    assert (row.source, row.endpoint, row.ok, row.status_code) == ("twelvedata", "eod", False, 401)


def test_a_spent_minute_is_raised_as_rate_limited(vendor, caplog) -> None:
    answers, asked = vendor
    answers.append((429, _bytes(_MINUTE)))
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        assert TwelveDataQuoteFetcher("k", throttle_seconds=0)("MSFT", _PARTITION) is None
    assert len(asked) == 1 and tally.total == 1
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "TwelveDataRateLimited" in warning and "current minute" in warning


def test_the_vendors_no_data_answer_still_falls_back_to_the_settled_session(vendor) -> None:
    """The one error body that IS an honest absence keeps its #557 path, with the bytes
    production actually receives."""
    answers, asked = vendor
    answers.extend([(400, _bytes(_NO_EOD)), (200, _SETTLED_SERIES)])
    with corroboration_tally() as tally:
        quote = TwelveDataQuoteFetcher("k", throttle_seconds=0)("AAPL", _PARTITION)
    assert quote is not None and quote.as_of == date(2026, 9, 11) and quote.close == Decimal("332.26999")
    assert len(asked) == 2 and tally.total == 0


def test_a_series_with_no_data_either_is_absent_and_not_a_loss(vendor) -> None:
    answers, _asked = vendor
    answers.extend([(400, _bytes(_NO_EOD)), (400, _bytes(_NO_SERIES))])
    with corroboration_tally() as tally:
        assert TwelveDataQuoteFetcher("k", throttle_seconds=0)("AAPL", _PARTITION) is None
    assert tally.total == 0


def test_a_bar_request_that_errors_keeps_the_settled_close_and_says_so(vendor, caplog) -> None:
    """v3's bar is additive: a spent minute on the series request leaves the settled
    close corroborated — and is now a warning rather than a silently empty bar."""
    answers, _asked = vendor
    answers.extend([(200, _SETTLED_EOD), (429, _bytes(_MINUTE))])
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        quote = TwelveDataQuoteFetcher("k", throttle_seconds=0)("AAPL", date(2026, 9, 11))
    assert quote is not None and quote.close == Decimal("332.26999") and quote.open is None
    assert quote.raw_bytes == _SETTLED_EOD and tally.total == 0
    assert any("bar for AAPL not attached (TwelveDataRateLimited" in r.getMessage() for r in caplog.records)


_DAILY = json.dumps(  # synthetic: no daily exhaustion has been recorded; the vendor's documented wording
    {
        "code": 429,
        "message": "You have run out of API credits for the day. 801 API credits were used, with the current "
        "limit being 800. Wait for the next day or consider switching to a higher tier plan at "
        "https://twelvedata.com/pricing",
        "status": "error",
    }
).encode()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, _bytes(_AUTH), TwelveDataAuthError),
        (200, _bytes(_AUTH), TwelveDataAuthError),  # the body's code wins over a 200 wrapper
        (403, b'{"code":403,"message":"not available with your plan","status":"error"}', TwelveDataAuthError),
        (429, _bytes(_MINUTE), TwelveDataRateLimited),
        (429, _DAILY, TwelveDataCreditsExhausted),
        (404, b'{"code":404,"message":"**symbol** not found: ZZZZ","status":"error"}', TwelveDataVendorError),
        (400, b'{"code":400,"message":"**date** parameter is incorrect","status":"error"}', TwelveDataVendorError),
        (502, b"<html>bad gateway</html>", TwelveDataVendorError),
        (400, _bytes(_NO_EOD), None),
        (400, _bytes(_NO_SERIES), None),
        (200, _SETTLED_EOD, None),
        (None, _SETTLED_SERIES, None),
        (200, b"not json", None),  # a 200 the parser refuses on its own terms
    ],
    ids=[
        "401-invalid-key",
        "401-inside-a-200",
        "403-plan",
        "429-minute",
        "429-day",
        "404-unknown-symbol",
        "400-bad-parameter",
        "502-not-json",
        "400-eod-no-data",
        "400-series-no-data",
        "200-settled-eod",
        "series-ok",
        "200-not-json",
    ],
)
def test_every_error_body_is_classified(status, body, expected) -> None:
    error = classify_error_body(status, body)
    if expected is None:
        assert error is None
    else:
        assert type(error) is expected and isinstance(error, TwelveDataError)
        assert error.code == (json.loads(body)["code"] if body.startswith(b"{") else status)
    if expected is TwelveDataCreditsExhausted:
        assert isinstance(error, TwelveDataRateLimited), "a spent day is still a rate limit"


def test_the_parsers_raise_a_classified_body_they_are_handed() -> None:
    with pytest.raises(TwelveDataAuthError):
        origin_module.parse_session_close(_bytes(_AUTH), partition=_PARTITION)
    with pytest.raises(TwelveDataRateLimited):
        origin_module.parse_last_settled_close(_bytes(_MINUTE), partition=_PARTITION)
    assert origin_module.parse_session_close(_bytes(_NO_EOD), partition=_PARTITION) is None
    assert origin_module.parse_last_settled_close(_bytes(_NO_SERIES), partition=_PARTITION) is None


# -- a request the rule-6 gate refused was never sent (#729) -----------------------------------


def test_a_request_the_gate_refused_costs_no_throttle_wait(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(origin_module.time, "sleep", slept.append)

    class Refused(TwelveDataQuoteFetcher):
        def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
            raise gateway.CapacityExceeded("twelvedata", "daily budget 480 spent (480 calls today)")

    with corroboration_tally() as tally:
        assert Refused("k", throttle_seconds=8)("AAPL", _PARTITION) is None
    assert slept == [] and tally.total == 1


def test_a_refused_bar_request_keeps_the_settled_close(vendor, monkeypatch) -> None:
    answers, asked = vendor
    answers.append((200, _SETTLED_EOD))
    fetcher = TwelveDataQuoteFetcher("k", throttle_seconds=0)
    real_get = fetcher._get

    def gated_get(url: str, params: dict[str, str]) -> bytes:
        if url.endswith("/time_series"):
            raise gateway.CapacityExceeded("twelvedata", "daily budget 480 spent (480 calls today)")
        return real_get(url, params)

    monkeypatch.setattr(fetcher, "_get", gated_get)
    quote = fetcher("AAPL", date(2026, 9, 11))
    assert quote is not None and quote.close == Decimal("332.26999") and len(asked) == 1
