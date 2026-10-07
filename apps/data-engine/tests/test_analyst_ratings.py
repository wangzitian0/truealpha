"""Unit tests for analyst_ratings producer, materializer, and universe runner (#771)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Collection, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dagster as dg
import moomoo
import pandas as pd
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.analyst_ratings import (
    FetchFailure,
    UniverseCapture,
    capture_ticker_analyst_ratings,
    materialize_analyst_ratings,
    materialize_universe_analyst_ratings,
)
from data_engine.datahub.canonical_issuer import CanonicalIssuer, CanonicalUniverse
from data_engine.datahub.question_coverage import stored_report_run as _STORED_REPORT_RUN
from data_engine.sources import moomoo as mm
from data_engine.sources import moomoo_ledger as ledger
from factors.base.analyst_track_record import (
    AnalystRatingItem,
    analyst_track_record,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "moomoo"


def _issuer_id(name: str) -> str:
    """The wide row's issuer id for a test name: a lower-case UUID. A writer refuses any other (#1079)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://truealpha.invalid/test/issuer/{name}"))


DDOG, NICE, SHOP, DUOL = (_issuer_id(name) for name in ("ddog", "nice", "shop", "duol"))


def _identity_universe(
    _connection: Any, tickers: Mapping[str, str], *, cutoff: datetime, as_of: date, wide_row_ids: Collection[str]
) -> CanonicalUniverse:
    """A universe whose corpus ids already are the wide row's. `test_canonical_issuer_id.py` tests the mapping."""
    return CanonicalUniverse(
        issuers=tuple(CanonicalIssuer(issuer_id=i, legacy_id=i, ticker=t) for i, t in tickers.items())
    )


@pytest.fixture(autouse=True)
def _isolated_moomoo_ledger(tmp_path, monkeypatch):
    """Run the real `_call` wrapper against a throwaway JSON ledger, never the staging table."""
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "json")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 1000)
    ledger._recent_calls.clear()
    # The real pacing sleeps for the rest of a 30 s window once the burst cap is spent. A test
    # over 25 tickers would run for a minute; the pacing is not what these tests assert.
    monkeypatch.setattr(mm, "throttle", lambda: None)


def _sample(ticker: str) -> dict[str, Any]:
    """The `get_research_analyst_consensus` payload captured from the real OpenD gateway."""
    captured = json.loads((SAMPLES / f"{ticker}.json").read_text())["analyst_consensus"]
    assert captured["ok"] is True
    return captured["data"]


class _FakeQuoteContext:
    """Answers like the SDK: `(RET_OK, payload)` per code, `(RET_ERROR, message)` for a str.

    A payload that is an `Exception` is raised, as a socket timeout would be. A list, a frame
    and None are returned as they are: the first stands for a payload that breaks the vendor
    contract, the other two for an empty answer.
    """

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.codes: list[str] = []

    def get_research_analyst_consensus(self, code: str):
        self.codes.append(code)
        response = self.responses[code]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return moomoo.RET_ERROR, response
        if response is None or isinstance(response, (list, pd.DataFrame)):
            return moomoo.RET_OK, response
        return moomoo.RET_OK, dict(response)


class _MockCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self


class _MockConnection:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _MockCursor()


def test_materialize_analyst_ratings_handles_dict_and_factor_record() -> None:
    """Consolidated: tests both raw dict items and AnalystTrackRecord factor items."""
    conn = _MockConnection()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    ratings = [
        AnalystRatingItem("analyst:1", 5, confidence=Decimal("0.9")),
        AnalystRatingItem("analyst:2", 4, confidence=Decimal("0.8")),
    ]
    rec = analyst_track_record(ratings, entity_id=_issuer_id("msft"), as_of=now)
    count = materialize_analyst_ratings(
        conn,
        run_id="run:both",
        cutoff=now,
        ratings_data=[
            {
                "issuer_id": _issuer_id("aapl"),
                "consensus_rating": Decimal("4.5"),
                "analysts_count": 32,
                "availability_status": "available",
                "reason_codes": [],
            },
            rec,
        ],
    )
    assert count == 2
    assert len(conn.executed) == 2
    # Row 1: dict
    sql1, params1 = conn.executed[0]
    assert "insert into mart.issuer_analyst_ratings" in sql1
    assert params1[1] == _issuer_id("aapl")
    assert params1[2] == now, "stamped with the given cutoff, not the clock"
    assert params1[3] == Decimal("4.5")
    assert params1[11] == "available"
    # Row 2: factor record
    _, params2 = conn.executed[1]
    assert params2[2] == now
    assert params2[1] == _issuer_id("msft")
    assert params2[3] == Decimal("4.5")
    assert params2[4] == 2
    assert params2[11] == "available"


@pytest.mark.parametrize(
    "legacy_id",
    ["issuer:lei:AAAAAAAAAAAAAAAAAA01", "issuer:cik:0000320193", "", "NOT-A-UUID", str(uuid.uuid4()).upper()],
    ids=["lei", "cik", "empty", "not-a-uuid", "upper-case-uuid"],
)
def test_a_row_is_never_written_under_a_legacy_id(legacy_id: str) -> None:
    """#1079: the legacy id of the corpus is not the wide row's id, so no row may carry it."""
    conn = _MockConnection()
    with pytest.raises(ValueError, match="not the wide row's id"):
        materialize_analyst_ratings(
            conn,
            run_id="run:legacy",
            cutoff=datetime(2026, 9, 25, tzinfo=UTC),
            ratings_data=[
                {"issuer_id": legacy_id, "consensus_rating": Decimal("4"), "availability_status": "available"}
            ],
        )
    assert conn.executed == [], "nothing reached the table"


def test_the_wrapper_hands_back_the_payload_without_the_return_code() -> None:
    """The root cause of #771: `_call` returns the payload alone, not `(ret, data)`.

    The capture code unpacked `ret, df = get_analyst_consensus(...)`. The real payload is a
    dict of ten entries, so the unpack raised `ValueError` after the ledger had recorded the
    call as ok.
    """
    payload = mm.get_analyst_consensus(_FakeQuoteContext({"US.DDOG": _sample("DDOG")}), "US.DDOG", caller="test")
    assert isinstance(payload, dict)
    assert payload["total"] == 35
    assert ledger.calls_this_month() == 1


@pytest.mark.parametrize(
    ("ticker", "rating", "total", "buy", "hold", "sell"),
    [
        ("DDOG", 4, 35, 34, 0, 1),
        ("NICE", 4, 13, 9, 4, 0),
        ("SHOP", 4, 23, 21, 2, 0),
        ("DUOL", 3, 7, 2, 4, 1),
    ],
)
def test_a_real_consensus_response_becomes_an_available_row(ticker, rating, total, buy, hold, sell) -> None:
    conn = _MockConnection()
    ctx = _FakeQuoteContext({f"US.{ticker}": _sample(ticker)})
    res = capture_ticker_analyst_ratings(
        ctx=ctx,
        ticker=ticker,
        company_id=_issuer_id(ticker.lower()),
        connection=conn,
        run_id="run:test",
    )
    assert res.rows == 1
    assert res.failure is None
    assert ctx.codes == [f"US.{ticker}"]
    assert ledger.calls_this_month() == 1
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    assert params[0] == "run:test"
    assert params[1] == _issuer_id(ticker.lower())
    assert params[9] == []
    assert params[11] == "available"
    assert params[12] == "verified"
    assert params[3] == Decimal(rating)
    assert params[4] == total
    assert (params[5], params[6], params[7]) == (buy, hold, sell)
    assert buy + hold + sell == total


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        pd.DataFrame(),
        {"rating": 0, "total": 0},
        {"rating": 0, "total": 12},
        {"rating": 4, "total": 0},
        {"rating": 7, "total": 0},
        {"total": 0},
        {"total": 0, "buy": 10.0},
        {"rating": None, "total": 0},
    ],
    ids=[
        "empty-dict",
        "none",
        "empty-frame",
        "unknown-rating-no-analysts",
        "unknown-rating",
        "no-analysts",
        "no-analysts-any-rating",
        "no-analysts-no-rating",
        "no-analysts-no-rating-with-shares",
        "no-analysts-rating-none",
    ],
)
def test_a_response_without_a_consensus_becomes_an_unavailable_row_with_a_reason(payload) -> None:
    """Anti-fabrication: no rating or no analysts never becomes a neutral rating.

    An EMPTY payload and an explicit zero (rating 0 is unknown, total 0 is no analysts) are
    no coverage. A non-empty payload that lacks a field is a contract error, tested below."""
    conn = _MockConnection()
    ctx = _FakeQuoteContext({"US.XYZ": payload})
    res = capture_ticker_analyst_ratings(
        ctx=ctx,
        ticker="XYZ",
        company_id=_issuer_id("xyz"),
        connection=conn,
        run_id="run:test",
    )
    assert res.rows == 1
    assert res.failure is None, "no coverage is not a fetch error"
    _, params = conn.executed[0]
    assert params[1] == _issuer_id("xyz")
    assert params[3] is None, "Fabricated rating without a consensus!"
    assert params[4] == 0
    assert params[11] == "unavailable"
    assert params[9] == ["no_analyst_coverage"]


