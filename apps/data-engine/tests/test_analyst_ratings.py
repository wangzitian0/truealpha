"""Unit tests for analyst_ratings producer, materializer, and universe runner (#771)."""

from __future__ import annotations

import json
import logging
import os
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
from data_engine.config import settings
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
    assert result.lane_failure() is None, "a partial failure is not a lane failure"

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


def test_the_op_commits_and_reports_a_total_failure_without_raising(monkeypatch) -> None:
    """#771: the lane failure travels in the summary. The op that measures the lane (coverage)
    must still run, so this op does not raise; the terminal op `fail_if_a_lane_failed` does."""
    result, events = _run_op(
        monkeypatch, {"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"}
    )

    assert result.success
    summary = json.loads(result.output_for_node("run_analyst_ratings"))
    assert summary["rows"] == 3
    assert summary["fetch_errors"] == 3
    assert "3 of 3" in summary["lane_failure"]
    assert "first failure" in summary["lane_failure"]
    assert events == ["execute:issuer:ddog", "execute:issuer:nice", "execute:issuer:shop", "commit", "close"], (
        "the unavailable rows are committed before the op returns"
    )


def test_the_op_succeeds_on_a_partial_failure(monkeypatch) -> None:
    result, events = _run_op(
        monkeypatch, {"US.DDOG": _sample("DDOG"), "US.NICE": "second failure", "US.SHOP": _sample("SHOP")}
    )

    assert result.success
    summary = json.loads(result.output_for_node("run_analyst_ratings"))
    assert summary["rows"] == 3
    assert summary["fetch_errors"] == 1
    assert "lane_failure" not in summary
    assert events == ["execute:issuer:ddog", "execute:issuer:nice", "execute:issuer:shop", "commit", "close"]


def test_an_unreachable_opend_still_records_honest_unavailable_rows(monkeypatch) -> None:
    """Without a context there is no fetch, so there is no fetch error to fail on."""
    result, events = _run_op(monkeypatch, {}, opend_connect_fails=True)

    assert result.success
    assert events == ["execute:issuer:ddog", "execute:issuer:nice", "execute:issuer:shop", "commit", "close"]


# --- the lane fails AFTER the coverage report is written (#771, the #1016 pattern) --------
#
# The coverage report is the instrument that measures every lane. A lane that fails for every
# ticker must not stop the instrument: the report is written first, and only then does the run
# end as FAILURE. These tests run the DEPLOYED jobs, with the real analyst op, the real coverage
# op and a real Postgres. The connection is shared and rolled back at the end: `commit` is
# counted, never executed, so no row outlives the test.
#
# These tests do not assert a Q4 coverage value. The coverage cells here use the issuer ids of
# the analyst rows. Production joins `issuer:lei:...` analyst rows to UUID wide rows, so Q4
# reads `unavailable:no_row` there (#1079). A Q4 assertion would pass only because of the fakes.
# What this PR controls is asserted instead: the persisted analyst rows, the lane summary, the run.

HEAD_RUN = "capture-run:" + "7" * 64
EXECUTED_AT = "2026-10-06T04:00:00+00:00"
FAILED = {"US.DDOG": "first failure", "US.NICE": "second failure", "US.SHOP": "third failure"}
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


def _execute_job(monkeypatch, shared_connection, job_name: str, responses: dict[str, Any]):
    """Execute one deployed job over a faked world: real analyst and coverage ops, real SQL."""
    from data_engine.datahub import question_coverage
    from data_engine.datahub.production_topt import theme_purity
    from data_engine.datahub.standards import planner, supply_chain_extraction
    from data_engine.lanes import standards
    from data_engine.quality import nightly_verdicts

    head = question_coverage.GovernedHead("universe:qqq-us-2026-06-30", HEAD_RUN, datetime(2026, 10, 6, tzinfo=UTC))
    issuers = [SimpleNamespace(issuer_id=i, ticker=t) for i, t in TICKERS.items()]
    ctx = _FakeQuoteContext(responses)

    @contextmanager
    def fake_connect():
        yield ctx

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: shared_connection)
    monkeypatch.setattr(question_coverage, "governed_head", lambda _c, **_k: head)
    monkeypatch.setattr(question_coverage, "stored_report_run", lambda _c, _universe: None)
    monkeypatch.setattr(question_coverage, "declared_environment", lambda _c: "test")
    monkeypatch.setattr(
        question_coverage, "gppe_cells", lambda _c, _run: tuple(question_coverage.Cell(i, True) for i in TICKERS)
    )
    monkeypatch.setattr(planner, "universe_issuers", lambda *_a, **_k: issuers)
    monkeypatch.setattr(standards, "universe_issuers", lambda *_a, **_k: issuers)
    monkeypatch.setattr(theme_purity, "materialize_theme_purity", lambda _c, **_k: ())
    monkeypatch.setattr(supply_chain_extraction, "materialize_universe_supply_chain_exposure", lambda _c, **_k: 0)

    def record_verdict(name: str, **row: Any) -> None:
        shared_connection.verdicts.append({"check": name, **row})
        shared_connection.events.append(f"verdict:{name}:{row['ok']}")

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
            "universe-list:qqq", EXECUTED_AT, run_key="test", only_if_stale=False
        ).run_config
    else:
        run_config = standards.backfill_run_config(EXECUTED_AT, "universe-list:qqq")
    return getattr(standards, job_name).execute_in_process(run_config=run_config, raise_on_error=False)


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
def test_a_partial_failure_leaves_the_run_green_and_the_rows_name_the_error(
    monkeypatch, shared_connection, job_name
) -> None:
    responses = {"US.DDOG": _sample("DDOG"), "US.NICE": "second failure", "US.SHOP": _sample("SHOP")}
    result = _execute_job(monkeypatch, shared_connection, job_name, responses)

    assert result.success
    assert _stored_analyst_rows(shared_connection) == {
        "issuer:ddog": ("available", []),
        "issuer:nice": ("unavailable", ["fetch_error:MoomooConnectionError"]),
        "issuer:shop": ("available", []),
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


QQQ = "universe-list:qqq"
RED_SUMMARY = "failed: analyst ratings fetch failed for 3 of 3 tickers"
LANE_MESSAGE = "analyst ratings fetch failed for 3 of 3 tickers; first error: DDOG: X: first failure"


def _lane_summary(**extra: Any) -> str:
    return json.dumps({"universe": QQQ, "executed_at": EXECUTED_AT, "rows": 3, "fetch_errors": 3, **extra})


def test_the_terminal_op_records_a_red_verdict_then_raises_the_summarys_failure(monkeypatch) -> None:
    """#771: a red run must also be a red verdict. The verdict text carries counts, no error text."""
    from data_engine.lanes.standards import fail_if_a_lane_failed
    from data_engine.quality import nightly_verdicts

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: rows.append({"check": name, **row}))

    with pytest.raises(RuntimeError) as raised:
        fail_if_a_lane_failed(
            dg.build_op_context(), _lane_summary(lane_failure=LANE_MESSAGE), json.dumps({"report_id": "r"})
        )

    assert str(raised.value) == LANE_MESSAGE
    [row] = rows
    assert (row["check"], row["ok"]) == (f"question_coverage@{QQQ}", False)
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
