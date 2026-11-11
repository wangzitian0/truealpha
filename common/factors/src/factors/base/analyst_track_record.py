"""Module 4: Analyst track record and consensus (#771)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import FactorResult, UnitFamily, refused_factor_result

_ZERO = Decimal(0)


@dataclass(frozen=True)
class AnalystRatingItem:
    analyst_id: str
    rating: int
    target_price: Decimal | None = None
    confidence: Decimal = _ZERO


@dataclass(frozen=True)
class AnalystTrackRecord:
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
    if not ratings:
        return AnalystTrackRecord(
            entity_id,
            refused_factor_result("analyst_track_record", entity_id, as_of, ["no_analyst_coverage"]),
            None,
            0,
            0,
            0,
            0,
        )
    valid = [r for r in ratings if 1 <= r.rating <= 5]
    if not valid:
        return AnalystTrackRecord(
            entity_id,
            refused_factor_result("analyst_track_record", entity_id, as_of, ["invalid_ratings_range"]),
            None,
            len(ratings),
            0,
            0,
            0,
        )
    count = len(valid)
    mean_rating = sum((Decimal(r.rating) for r in valid), _ZERO) / Decimal(count)
    confidences = [r.confidence for r in valid if r.confidence > _ZERO]
    avg_confidence = (sum(confidences, _ZERO) / Decimal(len(confidences))) if confidences else Decimal("0.8")
    return AnalystTrackRecord(
        entity_id=entity_id,
        result=FactorResult(
            factor="analyst_track_record",
            entity_id=entity_id,
            value=mean_rating,
            unit_family=UnitFamily.RATIO,
            confidence=avg_confidence,
            as_of=as_of,
            data_availability="unverified",
            flags=[],
        ),
        consensus_rating=mean_rating,
        ratings_count=count,
        buy_count=sum(1 for r in valid if r.rating >= 4),
        hold_count=sum(1 for r in valid if r.rating == 3),
        sell_count=sum(1 for r in valid if r.rating <= 2),
    )