def _capture(payload: Any):
    conn = _MockConnection()
    res = capture_ticker_analyst_ratings(
        ctx=_FakeQuoteContext({"US.XYZ": payload}),
        ticker="XYZ",
        company_id=_issuer_id("xyz"),
        connection=conn,
        run_id="run:test",
    )
    return res, conn.executed[0][1]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"total": 5}, "lacks rating"),
        ({"total": 1}, "lacks rating"),
        ({"rating": 4}, "lacks total"),
        ({"rating": 0}, "lacks total"),
        ({"buy": 50.0, "hold": 50.0}, "lacks rating and total"),
        ({"rating": None, "total": 5}, "lacks rating"),
        ({"rating": 4, "total": None}, "lacks total"),
    ],
    ids=[
        "no-rating",
        "one-analyst-no-rating",
        "no-total",
        "unknown-rating-no-total",
        "neither",
        "rating-none",
        "total-none",
    ],
)
def test_a_non_empty_payload_that_lacks_the_rating_or_the_total_is_a_contract_error(payload, message) -> None:
    """#771: only an EMPTY payload means no coverage. A payload with other keys but no
    `rating` or no `total` is a changed vendor contract, so it must not read as no coverage."""
    res, params = _capture(payload)

    assert res.failure is not None, "a contract error is a fetch failure, not no coverage"
    assert res.failure.error.startswith("ValueError: ")
    assert message in res.failure.error
    assert params[9] == ["fetch_error:ValueError"]
    assert params[3] is None
    assert params[11] == "unavailable"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"rating": 4.9, "total": 10}, "moomoo rating 4.9 is not a whole number"),
        ({"rating": float("nan"), "total": 10}, "moomoo rating nan is not a whole number"),
        ({"rating": "4", "total": 10}, "moomoo rating '4' is not a whole number"),
        ({"rating": True, "total": 10}, "moomoo rating True is not a whole number"),
        ({"rating": 4, "total": 10.5}, "moomoo total 10.5 is not a whole number"),
        ({"rating": 4, "total": "abc"}, "moomoo total 'abc' is not a whole number"),
        ({"rating": 4, "total": "12"}, "moomoo total '12' is not a whole number"),
        ({"rating": 4, "total": False}, "moomoo total False is not a whole number"),
        ({"rating": 4, "total": -1}, "moomoo total -1 is negative"),
        ({"rating": 0, "total": -3}, "moomoo total -3 is negative"),
        ({"rating": 6, "total": 10}, "moomoo rating 6 is outside 1 to 5"),
        ({"rating": -1, "total": 10}, "moomoo rating -1 is outside 1 to 5"),
    ],
    ids=[
        "rating-fraction",
        "rating-nan",
        "rating-string",
        "rating-bool",
        "total-fraction",
        "total-text",
        "total-numeric-text",
        "total-bool",
        "total-negative",
        "total-negative-with-unknown-rating",
        "rating-above-max",
        "rating-below-min",
    ],
)
def test_a_rating_or_total_that_is_not_a_valid_count_is_a_contract_error(payload, message) -> None:
    """A rating of 4.9 must not truncate to 4, and a negative total must not read as no analysts."""
    res, params = _capture(payload)

    assert res.failure is not None
    assert res.failure.error == f"ValueError: {message}"
    assert params[9] == ["fetch_error:ValueError"]
    assert params[3] is None, "no rating is persisted for a payload that broke the contract"


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5, 4.0])
def test_every_rating_from_1_to_5_is_a_consensus(rating) -> None:
    """The bounds are inclusive: 1 (strong sell) and 5 (strong buy) are both real ratings."""
    res, params = _capture({"rating": rating, "total": 10})

    assert res.failure is None
    assert params[3] == Decimal(int(rating))
    assert params[11] == "available"


def test_rating_0_is_unknown_and_not_a_consensus() -> None:
    """0 sits one below the lowest rating: it is unknown, so it stays no coverage."""
    res, params = _capture({"rating": 0, "total": 10})

    assert res.failure is None
    assert (params[3], params[9], params[11]) == (None, ["no_analyst_coverage"], "unavailable")


def test_a_consensus_without_the_share_fields_keeps_the_rating_and_counts_zero() -> None:
    """The SDK omits each field moomoo leaves unset: a missing or None share is no count."""
    for payload in ({"rating": 4, "total": 10}, {"rating": 4, "total": 10, "buy": None, "hold": None, "sell": None}):
        res, params = _capture(payload)

        assert res.failure is None, payload
        assert (params[3], params[4]) == (Decimal(4), 10)
        assert (params[5], params[6], params[7]) == (0, 0, 0)
        assert params[11] == "available"


@pytest.mark.parametrize(
    ("share", "count"), [(0.0, 0), (0.01, 0), (50.0, 5), (99.99, 10), (100.0, 10), (100, 10)], ids=str
)
def test_a_share_from_0_to_100_percent_is_a_count(share, count) -> None:
    res, params = _capture({"rating": 4, "total": 10, "buy": share})

    assert res.failure is None
    assert params[5] == count


@pytest.mark.parametrize("share", [True, False, "12.3", "abc"], ids=["true", "false", "numeric-text", "text"])
def test_a_share_that_is_no_number_is_a_contract_error(share) -> None:
    """`float(True)` is 1.0 and `float("12.3")` is 12.3: neither is a share moomoo sends."""
    res, params = _capture({"rating": 4, "total": 10, "hold": share})

    assert res.failure is not None
    assert res.failure.error == f"ValueError: analyst share {share!r} is not a number"
    assert params[9] == ["fetch_error:ValueError"]


@pytest.mark.parametrize("share", [-0.01, -1.0, 100.01, 101.0, float("nan")])
def test_a_share_outside_0_to_100_percent_is_a_contract_error(share) -> None:
    res, params = _capture({"rating": 4, "total": 10, "sell": share})

    assert res.failure is not None
    assert res.failure.error.startswith("ValueError: analyst share ")
    assert "is outside 0 to 100 percent" in res.failure.error
    assert params[9] == ["fetch_error:ValueError"]


def test_the_universe_run_writes_one_row_per_issuer_from_real_responses() -> None:
    conn = _MockConnection()
    ctx = _FakeQuoteContext({"US.DDOG": _sample("DDOG"), "US.DUOL": _sample("DUOL")})
    total = materialize_universe_analyst_ratings(
        conn,
        run_id="run:uni",
        cutoff=datetime(2026, 10, 6, tzinfo=UTC),
        tickers={DDOG: "DDOG", DUOL: "DUOL"},
        ctx=ctx,
    )
    assert total.rows == 2
    assert total.failures == ()
    assert [params[1] for _, params in conn.executed] == [DDOG, DUOL]
    assert [params[11] for _, params in conn.executed] == ["available", "available"]
    assert [params[4] for _, params in conn.executed] == [35, 7]


ERROR_LOGGER = "data_engine.datahub.analyst_ratings"
TICKERS = {DDOG: "DDOG", NICE: "NICE", SHOP: "SHOP"}


