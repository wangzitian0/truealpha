"""Unit tests for analyst_ratings producer, materializer, and universe runner (#771)."""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dagster as dg
import moomoo
import psycopg
import pytest
from data_engine.datahub.analyst_ratings import (
    capture_ticker_analyst_ratings,
    materialize_analyst_ratings,
    materialize_universe_analyst_ratings,
)
from data_engine.sources import moomoo as mm
from data_engine.sources import moomoo_ledger as ledger
from factors.base.analyst_track_record import (
    AnalystRatingItem,
    analyst_track_record,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "moomoo"


@pytest.fixture(autouse=True)
def _isolated_moomoo_ledger(tmp_path, monkeypatch):
    """Run the real `_call` wrapper against a throwaway JSON ledger, never the staging table."""
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "json")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", 1000)
    ledger._recent_calls.clear()


def _sample(ticker: str) -> dict[str, Any]:
    """The `get_research_analyst_consensus` payload captured from the real OpenD gateway."""
    captured = json.loads((SAMPLES / f"{ticker}.json").read_text())["analyst_consensus"]
    assert captured["ok"] is True
    return captured["data"]


class _FakeQuoteContext:
    """Answers like the SDK: `(RET_OK, payload)` per code, `(RET_ERROR, message)` for a str.

    A payload that is an `Exception` is raised, as a socket timeout would be. A list is
    returned as it is, to stand for a payload that breaks the vendor contract.
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
        if isinstance(response, list):
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
    rec = analyst_track_record(ratings, entity_id="issuer:msft", as_of=now)
    count = materialize_analyst_ratings(
        conn,
        run_id="run:both",
        cutoff=now,
        ratings_data=[
            {
                "issuer_id": "issuer:aapl",
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
    assert params1[1] == "issuer:aapl"
    assert params1[3] == Decimal("4.5")
    assert params1[11] == "available"
    # Row 2: factor record
    _, params2 = conn.executed[1]
    assert params2[1] == "issuer:msft"
    assert params2[3] == Decimal("4.5")
    assert params2[4] == 2
    assert params2[11] == "available"


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
        company_id=f"issuer:{ticker.lower()}",
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
    assert params[1] == f"issuer:{ticker.lower()}"
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
        {"rating": 0, "total": 0},
        {"rating": 0, "total": 12},
        {"rating": 4, "total": 0},
        {"total": 5},
    ],
    ids=["empty", "unknown-rating-no-analysts", "unknown-rating", "no-analysts", "no-rating"],
)
def test_a_response_without_a_consensus_becomes_an_unavailable_row_with_a_reason(payload) -> None:
    """Anti-fabrication: no rating or no analysts never becomes a neutral rating."""
    conn = _MockConnection()
    ctx = _FakeQuoteContext({"US.XYZ": payload})
    res = capture_ticker_analyst_ratings(
        ctx=ctx,
        ticker="XYZ",
        company_id="issuer:xyz",
        connection=conn,
        run_id="run:test",
    )
    assert res.rows == 1
    assert res.failure is None, "no coverage is not a fetch error"
    _, params = conn.executed[0]
    assert params[1] == "issuer:xyz"
    assert params[3] is None, "Fabricated rating without a consensus!"
    assert params[4] == 0
    assert params[11] == "unavailable"
    assert params[9] == ["no_analyst_coverage"]


def test_the_universe_run_writes_one_row_per_issuer_from_real_responses() -> None:
    conn = _MockConnection()
    ctx = _FakeQuoteContext({"US.DDOG": _sample("DDOG"), "US.DUOL": _sample("DUOL")})
    total = materialize_universe_analyst_ratings(
        conn,
        run_id="run:uni",
        cutoff=datetime(2026, 10, 6, tzinfo=UTC),
        tickers={"issuer:ddog": "DDOG", "issuer:duol": "DUOL"},
        ctx=ctx,
    )
    assert total.rows == 2
    assert total.failures == ()
    assert [params[1] for _, params in conn.executed] == ["issuer:ddog", "issuer:duol"]
    assert [params[11] for _, params in conn.executed] == ["available", "available"]
    assert [params[4] for _, params in conn.executed] == [35, 7]


ERROR_LOGGER = "data_engine.datahub.analyst_ratings"
TICKERS = {"issuer:ddog": "DDOG", "issuer:nice": "NICE", "issuer:shop": "SHOP"}


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
    result.raise_if_every_ticker_failed()  # a partial failure does not raise

    rows = {params[1]: params for _, params in conn.executed}
    assert rows["issuer:ddog"][11] == "available"
    assert rows["issuer:shop"][11] == "available"
    failed = rows["issuer:nice"]
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
    conn, result = _run_universe(
        {"US.DDOG": _sample("DDOG"), "US.NICE": response}, {"issuer:ddog": "DDOG", "issuer:nice": "NICE"}
    )

    assert [(f.ticker, f.error.split(":")[0]) for f in result.failures] == [("NICE", error_type)]
    assert message in result.failures[0].error
    reason_codes = conn.executed[1][1][9]
    assert reason_codes == [f"fetch_error:{error_type}"]
    assert all(re.fullmatch(r"fetch_error:[A-Za-z]+", code) for code in reason_codes)
    records = _error_records(caplog)
    assert len(records) == 1
    assert message in records[0].getMessage()


def test_every_ticker_failing_persists_the_rows_and_then_raises() -> None:
    conn, result = _run_universe({"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"})

    assert result.rows == 3
    assert len(conn.executed) == 3, "the unavailable rows are persisted before the run fails"
    assert [params[11] for _, params in conn.executed] == ["unavailable"] * 3
    with pytest.raises(RuntimeError) as raised:
        result.raise_if_every_ticker_failed()
    text = str(raised.value)
    assert "3 of 3" in text
    assert "first failure" in text
    assert "second failure" not in text


def test_an_empty_universe_and_a_run_without_a_context_do_not_raise() -> None:
    conn = _MockConnection()
    empty = materialize_universe_analyst_ratings(
        conn, run_id="run:none", cutoff=datetime(2026, 10, 6, tzinfo=UTC), tickers={}, ctx=_FakeQuoteContext({})
    )
    no_ctx = materialize_universe_analyst_ratings(
        conn, run_id="run:none", cutoff=datetime(2026, 10, 6, tzinfo=UTC), tickers=TICKERS, ctx=None
    )
    assert (empty.rows, no_ctx.rows) == (0, 3)
    empty.raise_if_every_ticker_failed()
    no_ctx.raise_if_every_ticker_failed()
    assert [params[9] for _, params in conn.executed] == [["no_analyst_coverage"]] * 3


class _RecordingConnection:
    """A connection that records the order of `execute` and `commit`, shared across the op."""

    def __init__(self, events: list[str]):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_a):
        self.events.append("rollback" if exc_type else "close")
        return False

    def execute(self, sql, params=None):
        self.events.append(f"execute:{params[1]}")

    def commit(self) -> None:
        self.events.append("commit")


def _run_op(monkeypatch, responses: dict[str, Any], *, opend_connect_fails: bool = False):
    """Run the deployed op `run_analyst_ratings` as a one-op job, with moomoo and Postgres faked."""
    from data_engine.datahub import question_coverage
    from data_engine.datahub.standards import planner
    from data_engine.lanes.standards import run_analyst_ratings

    events: list[str] = []
    ctx = _FakeQuoteContext(responses)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _RecordingConnection(events))
    monkeypatch.setattr(
        question_coverage,
        "governed_head",
        lambda _c, **_k: question_coverage.GovernedHead("universe:test", "run:head", datetime(2026, 10, 6, tzinfo=UTC)),
    )
    monkeypatch.setattr(
        planner,
        "universe_issuers",
        lambda *_a, **_k: [SimpleNamespace(issuer_id=i, ticker=t) for i, t in TICKERS.items()],
    )

    @contextmanager
    def fake_connect():
        if opend_connect_fails:
            raise mm.MoomooConnectionError("OpenD not reachable")
        yield ctx

    monkeypatch.setattr(mm, "connect", fake_connect)

    @dg.op
    def upstream_summary() -> str:
        return "{}"

    @dg.job
    def one_op_job():
        run_analyst_ratings(upstream_summary())

    result = one_op_job.execute_in_process(
        run_config={"ops": {"run_analyst_ratings": {"config": {"executed_at": "2026-10-06T00:00:00+00:00"}}}},
        raise_on_error=False,
    )
    return result, events


def test_the_op_fails_after_it_commits_when_every_ticker_fails(monkeypatch) -> None:
    result, events = _run_op(
        monkeypatch, {"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"}
    )

    assert not result.success, "the job reports FAILURE, not 'published 3 analyst ratings rows'"
    [step_failure] = [e for e in result.all_events if e.is_step_failure]
    assert step_failure.step_key == "run_analyst_ratings"
    cause = step_failure.step_failure_data.error.cause
    assert cause is not None and cause.cls_name == "RuntimeError"
    assert "3 of 3" in cause.message
    assert "first failure" in cause.message
    assert events == ["execute:issuer:ddog", "execute:issuer:nice", "execute:issuer:shop", "commit", "close"], (
        "the unavailable rows are committed, and the connection closes without a rollback, before the op fails"
    )


def test_the_op_succeeds_on_a_partial_failure(monkeypatch) -> None:
    result, events = _run_op(
        monkeypatch, {"US.DDOG": _sample("DDOG"), "US.NICE": "second failure", "US.SHOP": _sample("SHOP")}
    )

    assert result.success
    assert json.loads(result.output_for_node("run_analyst_ratings"))["rows"] == 3
    assert events == ["execute:issuer:ddog", "execute:issuer:nice", "execute:issuer:shop", "commit", "close"]


def test_an_unreachable_opend_still_records_honest_unavailable_rows(monkeypatch) -> None:
    """Without a context there is no fetch, so there is no fetch error to fail on."""
    result, events = _run_op(monkeypatch, {}, opend_connect_fails=True)

    assert result.success
    assert events == ["execute:issuer:ddog", "execute:issuer:nice", "execute:issuer:shop", "commit", "close"]
