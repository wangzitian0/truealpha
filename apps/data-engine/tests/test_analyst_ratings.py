"""Unit tests for analyst_ratings producer, materializer, and universe runner (#771)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import moomoo
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

    A payload that is an `Exception` is raised, as a socket timeout would be.
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
    assert res == 1
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
    assert res == 1
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
    assert total == 2
    assert [params[1] for _, params in conn.executed] == ["issuer:ddog", "issuer:duol"]
    assert [params[11] for _, params in conn.executed] == ["available", "available"]
    assert [params[4] for _, params in conn.executed] == [35, 7]