def _run_universe(responses: dict[str, Any], tickers: dict[str, str] | None = None):
    conn = _MockConnection()
    result = materialize_universe_analyst_ratings(
        conn,
        run_id="run:uni",
        cutoff=datetime(2026, 10, 6, tzinfo=UTC),
        tickers=tickers or TICKERS,
        ctx=_FakeQuoteContext(responses),
    )
    return conn, result


def _error_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == ERROR_LOGGER and r.levelno == logging.ERROR]


def test_one_failing_ticker_stays_unavailable_with_a_code_and_is_logged(caplog) -> None:
    """A partial failure keeps its row, names the cause in the log, and does not raise."""
    caplog.set_level(logging.INFO)
    conn, result = _run_universe(
        {"US.DDOG": _sample("DDOG"), "US.NICE": "no quote right for NICE", "US.SHOP": _sample("SHOP")}
    )

    assert result.rows == 3
    assert [f.ticker for f in result.failures] == ["NICE"]
    assert result.lane_failure() is None, "a partial failure is not a lane failure"

    rows = {params[1]: params for _, params in conn.executed}
    assert rows[DDOG][11] == "available"
    assert rows[SHOP][11] == "available"
    failed = rows[NICE]
    assert failed[11] == "unavailable"
    assert failed[12] == "degraded"
    assert failed[9] == ["fetch_error:MoomooConnectionError"], "a code, never the free-text message"

    records = _error_records(caplog)
    assert len(records) == 1, "one ERROR record for the one failing ticker"
    assert "NICE" in records[0].getMessage()
    assert "no quote right for NICE" in records[0].getMessage()
    assert records[0].exc_info is not None, "the traceback is part of the record"


@pytest.mark.parametrize(
    ("response", "error_type", "message"),
    [
        ({"rating": 7, "total": 10}, "ValueError", "moomoo rating 7 is outside 1 to 5"),
        ({"rating": 4, "total": 10, "buy": 140.0}, "ValueError", "analyst share 140.0 is outside 0 to 100 percent"),
        (["not", "a", "mapping"], "TypeError", "returned list, expected a mapping"),
        (TimeoutError("network blip"), "MoomooConnectionError", "network blip"),
    ],
    ids=["rating-out-of-range", "share-out-of-range", "payload-not-a-mapping", "sdk-raises"],
)
def test_a_payload_that_breaks_the_vendor_contract_is_a_logged_fetch_error(
    caplog, response, error_type, message
) -> None:
    conn, result = _run_universe({"US.DDOG": _sample("DDOG"), "US.NICE": response}, {DDOG: "DDOG", NICE: "NICE"})

    assert [(f.ticker, f.error.split(":")[0]) for f in result.failures] == [("NICE", error_type)]
    assert message in result.failures[0].error
    reason_codes = conn.executed[1][1][9]
    assert reason_codes == [f"fetch_error:{error_type}"]
    assert all(re.fullmatch(r"fetch_error:[A-Za-z]+", code) for code in reason_codes)
    records = _error_records(caplog)
    assert len(records) == 1
    assert message in records[0].getMessage()


CUTOFF = datetime(2026, 10, 6, tzinfo=UTC)


def test_every_row_is_stamped_with_the_heads_cutoff_not_the_clock() -> None:
    """The cutoff is the governed head's (PIT rule): an available row, a no-coverage row,
    a fetch-error row, a row without a context and a row for a context that did not open."""
    responses = {"US.DDOG": _sample("DDOG"), "US.NICE": {}, "US.SHOP": "first failure"}
    conn = _MockConnection()
    kwargs: dict[str, Any] = {"run_id": "run:uni", "cutoff": CUTOFF, "tickers": TICKERS}
    materialize_universe_analyst_ratings(conn, ctx=_FakeQuoteContext(responses), **kwargs)
    materialize_universe_analyst_ratings(conn, ctx=None, **kwargs)
    materialize_universe_analyst_ratings(conn, ctx=None, open_error=RuntimeError("closed"), **kwargs)

    assert [params[11] for _, params in conn.executed[:3]] == ["available", "unavailable", "unavailable"]
    assert [params[2] for _, params in conn.executed] == [CUTOFF] * 9


def test_the_confidence_of_each_kind_of_row() -> None:
    """0.85 for a real consensus; 0 for a row that holds no rating, whatever the reason."""
    responses = {"US.DDOG": _sample("DDOG"), "US.NICE": {}, "US.SHOP": "first failure"}
    conn, _result = _run_universe(responses)

    assert [params[8] for _, params in conn.executed] == [Decimal("0.85"), Decimal("0"), Decimal("0")]
    assert [params[9] for _, params in conn.executed] == [
        [],
        ["no_analyst_coverage"],
        ["fetch_error:MoomooConnectionError"],
    ]


class _FailingWrite(_MockConnection):
    def execute(self, sql, params=None):
        raise psycopg.OperationalError("write failed")


@pytest.mark.parametrize(
    "payload",
    [_sample("DDOG"), {}, "first failure"],
    ids=["available-row", "no-coverage-row", "fetch-error-row"],
)
def test_a_write_error_is_not_a_fetch_error_and_is_not_swallowed(payload) -> None:
    ctx = _FakeQuoteContext({"US.DDOG": payload})
    with pytest.raises(psycopg.OperationalError, match="write failed"):
        capture_ticker_analyst_ratings(
            ctx=ctx, ticker="DDOG", company_id=DDOG, connection=_FailingWrite(), run_id="run:test"
        )
    with pytest.raises(psycopg.OperationalError, match="write failed"):
        materialize_universe_analyst_ratings(
            _FailingWrite(), run_id="run:test", cutoff=CUTOFF, tickers={DDOG: "DDOG"}, ctx=ctx
        )


@pytest.mark.parametrize(
    ("rows", "failed", "is_lane_failure"),
    [
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
        (2, 1, False),
        (3, 2, False),
        (4, 3, False),
        (3, 1, False),
        (3, 0, False),
        (1, 0, False),
        (0, 0, False),
    ],
)
def test_a_lane_failure_is_every_ticker_of_a_non_empty_run(rows, failed, is_lane_failure) -> None:
    failures = tuple(FetchFailure(ticker=f"T{n}", error="X: boom") for n in range(failed))
    text = UniverseCapture(rows=rows, failures=failures).lane_failure()

    assert (text is not None) is is_lane_failure
    if is_lane_failure:
        assert text == f"analyst ratings fetch failed for {rows} of {rows} tickers; first error: T0: X: boom"


def test_the_traceback_is_logged_for_the_first_20_failures_only(caplog) -> None:
    from data_engine.datahub.analyst_ratings import MAX_FAILURES_LOGGED

    tickers = {_issuer_id(f"t{n}"): f"T{n}" for n in range(25)}
    _conn, result = _run_universe({f"US.T{n}": "no quote right" for n in range(25)}, tickers)

    records = _error_records(caplog)
    assert (MAX_FAILURES_LOGGED, len(result.failures), len(records)) == (20, 25, 25), "one record per failure"
    assert [bool(r.exc_info) for r in records] == [True] * 20 + [False] * 5
    assert all("no quote right" in r.getMessage() for r in records)


def test_the_traceback_cap_counts_failures_not_tickers(caplog) -> None:
    """30 tickers, the first 5 answer, the other 25 fail: the first 20 FAILURES log a traceback."""
    tickers = {_issuer_id(f"t{n}"): f"T{n}" for n in range(30)}
    responses = {f"US.T{n}": _sample("DDOG") if n < 5 else "no quote right" for n in range(30)}
    _conn, result = _run_universe(responses, tickers)

    records = _error_records(caplog)
    assert (len(result.failures), len(records)) == (25, 25)
    assert [bool(r.exc_info) for r in records] == [True] * 20 + [False] * 5
    assert result.failures[0].ticker == "T5"
    assert records[0].getMessage().startswith("analyst consensus fetch failed for T5 ")
    assert records[19].getMessage().startswith("analyst consensus fetch failed for T24 ")


def test_every_ticker_failing_persists_the_rows_and_names_the_lane_failure() -> None:
    conn, result = _run_universe({"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"})

    assert result.rows == 3
    assert len(conn.executed) == 3, "the unavailable rows are persisted before the run fails"
    assert [params[11] for _, params in conn.executed] == ["unavailable"] * 3
    text = result.lane_failure()
    assert text is not None
    assert "3 of 3" in text
    assert "first failure" in text
    assert "second failure" not in text


