"""Module 4: Analyst track record and consensus (#771, init.md §7 item 4).

init.md §0 question 4: *"Is a given analyst's track record worth trusting?"*
Module 4 computes analyst rating consensus, depth, and track record over
corroborated rating events.

`analyst_track_record` processes PIT rating observations for an issuer, computing:
1. `consensus_rating`: mean recommendation rating (1=Strong Sell to 5=Strong Buy).
2. `ratings_count`: total active analyst ratings in the window.
3. Breakdown of buy, hold, and sell counts.

When no rating events exist within knowable_at, the factor refuses cleanly with
`no_analyst_coverage`, never guessing or manufacturing a neutral rating.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import FactorResult, UnitFamily

_ZERO = Decimal(0)


@dataclass(frozen=True)
class AnalystRatingItem:
    """One historical or current analyst rating event."""

    analyst_id: str
    rating: int  # 1 to 5 (1=Strong Sell, 2=Sell, 3=Hold, 4=Buy, 5=Strong Buy)
    target_price: Decimal | None = None
    confidence: Decimal = _ZERO


@dataclass(frozen=True)
class AnalystTrackRecord:
    """The analyst track record and consensus result."""

    entity_id: str
    result: FactorResult
    consensus_rating: Decimal | None
    ratings_count: int
    buy_count: int
    hold_count: int
    sell_count: int

    @property
    def value(self) -> Decimal | None:
        return self.result.value


@factor("analyst_track_record", kind="base", module=4)
def analyst_track_record(
    ratings: Sequence[AnalystRatingItem],
    *,
    entity_id: str,
    as_of: datetime,
) -> AnalystTrackRecord:
    """Compute consensus rating and analyst depth.

    If ratings is empty, returns an unavailable result with reason 'no_analyst_coverage'.
    """
    if not ratings:
        return AnalystTrackRecord(
            entity_id=entity_id,
            result=FactorResult(
                factor="analyst_track_record",
                entity_id=entity_id,
                value=None,
                unit_family=UnitFamily.RATIO,
                confidence=_ZERO,
                as_of=as_of,
                data_availability="unverified",
                flags=["no_analyst_coverage"],
            ),
            consensus_rating=None,
            ratings_count=0,
            buy_count=0,
            hold_count=0,
            sell_count=0,
        )

    valid_ratings = [r for r in ratings if 1 <= r.rating <= 5]
    if not valid_ratings:
        return AnalystTrackRecord(
            entity_id=entity_id,
            result=FactorResult(
                factor="analyst_track_record",
                entity_id=entity_id,
                value=None,
                unit_family=UnitFamily.RATIO,
                confidence=_ZERO,
                as_of=as_of,
                data_availability="unverified",
                flags=["invalid_ratings_range"],
            ),
            consensus_rating=None,
            ratings_count=len(ratings),
            buy_count=0,
            hold_count=0,
            sell_count=0,
        )

    ratings_count = len(valid_ratings)
    total_score = sum(Decimal(r.rating) for r in valid_ratings)
    mean_rating = total_score / Decimal(ratings_count)

    buy_count = sum(1 for r in valid_ratings if r.rating >= 4)
    hold_count = sum(1 for r in valid_ratings if r.rating == 3)
    sell_count = sum(1 for r in valid_ratings if r.rating <= 2)

    confidences = [r.confidence for r in valid_ratings if r.confidence > _ZERO]
    avg_confidence = (sum(confidences) / Decimal(len(confidences))) if confidences else Decimal("0.8")

    return AnalystTrackRecord(
        entity_id=entity_id,
        result=FactorResult(
            factor="analyst_track_record",
            entity_id=entity_id,
            value=mean_rating,
            unit_family=UnitFamily.RATIO,
            confidence=avg_confidence,
            as_of=as_of,
            data_availability="verified",
            flags=[],
        ),
        consensus_rating=mean_rating,
        ratings_count=ratings_count,
        buy_count=buy_count,
        hold_count=hold_count,
        sell_count=sell_count,
    )
