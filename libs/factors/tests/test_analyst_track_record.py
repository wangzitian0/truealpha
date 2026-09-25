"""Unit tests for Module 4 factor: analyst_track_record."""

from datetime import UTC, datetime
from decimal import Decimal

from factors.base.analyst_track_record import (
    AnalystRatingItem,
    AnalystTrackRecord,
    analyst_track_record,
)


def test_analyst_track_record_refuses_when_no_ratings() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    res = analyst_track_record([], entity_id="issuer:test:1", as_of=now)
    assert isinstance(res, AnalystTrackRecord)
    assert res.consensus_rating is None
    assert res.result.data_availability == "unverified"
    assert "no_analyst_coverage" in res.result.flags
    assert res.ratings_count == 0


def test_analyst_track_record_computes_consensus() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    ratings = [
        AnalystRatingItem("analyst:ms", 5, confidence=Decimal("0.9")),
        AnalystRatingItem("analyst:gs", 4, confidence=Decimal("0.8")),
        AnalystRatingItem("analyst:jpm", 3, confidence=Decimal("0.85")),
    ]
    res = analyst_track_record(ratings, entity_id="issuer:aapl", as_of=now)
    assert isinstance(res, AnalystTrackRecord)
    assert res.consensus_rating is not None
    # (5 + 4 + 3) / 3 = 4.0
    assert res.consensus_rating == Decimal("4.0")
    assert res.ratings_count == 3
    assert res.buy_count == 2
    assert res.hold_count == 1
    assert res.sell_count == 0
    assert res.result.data_availability == "verified"