def test_an_empty_universe_and_a_run_without_a_context_are_not_a_lane_failure() -> None:
    conn = _MockConnection()
    empty = materialize_universe_analyst_ratings(
        conn, run_id="run:none", cutoff=datetime(2026, 10, 6, tzinfo=UTC), tickers={}, ctx=_FakeQuoteContext({})
    )
    no_ctx = materialize_universe_analyst_ratings(
        conn, run_id="run:none", cutoff=datetime(2026, 10, 6, tzinfo=UTC), tickers=TICKERS, ctx=None
    )
    assert (empty.rows, no_ctx.rows) == (0, 3)
    assert empty.lane_failure() is None
    assert no_ctx.lane_failure() is None
    assert [params[9] for _, params in conn.executed] == [["no_analyst_coverage"]] * 3


class _RecordingConnection:
    """A connection that records the order of `execute` and `commit`, shared across the op."""

    def __init__(self, events: list[str], rows: list[tuple], write_fails: Exception | None = None):
        self.events = events
        self.rows = rows
        self.write_fails = write_fails

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_a):
        self.events.append("rollback" if exc_type else "close")
        return False

    def execute(self, sql, params=None):
        self.events.append(f"execute:{params[1]}")
        if self.write_fails is not None:
            raise self.write_fails
        self.rows.append(params)

    def commit(self) -> None:
        self.events.append("commit")


@dataclass
class _OpRun:
    """What `_run_op` observed: the result, the ordered connection events, the row params
    the op wrote, and the Dagster instance that holds the run's log records."""

    result: Any
    events: list[str]
    rows: list[tuple]
    instance: dg.DagsterInstance

    def log_messages(self, level: int) -> list[str]:
        """The user messages the op logged at exactly this Python logging level."""
        return [
            entry.user_message
            for entry in self.instance.all_logs(self.result.run_id)
            if entry.dagster_event is None and entry.level == level
        ]

    def output_metadata(self) -> dict[str, Any]:
        [output] = [e for e in self.result.events_for_node("run_analyst_ratings") if e.is_successful_output]
        return {key: value.value for key, value in output.step_output_data.metadata.items()}


HEAD_CUTOFF = datetime(2026, 10, 6, tzinfo=UTC)


def _run_op(
    monkeypatch,
    responses: dict[str, Any],
    *,
    tickers: dict[str, str] | None = None,
    open_fails: Exception | None = None,
    close_fails: Exception | None = None,
    write_fails: Exception | None = None,
) -> _OpRun:
    """Run the deployed op `run_analyst_ratings` as a one-op job, with moomoo and Postgres faked.

    `open_fails` is raised when the op opens the moomoo context. `close_fails` is raised when
    the op closes it again. `write_fails` is raised by the first row write.
    `tickers` is the universe: issuer id to ticker, `TICKERS` by default.
    """
    from data_engine.datahub import question_coverage
    from data_engine.datahub.standards import planner
    from data_engine.lanes import standards
    from data_engine.lanes.standards import run_analyst_ratings

    events: list[str] = []
    rows: list[tuple] = []
    ctx = _FakeQuoteContext(responses)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _RecordingConnection(events, rows, write_fails))
    monkeypatch.setattr(standards, "canonicalize_universe", _identity_universe)
    monkeypatch.setattr(question_coverage, "head_report_date", lambda *_a, **_k: date(2026, 10, 6))
    monkeypatch.setattr(
        question_coverage,
        "governed_head",
        lambda _c, **_k: question_coverage.GovernedHead("universe:test", "run:head", HEAD_CUTOFF),
    )
    monkeypatch.setattr(
        planner,
        "universe_issuers",
        lambda *_a, **_k: [SimpleNamespace(issuer_id=i, ticker=t) for i, t in (tickers or TICKERS).items()],
    )
    monkeypatch.setattr(
        question_coverage,
        "gppe_cells",
        lambda _c, _run: tuple(question_coverage.Cell(i, True) for i in (tickers or TICKERS)),
    )

    @contextmanager
    def fake_connect():
        if open_fails is not None:
            raise open_fails
        yield ctx
        if close_fails is not None:
            raise close_fails

    monkeypatch.setattr(mm, "connect", fake_connect)

    @dg.op
    def upstream_summary() -> str:
        return "{}"

    @dg.job
    def one_op_job():
        run_analyst_ratings(upstream_summary())

    instance = dg.DagsterInstance.ephemeral()
    result = one_op_job.execute_in_process(
        run_config={"ops": {"run_analyst_ratings": {"config": {"executed_at": "2026-10-06T00:00:00+00:00"}}}},
        instance=instance,
        raise_on_error=False,
    )
    return _OpRun(result, events, rows, instance)


FETCH_FAILURE_LOG = "analyst consensus fetch failed for "
FAILED = {"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"}


def test_the_op_commits_and_reports_a_total_failure_without_raising(monkeypatch) -> None:
    """#771: the lane failure travels in the summary. The op that measures the lane (coverage)
    must still run, so this op does not raise; the terminal op `fail_if_a_lane_failed` does."""
    run = _run_op(monkeypatch, {"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"})

    assert run.result.success
    summary = json.loads(run.result.output_for_node("run_analyst_ratings"))
    assert summary["rows"] == 3
    assert summary["fetch_errors"] == 3
    assert "3 of 3" in summary["lane_failure"]
    assert "first failure" in summary["lane_failure"]
    assert run.events == [f"execute:{DDOG}", f"execute:{NICE}", f"execute:{SHOP}", "commit", "close"], (
        "the unavailable rows are committed before the op returns"
    )
    assert [params[2] for params in run.rows] == [HEAD_CUTOFF] * 3, "stamped with the head's cutoff, not the clock"


def test_the_op_succeeds_on_a_partial_failure(monkeypatch) -> None:
    run = _run_op(monkeypatch, {"US.DDOG": _sample("DDOG"), "US.NICE": "second failure", "US.SHOP": _sample("SHOP")})

    assert run.result.success
    summary = json.loads(run.result.output_for_node("run_analyst_ratings"))
    assert summary["rows"] == 3
    assert summary["fetch_errors"] == 1
    assert "lane_failure" not in summary
    assert run.events == [f"execute:{DDOG}", f"execute:{NICE}", f"execute:{SHOP}", "commit", "close"]


def test_the_op_adds_its_summary_as_output_metadata(monkeypatch) -> None:
    """The Dagster UI shows the run's metadata: it must hold the summary the next op reads."""
    run = _run_op(monkeypatch, FAILED)

    summary = json.loads(run.result.output_for_node("run_analyst_ratings"))
    assert run.output_metadata() == summary
    assert (summary["rows"], summary["fetch_errors"]) == (3, 3)
    assert "lane_failure" in summary


def test_the_op_logs_each_fetch_failure_at_error_level(monkeypatch) -> None:
    run = _run_op(monkeypatch, FAILED)

    logged = [m for m in run.log_messages(logging.ERROR) if m.startswith(FETCH_FAILURE_LOG)]
    prefix = f"{FETCH_FAILURE_LOG}%s: MoomooConnectionError: get_research_analyst_consensus failed: %s"
    assert logged == [
        prefix % ("DDOG", "first failure"),
        prefix % ("NICE", "second failure"),
        prefix % ("SHOP", "third failure"),
    ]


def test_the_op_logs_a_partial_failure_for_the_failed_ticker_only(monkeypatch) -> None:
    run = _run_op(monkeypatch, {"US.DDOG": _sample("DDOG"), "US.NICE": "second failure", "US.SHOP": _sample("SHOP")})

    logged = [m for m in run.log_messages(logging.ERROR) if m.startswith(FETCH_FAILURE_LOG)]
    assert len(logged) == 1
    assert "NICE" in logged[0] and "second failure" in logged[0]


