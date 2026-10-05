"""Unit tests for analyst_ratings producer, materializer, and universe runner (#771)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
from data_engine.datahub.analyst_ratings import (
    capture_ticker_analyst_ratings,
    materialize_analyst_ratings,
    materialize_universe_analyst_ratings,
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
    assert params[0] == "run:test"
    assert params[1] == "issuer:nvda"
    assert params[3] == Decimal("4.0")
    assert params[4] == 25
    assert params[11] == "available"
    assert params[12] == "verified"


def test_capture_ticker_analyst_ratings_anti_fabrication_on_null_or_empty() -> None:
    """Anti-fabrication / Anti-GREEN-WHILE-EMPTY:
    When consensus_rating is None or empty df is returned, must NOT fabricate rating=3 (Hold).
    Must strictly record unavailable with 'no_analyst_coverage'.
    """
    conn = _MockConnection()
    # Case 1: Empty DataFrame
    with patch("data_engine.sources.moomoo.get_analyst_consensus", return_value=(0, pd.DataFrame())):
        res = capture_ticker_analyst_ratings(
            ctx=MagicMock(),
            ticker="XYZ",
            company_id="issuer:xyz",
            connection=conn,
            run_id="run:test",
        )
    assert res == 1
    _, params_empty = conn.executed[0]
    assert params_empty[1] == "issuer:xyz"
    assert params_empty[3] is None, "Fabricated rating on empty df!"
    assert params_empty[4] == 0
    assert params_empty[11] == "unavailable"
    assert params_empty[9] == ["no_analyst_coverage"]

    # Case 2: DataFrame with None consensus_rating (e.g. recommend_num=0 or rating=None)
    mock_df_null = pd.DataFrame([{"consensus_rating": None, "recommend_num": 0}])
    with patch("data_engine.sources.moomoo.get_analyst_consensus", return_value=(0, mock_df_null)):
        res2 = capture_ticker_analyst_ratings(
            ctx=MagicMock(),
            ticker="ABC",
            company_id="issuer:abc",
            connection=conn,
            run_id="run:test",
        )
    assert res2 == 1
    _, params_null = conn.executed[1]
    assert params_null[1] == "issuer:abc"
    assert params_null[3] is None, "Fabricated rating on None consensus_rating!"
    assert params_null[4] == 0
    assert params_null[11] == "unavailable"
    assert params_null[9] == ["no_analyst_coverage"]


def test_materialize_universe_analyst_ratings_iterates_all_issuers() -> None:
    conn = _MockConnection()
    tickers = {"issuer:1": "NVDA", "issuer:2": "AAPL"}
    mock_df = pd.DataFrame([{"consensus_rating": 4.0, "recommend_num": 10}])
    with patch("data_engine.sources.moomoo.get_analyst_consensus", return_value=(0, mock_df)):
        total = materialize_universe_analyst_ratings(
            conn,
            run_id="run:uni",
            cutoff=datetime.now(tz=UTC),
            tickers=tickers,
        )
    assert total == 2
    assert len(conn.executed) == 2
