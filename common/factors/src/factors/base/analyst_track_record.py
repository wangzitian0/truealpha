"""Module 4: analyst ratings and consensus (#771)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import FactorResult, UnitFamily, refused_factor_result

_ZERO = Decimal(0)
_HOLD_BAND = Decimal("0.05")


@dataclass(frozen=True)
class AnalystRatingItem:
    analyst_id: str
    rating: int
    target_price: Decimal | None = None
    confidence: Decimal = _ZERO
    knowable_at: datetime | None = None
    outcome_end: datetime | None = None
    realized_return: Decimal | None = None


@dataclass(frozen=True)
class AnalystTrackRecord:
    entity_id: str
    result: FactorResult
    consensus_rating: Decimal | None
    ratings_count: int
    buy_count: int
    hold_count: int
    sell_count: int
    track_record_score: Decimal | None = None

    @property
    def value(self) -> Decimal | None:
        return self.result.value


def _score_matured_rating(item: AnalystRatingItem) -> Decimal:
    assert item.realized_return is not None
    if item.rating >= 4:
        return Decimal(1) if item.realized_return > _ZERO else _ZERO
    if item.rating <= 2:
        return Decimal(1) if item.realized_return < _ZERO else _ZERO
    return Decimal(1) if abs(item.realized_return) <= _HOLD_BAND else _ZERO


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
    knowable = [r for r in ratings if r.knowable_at is None or r.knowable_at <= as_of]
    if not knowable:
        return AnalystTrackRecord(
            entity_id,
            refused_factor_result("analyst_track_record", entity_id, as_of, ["not_knowable_at_cutoff"]),
            None,
            0,
            0,
            0,
            0,
        )
    valid = [r for r in knowable if 1 <= r.rating <= 5]
    if not valid:
        return AnalystTrackRecord(
            entity_id,
            refused_factor_result("analyst_track_record", entity_id, as_of, ["invalid_ratings_range"]),
            None,
            len(knowable),
            0,
            0,
            0,
        )
    count = len(valid)
    mean_rating = sum((Decimal(r.rating) for r in valid), _ZERO) / Decimal(count)
    matured = [
        r for r in valid if r.outcome_end is not None and r.outcome_end <= as_of and r.realized_return is not None
    ]
    track_score = sum((_score_matured_rating(r) for r in matured), _ZERO) / Decimal(len(matured)) if matured else None
    confidences = [r.confidence for r in valid if r.confidence > _ZERO]
    composite_confidence = min(confidences, default=_ZERO)
    return AnalystTrackRecord(
        entity_id=entity_id,
        result=FactorResult(
            factor="analyst_track_record",
            entity_id=entity_id,
            value=mean_rating,
            unit_family=UnitFamily.RATIO,
            confidence=composite_confidence,
            as_of=as_of,
            data_availability="unverified",
            flags=[],
        ),
        consensus_rating=mean_rating,
        ratings_count=count,
        buy_count=sum(1 for r in valid if r.rating >= 4),
        hold_count=sum(1 for r in valid if r.rating == 3),
        sell_count=sum(1 for r in valid if r.rating <= 2),
        track_record_score=track_score,
    )