def test_the_op_logs_at_most_20_fetch_failures_per_run_and_counts_the_rest(monkeypatch) -> None:
    tickers = {_issuer_id(f"t{n}"): f"T{n}" for n in range(25)}
    run = _run_op(monkeypatch, {f"US.T{n}": "no quote right" for n in range(25)}, tickers=tickers)

    logged = [m for m in run.log_messages(logging.ERROR) if m.startswith(FETCH_FAILURE_LOG)]
    assert len(logged) == 20
    assert logged[0].startswith(f"{FETCH_FAILURE_LOG}T0: ")
    assert logged[-1].startswith(f"{FETCH_FAILURE_LOG}T19: ")
    assert [m for m in run.log_messages(logging.WARNING) if "5 more" in m], "the unlogged rest is counted"
    assert json.loads(run.result.output_for_node("run_analyst_ratings"))["fetch_errors"] == 25


def test_the_op_logs_exactly_20_failures_without_an_overflow_note(monkeypatch) -> None:
    tickers = {_issuer_id(f"t{n}"): f"T{n}" for n in range(20)}
    run = _run_op(monkeypatch, {f"US.T{n}": "no quote right" for n in range(20)}, tickers=tickers)

    logged = [m for m in run.log_messages(logging.ERROR) if m.startswith(FETCH_FAILURE_LOG)]
    assert len(logged) == 20
    assert [m for m in run.log_messages(logging.WARNING) if "more" in m] == []


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (mm.MoomooConnectionError("OpenD not reachable at the host"), "MoomooConnectionError"),
        (RuntimeError("MOOMOO_OPEND_HOST / MOOMOO_OPEND_PORT are not configured"), "RuntimeError"),
    ],
    ids=["opend-down", "opend-not-configured"],
)
def test_a_context_that_cannot_be_opened_fails_every_ticker_with_the_exception_type(
    monkeypatch, error, error_type
) -> None:
    """#771: an OpenD outage is a total fetch failure, not a quiet `no_analyst_coverage` row."""
    run = _run_op(monkeypatch, {}, open_fails=error)

    assert run.result.success, "the op reports and returns; the terminal op fails the run"
    assert [params[1] for params in run.rows] == list(TICKERS)
    assert [(params[9], params[11]) for params in run.rows] == [([f"fetch_error:{error_type}"], "unavailable")] * 3
    summary = json.loads(run.result.output_for_node("run_analyst_ratings"))
    assert (summary["rows"], summary["fetch_errors"]) == (3, 3)
    assert "3 of 3" in summary["lane_failure"]
    assert f"DDOG: {error_type}: {error}" in summary["lane_failure"]
    assert run.events[-2:] == ["commit", "close"], "the rows are committed before the terminal op fails the run"
    assert any(str(error) in m for m in run.log_messages(logging.ERROR)), "the open failure is logged"


def test_a_close_error_keeps_the_committed_rows_and_still_fails_the_op(monkeypatch) -> None:
    """Closing the context raises after the good rows are written. The rows are committed
    BEFORE the context closes, so the close error cannot undo the night's fetches. The op still
    fails: the close error is not swallowed, and no fallback pass overwrites a good row."""
    run = _run_op(
        monkeypatch,
        {"US.DDOG": _sample("DDOG"), "US.NICE": _sample("NICE"), "US.SHOP": _sample("SHOP")},
        close_fails=RuntimeError("close failed"),
    )

    assert not run.result.success
    assert [(params[1], params[11]) for params in run.rows] == [
        (DDOG, "available"),
        (NICE, "available"),
        (SHOP, "available"),
    ], "each issuer is written once, as available"
    assert run.events == [f"execute:{DDOG}", f"execute:{NICE}", f"execute:{SHOP}", "commit", "rollback"]
    [failure] = [e for e in run.result.all_events if e.is_step_failure]
    assert failure.step_failure_data.error.cause.cls_name == "RuntimeError"
    assert "close failed" in failure.step_failure_data.error.cause.message


def test_a_write_error_in_the_op_commits_nothing_and_fails_the_op(monkeypatch) -> None:
    """A write error is not a fetch error: no row is committed and no second pass runs."""
    run = _run_op(
        monkeypatch,
        {"US.DDOG": _sample("DDOG"), "US.NICE": _sample("NICE"), "US.SHOP": _sample("SHOP")},
        write_fails=psycopg.OperationalError("write failed"),
    )

    assert not run.result.success
    assert run.events == [f"execute:{DDOG}", "rollback"]
    [failure] = [e for e in run.result.all_events if e.is_step_failure]
    assert "write failed" in failure.step_failure_data.error.cause.message


def test_the_universe_run_with_an_open_error_fails_every_ticker_like_a_total_fetch_failure(caplog) -> None:
    conn = _MockConnection()
    error = mm.MoomooConnectionError("OpenD not reachable")
    result = materialize_universe_analyst_ratings(
        conn,
        run_id="run:uni",
        cutoff=datetime(2026, 10, 6, tzinfo=UTC),
        tickers=TICKERS,
        ctx=None,
        open_error=error,
    )

    assert result.rows == 3
    assert [f.ticker for f in result.failures] == list(TICKERS.values())
    assert all(f.error == "MoomooConnectionError: OpenD not reachable" for f in result.failures)
    assert [params[9] for _, params in conn.executed] == [["fetch_error:MoomooConnectionError"]] * 3
    assert [params[11] for _, params in conn.executed] == ["unavailable"] * 3
    assert "3 of 3" in (result.lane_failure() or "")
    assert len(_error_records(caplog)) == 1, "one ERROR record for the one open failure, not one per ticker"


# --- the lane fails AFTER the coverage report is written (#771, the #1016 pattern) --------
#
# The coverage report is the instrument that measures every lane. A lane that fails for every
# ticker must not stop the instrument: the report is written first, and only then does the run
# end as FAILURE. These tests run the DEPLOYED jobs, with the real analyst op, the real coverage
# op and a real Postgres. The connection is shared and rolled back at the end: `commit` is
# counted, never executed, so no row outlives the test.
#
# These tests do not assert a Q4 coverage value. The coverage cells here reuse the issuer ids
# of the analyst rows. A Q4 assertion would pass only because of those fakes.
# `test_canonical_issuer_id.py` asserts the join over a real capture (#1079).
# What this file controls is asserted instead: the persisted analyst rows, the lane summary, the run.

# One token per test session. The DB-backed tests read verdicts, reports and analyst rows by
# these names only, so rows that a development database holds from earlier work cannot change a result.
_TOKEN = uuid.uuid4().hex
HEAD_RUN = "capture-run:" + _TOKEN * 2
HEAD_UNIVERSE_ID = f"universe:t771-{_TOKEN[:12]}"
UNIQUE_UNIVERSE = f"universe-list:t771-{_TOKEN[:12]}"
EXECUTED_AT = "2026-10-06T04:00:00+00:00"
QQQ = "universe-list:qqq"
JOB_NAMES = ("head_reports_pipeline_job", "standard_backfill_pipeline_job")


class _SharedConnection:
    """One real connection for every op of a job. `commit` is recorded, not executed.

    `events` holds, in order, every commit and every verdict row the job writes, so a test
    can assert what happened before what.
    """

    def __init__(self, connection: psycopg.Connection[Any]):
        self.connection = connection
        self.commits = 0
        self.events: list[str] = []
        self.verdicts: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def commit(self) -> None:
        self.commits += 1
        self.events.append("commit")

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


@pytest.fixture
def shared_connection():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            raise
        pytest.skip("no local Postgres")
    try:
        yield _SharedConnection(connection)
    finally:
        connection.rollback()
        connection.close()


