"""Unit tests for analyst_ratings producer and materializer (#771)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
from data_engine.datahub.analyst_ratings import (
    capture_ticker_analyst_ratings,
    materialize_analyst_ratings,
)
from factors.base.analyst_track_record import (
    AnalystRatingItem,
    analyst_track_record,
)


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


def test_materialize_analyst_ratings_executes_insert() -> None:
    conn = _MockConnection()
    count = materialize_analyst_ratings(
        conn,
        run_id="run:1",
        ratings_data=[
            {
                "issuer_id": "issuer:aapl",
                "consensus_rating": Decimal("4.5"),
                "analysts_count": 32,
                "availability_status": "available",
                "reason_codes": [],
            }
        ],
    )
    assert count == 1
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "insert into mart.issuer_analyst_ratings" in sql
    assert len(params) == 14
    assert params[0] == "run:1"
    assert params[1] == "issuer:aapl"
    assert params[2] is not None  # cutoff
    assert params[3] == Decimal("4.5")  # consensus_rating
    assert params[4] == 32  # analysts_count
    assert params[11] == "available"
    assert params[12] == "verified"
    assert params[13] == "accepted"


def test_materialize_analyst_ratings_from_factor_record() -> None:
    conn = _MockConnection()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    ratings = [
        AnalystRatingItem("analyst:1", 5, confidence=Decimal("0.9")),
        AnalystRatingItem("analyst:2", 4, confidence=Decimal("0.8")),
    ]
    rec = analyst_track_record(ratings, entity_id="issuer:msft", as_of=now)
    count = materialize_analyst_ratings(conn, run_id="run:2", cutoff=now, ratings_data=[rec])
    assert count == 1
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    assert len(params) == 14
    assert params[0] == "run:2"
    assert params[1] == "issuer:msft"
    assert params[3] == Decimal("4.5")
    assert params[4] == 2
    assert params[5] == 2  # buy_count
    assert params[11] == "available"


def test_capture_ticker_analyst_ratings_success() -> None:
    conn = _MockConnection()
    mock_df = pd.DataFrame([{"consensus_rating": 4.2, "recommend_num": 25}])
    with patch("data_engine.sources.moomoo.get_analyst_consensus", return_value=(0, mock_df)):
        res = capture_ticker_analyst_ratings(
            ctx=MagicMock(),
            ticker="NVDA",
            company_id="issuer:nvda",
            connection=conn,
            run_id="run:test",
        )
    assert res == 1
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    assert len(params) == 14
    assert params[0] == "run:test"
    assert params[1] == "issuer:nvda"
    assert params[3] == Decimal("4.0")  # evaluated by analyst_track_record with ratings
    assert params[4] == 25
    assert params[11] == "available"
    assert params[12] == "verified"
    assert params[13] == "accepted"


def test_capture_ticker_analyst_ratings_unavailable_when_no_data() -> None:
    conn = _MockConnection()
    with patch("data_engine.sources.moomoo.get_analyst_consensus", return_value=(0, pd.DataFrame())):
        res = capture_ticker_analyst_ratings(
            ctx=MagicMock(),
            ticker="XYZ",
            company_id="issuer:xyz",
            connection=conn,
            run_id="run:test",
        )
    assert res == 1
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    assert len(params) == 14
    assert params[1] == "issuer:xyz"
    assert params[3] is None
    assert params[4] == 0
    assert params[11] == "unavailable"
    assert params[12] == "degraded"
    assert params[13] == "not_evaluated"
    assert params[9] == ["no_analyst_coverage"]