def _execute_job(
    monkeypatch,
    shared_connection,
    job_name: str,
    responses: dict[str, Any],
    *,
    open_fails: Exception | None = None,
    executed_at: str = EXECUTED_AT,
    fallback: bool = False,
    real_verdicts: bool = False,
    instance: dg.DagsterInstance | None = None,
    universe: str = QQQ,
    connects: list[int] | None = None,
):
    """Execute one deployed job over a faked world: real analyst and coverage ops, real SQL.

    `open_fails` is raised when the analyst op opens the moomoo context, as an OpenD outage does.
    `executed_at` is the tick. `fallback` runs the 04:00 fallback request (`only_if_stale`) of the
    head-reports job, which reads the report an earlier run stored. `real_verdicts` writes the
    verdict rows to `mart.nightly_verdicts` through the real recorder, in the shared transaction.
    `instance` is the Dagster instance to run on; a test passes one to read the run's log records.
    `universe` is the universe of the run. `connects` gets one entry per attempt to open the moomoo
    context, so a test can tell a run that fetched from a run that found the head current."""
    from data_engine.datahub import question_coverage
    from data_engine.datahub.production_topt import theme_purity
    from data_engine.datahub.standards import planner, supply_chain_extraction
    from data_engine.lanes import standards
    from data_engine.quality import nightly_verdicts

    head = question_coverage.GovernedHead(HEAD_UNIVERSE_ID, HEAD_RUN, datetime(2026, 10, 6, tzinfo=UTC))
    issuers = [SimpleNamespace(issuer_id=i, ticker=t) for i, t in TICKERS.items()]
    ctx = _FakeQuoteContext(responses)

    @contextmanager
    def fake_connect():
        if connects is not None:
            connects.append(1)
        if open_fails is not None:
            raise open_fails
        yield ctx

    # The verdict names of a universe no schedule ticks are not recorded: register the test's own.
    names = tuple(f"{check}@{universe}" for check in ("theme_purity", "question_coverage"))
    monkeypatch.setattr(standards, "NIGHTLY_VERDICTS", (*dict.fromkeys((*standards.NIGHTLY_VERDICTS, *names)),))
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: shared_connection)
    monkeypatch.setattr(question_coverage, "governed_head", lambda _c, **_k: head)
    # A second job in one test must see the real reader again: monkeypatch keeps the first patch.
    monkeypatch.setattr(
        question_coverage,
        "stored_report_run",
        _STORED_REPORT_RUN if fallback else lambda _c, _universe: None,
    )
    monkeypatch.setattr(question_coverage, "declared_environment", lambda _c: "test")
    monkeypatch.setattr(
        question_coverage, "gppe_cells", lambda _c, _run: tuple(question_coverage.Cell(i, True) for i in TICKERS)
    )
    monkeypatch.setattr(planner, "universe_issuers", lambda *_a, **_k: issuers)
    monkeypatch.setattr(standards, "universe_issuers", lambda *_a, **_k: issuers)
    monkeypatch.setattr(standards, "canonicalize_universe", _identity_universe)
    monkeypatch.setattr(question_coverage, "head_report_date", lambda *_a, **_k: date(2026, 10, 6))
    monkeypatch.setattr(theme_purity, "materialize_theme_purity", lambda _c, **_k: ())
    monkeypatch.setattr(supply_chain_extraction, "materialize_universe_supply_chain_exposure", lambda _c, **_k: 0)

    def record_verdict(name: str, **row: Any) -> None:
        shared_connection.verdicts.append({"check": name, **row})
        shared_connection.events.append(f"verdict:{name}:{row['ok']}")

    if not real_verdicts:
        monkeypatch.setattr(nightly_verdicts, "record", record_verdict)
    monkeypatch.setattr(
        standards,
        "_run_standard_backfill",
        lambda *_a, **_k: SimpleNamespace(
            summary=lambda: {"standard": "stub"}, issuers=3, open=0, open_by_reason={}, outcomes={}
        ),
    )
    monkeypatch.setattr(mm, "connect", fake_connect)

    if job_name == "head_reports_pipeline_job":
        run_config = standards.head_reports_request(
            universe, executed_at, run_key="test", only_if_stale=fallback
        ).run_config
    else:
        assert not fallback, "only the head-reports job has a fallback"
        run_config = standards.backfill_run_config(executed_at, universe)
    return getattr(standards, job_name).execute_in_process(
        run_config=run_config, instance=instance, raise_on_error=False
    )


def _stored_analyst_rows(shared_connection) -> dict[str, tuple[str, list[str]]]:
    """issuer id -> (availability status, reason codes) of the analyst rows the job persisted."""
    rows = shared_connection.execute(
        "select issuer_id, availability_status, reason_codes from mart.issuer_analyst_ratings where run_id = %s",
        (HEAD_RUN,),
    ).fetchall()
    return {issuer_id: (status, list(codes)) for issuer_id, status, codes in rows}


def _coverage_report_exists(shared_connection) -> bool:
    row = shared_connection.execute(
        "select 1 from mart.question_coverage_report where run_id = %s", (HEAD_RUN,)
    ).fetchone()
    return row is not None


def _steps(result, *, failed: bool) -> list[str]:
    return [e.step_key for e in result.all_events if (e.is_step_failure if failed else e.is_step_success)]


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_a_total_failure_fails_the_run_after_the_coverage_report_is_written(
    monkeypatch, shared_connection, job_name
) -> None:
    result = _execute_job(monkeypatch, shared_connection, job_name, FAILED)

    assert not result.success, "the run ends as FAILURE, not 'published 3 analyst ratings rows'"
    assert _stored_analyst_rows(shared_connection) == {
        issuer_id: ("unavailable", ["fetch_error:MoomooConnectionError"]) for issuer_id in TICKERS
    }, "every persisted analyst row names the fetch error"
    summary = json.loads(result.output_for_node("run_analyst_ratings"))
    assert summary["fetch_errors"] == 3
    assert "3 of 3" in summary["lane_failure"]
    assert _coverage_report_exists(shared_connection), (
        "the coverage report for the head exists although the lane failed"
    )
    assert "run_analyst_ratings" in _steps(result, failed=False)
    assert "run_question_coverage" in _steps(result, failed=False)
    assert _steps(result, failed=True) == ["fail_if_a_lane_failed"]
    events = [(e.step_key, e.is_step_success or e.is_step_failure) for e in result.all_events if e.is_step_event]
    coverage_done = events.index(("run_question_coverage", True))
    assert events.index(("fail_if_a_lane_failed", True)) > coverage_done, "the terminal op runs after coverage"
    [failure] = [e for e in result.all_events if e.is_step_failure]
    cause = failure.step_failure_data.error.cause
    assert cause is not None and cause.cls_name == "RuntimeError"
    assert "3 of 3" in cause.message
    assert "first failure" in cause.message
    assert "second failure" not in cause.message


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_an_opend_outage_fails_the_run_after_the_rows_and_the_report_are_written(
    monkeypatch, shared_connection, job_name
) -> None:
    """#771: with OpenD down, the old op wrote `no_analyst_coverage` rows and the run succeeded."""
    result = _execute_job(
        monkeypatch, shared_connection, job_name, {}, open_fails=mm.MoomooConnectionError("OpenD not reachable")
    )

    assert not result.success, "an OpenD outage must not end as SUCCESS"
    assert _stored_analyst_rows(shared_connection) == {
        issuer_id: ("unavailable", ["fetch_error:MoomooConnectionError"]) for issuer_id in TICKERS
    }, "no row reads as no_analyst_coverage"
    assert _coverage_report_exists(shared_connection)
    assert _steps(result, failed=True) == ["fail_if_a_lane_failed"]
    verdict_name = f"question_coverage@{QQQ}"
    assert shared_connection.events[-3:] == ["commit", f"verdict:{verdict_name}:True", f"verdict:{verdict_name}:False"]
    [failure] = [e for e in result.all_events if e.is_step_failure]
    assert "3 of 3" in failure.step_failure_data.error.cause.message


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_a_partial_failure_leaves_the_run_green_and_the_rows_name_the_error(
    monkeypatch, shared_connection, job_name
) -> None:
    responses = {"US.DDOG": _sample("DDOG"), "US.NICE": "second failure", "US.SHOP": _sample("SHOP")}
    result = _execute_job(monkeypatch, shared_connection, job_name, responses)

    assert result.success
    assert _stored_analyst_rows(shared_connection) == {
        DDOG: ("available", []),
        NICE: ("unavailable", ["fetch_error:MoomooConnectionError"]),
        SHOP: ("available", []),
    }
    summary = json.loads(result.output_for_node("run_analyst_ratings"))
    assert summary["fetch_errors"] == 1
    assert "lane_failure" not in summary
    assert _coverage_report_exists(shared_connection)
    assert "fail_if_a_lane_failed" in _steps(result, failed=False)
    assert [v["check"] for v in shared_connection.verdicts if v["ok"] is False] == [], "a partial failure is not red"


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_a_run_without_a_failure_stays_green_and_the_terminal_op_does_no_work(
    monkeypatch, shared_connection, job_name
) -> None:
    responses = {f"US.{t}": _sample(t) for t in ("DDOG", "NICE", "SHOP")}
    result = _execute_job(monkeypatch, shared_connection, job_name, responses)

    assert result.success
    assert _stored_analyst_rows(shared_connection) == {issuer_id: ("available", []) for issuer_id in TICKERS}
    assert _coverage_report_exists(shared_connection)
    assert "fail_if_a_lane_failed" in _steps(result, failed=False)
    assert shared_connection.commits == 4, "purity, supply chain, analyst ratings and coverage commit once each"
    assert [v["check"] for v in shared_connection.verdicts if v["ok"] is False] == [], "a clean run has no red row"


FALLBACK_AT = "2026-10-07T04:00:00+00:00"
COVERAGE_CHECK = f"question_coverage@{UNIQUE_UNIVERSE}"
PURITY_CHECK = f"theme_purity@{UNIQUE_UNIVERSE}"
HEAD_JOB = "head_reports_pipeline_job"
OPEND_DOWN = mm.MoomooConnectionError("OpenD not reachable")


def _verdict_rows(shared_connection, check: str) -> list[tuple[bool | None, str, str]]:
    """Every row of one check in write order: (ok, tick as ISO text, summary)."""
    rows = shared_connection.execute(
        "select ok, ran_at, summary from mart.nightly_verdicts where check_name = %s order by verdict_id", (check,)
    ).fetchall()
    return [(ok, ran_at.isoformat(), summary) for ok, ran_at, summary in rows]


def _health_verdict(shared_connection, check: str) -> tuple[bool | None, str]:
    """What `/api/health` reports for one check: the row of `llm_service.main.NIGHTLY_VERDICTS_SQL`."""
    from llm_service.main import NIGHTLY_VERDICTS_SQL

    reported = {
        name: (ok, ran_at.isoformat()) for name, ran_at, ok, _summary in shared_connection.execute(NIGHTLY_VERDICTS_SQL)
    }
    return reported[check]


VALID = {f"US.{ticker}": _sample(ticker) for ticker in ("DDOG", "NICE", "SHOP")}


def test_newest_is_red_reads_the_row_the_health_endpoint_reads(monkeypatch, shared_connection) -> None:
    """The newest row per check decides: newest by `ran_at`, then by `recorded_at`. A pending
    row (ok null) and a missing check are not red."""
    from data_engine.quality import nightly_verdicts

    name = f"question_coverage@newest-red-{_TOKEN[:12]}"
    day1, day2 = datetime(2026, 10, 6, 4, 0, tzinfo=UTC), datetime(2026, 10, 7, 4, 0, tzinfo=UTC)

    def add(ok: bool | None, ran_at: datetime) -> None:
        shared_connection.execute(nightly_verdicts.INSERT_SQL, (name, ran_at, ok, "x", "test-run"))

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: shared_connection)
    assert nightly_verdicts.newest_is_red(name) is False, "no row"
    add(True, day1)
    add(False, day1)
    assert nightly_verdicts.newest_is_red(name) is True, "red written after green on the same tick"
    add(True, day2)
    assert nightly_verdicts.newest_is_red(name) is False, "a later green"
    add(False, datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    assert nightly_verdicts.newest_is_red(name) is False, "a red row of an older tick, written last"
    add(None, datetime(2026, 10, 8, 4, 0, tzinfo=UTC))
    assert nightly_verdicts.newest_is_red(name) is False, "a pending row is not red"
    add(False, datetime(2026, 10, 9, 4, 0, tzinfo=UTC))
    assert nightly_verdicts.newest_is_red(name) is True


def _record_red(name: str, ran_at: str) -> None:
    """One red verdict row through the real recorder: a check that failed at `ran_at`."""
    from data_engine.quality import nightly_verdicts

    nightly_verdicts.record(
        name, ran_at=datetime.fromisoformat(ran_at), ok=False, summary="failed: test", run_id="test-run"
    )


def _ticks(shared_connection, check: str) -> list[tuple[bool | None, str]]:
    return [(ok, tick) for ok, tick, _summary in _verdict_rows(shared_connection, check)]


def test_a_fallback_retries_a_red_head_and_stays_red_while_opend_is_down(monkeypatch, shared_connection) -> None:
    """#771: the fallback found the failed run's stored report current, so it never retried the
    lane, and the red verdict stayed until the next pointer advance, whatever OpenD did."""
    connects: list[int] = []
    failed = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        FAILED,
        universe=UNIQUE_UNIVERSE,
        real_verdicts=True,
        connects=connects,
    )
    assert not failed.success
    assert connects == [1]
    assert _ticks(shared_connection, COVERAGE_CHECK) == [(True, EXECUTED_AT), (False, EXECUTED_AT)]

    retry = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        {},
        universe=UNIQUE_UNIVERSE,
        executed_at=FALLBACK_AT,
        fallback=True,
        real_verdicts=True,
        connects=connects,
        open_fails=OPEND_DOWN,
    )

    assert connects == [1, 1], "the lane ran again: OpenD was asked once more"
    assert not retry.success, "OpenD is still down, so the retry fails too"
    assert _stored_analyst_rows(shared_connection) == {
        issuer_id: ("unavailable", ["fetch_error:MoomooConnectionError"]) for issuer_id in TICKERS
    }
    assert _ticks(shared_connection, COVERAGE_CHECK) == [
        (True, EXECUTED_AT),
        (False, EXECUTED_AT),
        (True, FALLBACK_AT),
        (False, FALLBACK_AT),
    ], "the retry wrote a newer red row"
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (False, FALLBACK_AT)


def test_a_fallback_retries_a_red_head_and_turns_green_when_opend_is_back(monkeypatch, shared_connection) -> None:
    connects: list[int] = []
    failed = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        FAILED,
        universe=UNIQUE_UNIVERSE,
        real_verdicts=True,
        connects=connects,
    )
    assert not failed.success
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (False, EXECUTED_AT)

    retry = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        VALID,
        universe=UNIQUE_UNIVERSE,
        executed_at=FALLBACK_AT,
        fallback=True,
        real_verdicts=True,
        connects=connects,
    )

    assert retry.success
    assert connects == [1, 1]
    assert _stored_analyst_rows(shared_connection) == {issuer_id: ("available", []) for issuer_id in TICKERS}
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (True, FALLBACK_AT)
    assert _verdict_rows(shared_connection, COVERAGE_CHECK)[-1][2].startswith("report persisted for ")

    next_fallback = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        {},
        universe=UNIQUE_UNIVERSE,
        executed_at="2026-10-08T04:00:00+00:00",
        fallback=True,
        real_verdicts=True,
        connects=connects,
    )
    assert next_fallback.success
    assert connects == [1, 1], "a green head is current again: the next fallback fetches nothing"


def test_a_fallback_finds_a_green_head_current_and_fetches_nothing(monkeypatch, shared_connection) -> None:
    connects: list[int] = []
    first = _execute_job(
        monkeypatch, shared_connection, HEAD_JOB, VALID, universe=UNIQUE_UNIVERSE, real_verdicts=True, connects=connects
    )
    assert first.success

    fallback = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        {},
        universe=UNIQUE_UNIVERSE,
        executed_at=FALLBACK_AT,
        fallback=True,
        real_verdicts=True,
        connects=connects,
    )

    assert fallback.success
    assert connects == [1], "only the first run asked OpenD"
    rows = _verdict_rows(shared_connection, COVERAGE_CHECK)
    assert [(ok, tick) for ok, tick, _summary in rows] == [(True, EXECUTED_AT), (True, FALLBACK_AT)]
    assert rows[-1][2].startswith("reports already current on ")
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (True, FALLBACK_AT)


def test_a_red_theme_purity_verdict_makes_the_head_not_current(monkeypatch, shared_connection) -> None:
    """The rule is shared: the coverage verdict is green and the report is stored, yet a red
    theme purity verdict sends the fallback through the lanes again."""
    connects: list[int] = []
    first = _execute_job(
        monkeypatch, shared_connection, HEAD_JOB, VALID, universe=UNIQUE_UNIVERSE, real_verdicts=True, connects=connects
    )
    assert first.success
    _record_red(PURITY_CHECK, "2026-10-06T06:00:00+00:00")
    assert _health_verdict(shared_connection, PURITY_CHECK) == (False, "2026-10-06T06:00:00+00:00")

    fallback = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        VALID,
        universe=UNIQUE_UNIVERSE,
        executed_at=FALLBACK_AT,
        fallback=True,
        real_verdicts=True,
        connects=connects,
    )

    assert fallback.success
    assert connects == [1, 1], "the head was not current: the lanes ran again"
    assert _health_verdict(shared_connection, PURITY_CHECK) == (True, FALLBACK_AT)
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (True, FALLBACK_AT)


def test_already_current_writes_no_green_row_over_a_red_verdict(monkeypatch, shared_connection) -> None:
    """Defence in depth: if the start op calls a red head current, `_already_current` still keeps the red."""
    from data_engine.lanes import standards

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: shared_connection)
    monkeypatch.setattr(standards, "NIGHTLY_VERDICTS", (*standards.NIGHTLY_VERDICTS, COVERAGE_CHECK))
    config = standards.StandardBackfillConfig(executed_at=FALLBACK_AT, universe=UNIQUE_UNIVERSE)
    _record_red(COVERAGE_CHECK, EXECUTED_AT)

    out = standards._already_current(dg.build_op_context(), standards.QUESTION_COVERAGE_VERDICT, config, HEAD_RUN)

    assert json.loads(out)[standards.REPORTS_CURRENT] == HEAD_RUN
    assert _ticks(shared_connection, COVERAGE_CHECK) == [(False, EXECUTED_AT)], "nothing written over the red"

    from data_engine.quality import nightly_verdicts

    nightly_verdicts.record(
        COVERAGE_CHECK, ran_at=datetime.fromisoformat(EXECUTED_AT), ok=True, summary="ok", run_id="test-run"
    )
    standards._already_current(dg.build_op_context(), standards.QUESTION_COVERAGE_VERDICT, config, HEAD_RUN)
    assert _ticks(shared_connection, COVERAGE_CHECK) == [(False, EXECUTED_AT), (True, EXECUTED_AT), (True, FALLBACK_AT)]


def test_a_full_run_without_a_lane_failure_turns_the_red_verdict_green_again(monkeypatch, shared_connection) -> None:
    assert not _execute_job(
        monkeypatch, shared_connection, HEAD_JOB, FAILED, universe=UNIQUE_UNIVERSE, real_verdicts=True
    ).success
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (False, EXECUTED_AT)

    rerun = _execute_job(
        monkeypatch,
        shared_connection,
        HEAD_JOB,
        VALID,
        universe=UNIQUE_UNIVERSE,
        executed_at=FALLBACK_AT,
        real_verdicts=True,
    )

    assert rerun.success
    assert _health_verdict(shared_connection, COVERAGE_CHECK) == (True, FALLBACK_AT)
    assert _stored_analyst_rows(shared_connection) == {issuer_id: ("available", []) for issuer_id in TICKERS}


def test_a_total_failure_turns_the_coverage_verdict_of_its_own_universe_red(monkeypatch, shared_connection) -> None:
    """The fallback and the sensor run the head-reports job for `topt` too: the red row must say `topt`."""
    result = _execute_job(monkeypatch, shared_connection, HEAD_JOB, FAILED, universe="topt")

    assert not result.success
    assert [v["check"] for v in shared_connection.verdicts if v["ok"] is False] == ["question_coverage@topt"]
    assert {v["check"] for v in shared_connection.verdicts} == {"theme_purity@topt", "question_coverage@topt"}


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_a_rerun_with_a_failing_fetch_keeps_the_available_rows_and_still_ends_red(
    monkeypatch, shared_connection, job_name
) -> None:
    """A failed fetch must not replace the good row an earlier run of the same head wrote."""
    assert _execute_job(monkeypatch, shared_connection, job_name, VALID).success

    rerun = _execute_job(monkeypatch, shared_connection, job_name, FAILED)

    assert not rerun.success, "the failure is still counted"
    summary = json.loads(rerun.output_for_node("run_analyst_ratings"))
    assert summary["fetch_errors"] == 3
    assert _stored_analyst_rows(shared_connection) == {issuer_id: ("available", []) for issuer_id in TICKERS}


RED_SUMMARY = "failed: analyst ratings fetch failed for 3 of 3 tickers"
LANE_MESSAGE = "analyst ratings fetch failed for 3 of 3 tickers; first error: DDOG: X: first failure"


def _lane_summary(**extra: Any) -> str:
    return json.dumps({"universe": QQQ, "executed_at": EXECUTED_AT, "rows": 3, "fetch_errors": 3, **extra})


@pytest.mark.parametrize("universe", [QQQ, "topt"])
def test_the_terminal_op_records_a_red_verdict_then_raises_the_summarys_failure(monkeypatch, universe) -> None:
    """#771: a red run must also be a red verdict. The verdict text carries counts, no error text.
    The verdict names the universe of the run, whichever it is."""
    from data_engine.lanes.standards import fail_if_a_lane_failed
    from data_engine.quality import nightly_verdicts

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: rows.append({"check": name, **row}))

    with pytest.raises(RuntimeError) as raised:
        fail_if_a_lane_failed(
            dg.build_op_context(),
            _lane_summary(universe=universe, lane_failure=LANE_MESSAGE),
            json.dumps({"report_id": "r"}),
        )

    assert str(raised.value) == LANE_MESSAGE
    [row] = rows
    assert (row["check"], row["ok"]) == (f"question_coverage@{universe}", False)
    assert row["summary"] == RED_SUMMARY
    assert row["ran_at"] == datetime.fromisoformat(EXECUTED_AT), "dated by the tick, like the green coverage verdict"


def test_the_terminal_op_writes_no_verdict_and_does_not_raise_without_a_lane_failure(monkeypatch) -> None:
    from data_engine.lanes.standards import REPORTS_CURRENT, fail_if_a_lane_failed
    from data_engine.quality import nightly_verdicts

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: rows.append({"check": name, **row}))
    report = json.dumps({"report_id": "r"})

    fail_if_a_lane_failed(dg.build_op_context(), _lane_summary(), report)
    fail_if_a_lane_failed(dg.build_op_context(), json.dumps({REPORTS_CURRENT: HEAD_RUN}), report)

    assert rows == []


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_a_total_failure_commits_the_rows_then_turns_the_coverage_verdict_red(
    monkeypatch, shared_connection, job_name
) -> None:
    """Both jobs, through the deployed ops: the coverage op writes its green verdict, as the
    report is persisted. The terminal op then writes the red one, which is the newest row."""
    result = _execute_job(monkeypatch, shared_connection, job_name, FAILED)

    assert not result.success
    verdict_name = f"question_coverage@{QQQ}"
    assert shared_connection.events == [
        "commit",  # theme purity
        f"verdict:theme_purity@{QQQ}:True",
        "commit",  # supply chain
        "commit",  # analyst ratings: the unavailable rows are committed BEFORE the run fails
        "commit",  # coverage report
        f"verdict:{verdict_name}:True",
        f"verdict:{verdict_name}:False",
    ]
    red = shared_connection.verdicts[-1]
    assert (red["check"], red["ok"], red["summary"]) == (verdict_name, False, RED_SUMMARY)
    assert red["run_id"] == result.run_id
    assert "first failure" not in red["summary"], "the verdict is public: no exception text"


@pytest.mark.parametrize("job_name", JOB_NAMES)
def test_the_terminal_op_follows_coverage_and_the_analyst_summary(job_name) -> None:
    from data_engine.lanes import standards

    job = getattr(standards, job_name)
    structure = job.graph.dependency_structure
    upstream = {
        handle.input_name: [output.node_name for output in outputs]
        for handle, outputs in structure.input_to_upstream_outputs_for_node("fail_if_a_lane_failed").items()
    }
    assert upstream == {"analyst_summary": ["run_analyst_ratings"], "coverage_summary": ["run_question_coverage"]}
