"""Core value types and shared helpers across base, feeder, and composite factors."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from truealpha_contracts.measure import Measure, MeasureUnit
from truealpha_contracts.metrics import METRICS, UnitFamily

from factors.measure import UnitMismatchError, default_unit_for_metric, times

DataAvailability = Literal["verified", "unverified"]

__all__ = [
    "DataAvailability",
    "Fact",
    "FactorError",
    "FactorResult",
    "GrowthConvention",
    "InsufficientDataError",
    "UnitFamily",
    "market_multiple_factor",
    "refused_factor_result",
    "sole_fact",
]


class GrowthConvention(StrEnum):
    ANALYST_CONSENSUS = "analyst_consensus"
    HISTORICAL_CAGR = "historical_cagr"
    COMPANY_GUIDANCE = "company_guidance"


class Fact(BaseModel):
    entity_id: str
    metric: str
    value: Decimal | None
    unit_family: UnitFamily
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    fiscal_period: str | None = None
    unit: MeasureUnit | None = None

    @model_validator(mode="after")
    def validate_registered_metric(self) -> Fact:
        spec = METRICS.get(self.metric)
        if spec is None:
            raise ValueError(f"unregistered metric {self.metric!r} — add it to truealpha_contracts.metrics.METRICS")
        if self.unit_family is not spec.unit_family:
            raise ValueError(
                f"metric {self.metric!r} is registered as unit_family={spec.unit_family!r}, got {self.unit_family!r}"
            )
        return self

    def to_measure(self) -> Measure:
        if self.value is None:
            raise ValueError(f"cannot build Measure from Fact({self.metric!r}) with value=None")
        resolved_unit = (
            self.unit
            if self.unit is not None
            else default_unit_for_metric(self.metric, self.unit_family, self.entity_id)
        )
        return Measure(
            value=self.value,
            unit=resolved_unit,
            period=self.fiscal_period,
            knowable_at=self.as_of,
            subject=self.entity_id,
        )


class FactorResult(BaseModel):
    factor: str
    entity_id: str
    value: Decimal | None
    unit_family: UnitFamily
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    data_availability: DataAvailability = "unverified"
    flags: list[str] = Field(default_factory=list)


class FactorError(Exception):
    """Base class for factor-domain errors."""


class InsufficientDataError(FactorError):
    def __init__(self, factor: str, entity_id: str, missing: list[str]):
        self.factor = factor
        self.entity_id = entity_id
        self.missing = missing
        super().__init__(f"{factor}({entity_id}): missing inputs {missing}")


def sole_fact(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    """Return the single PIT-resolved Fact for (entity_id, metric), or raise on ambiguity."""
    matches = [f for f in facts if f.entity_id == entity_id and f.metric == metric]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple PIT-resolved facts for metric {metric!r}")
    return matches[0] if matches else None


def refused_factor_result(
    factor_name: str,
    entity_id: str,
    as_of: datetime,
    flags: Sequence[str],
    *,
    unit_family: UnitFamily = UnitFamily.RATIO,
    dedupe_sorted: bool = False,
) -> FactorResult:
    """Build a standard unavailable FactorResult with confidence=0 and value=None."""
    return FactorResult(
        factor=factor_name,
        entity_id=entity_id,
        value=None,
        unit_family=unit_family,
        confidence=Decimal("0"),
        as_of=as_of,
        data_availability="unverified",
        flags=sorted(set(flags)) if dedupe_sorted else list(flags),
    )


def market_multiple_factor(
    facts: Sequence[Fact],
    *,
    factor_name: str,
    entity_id: str,
    as_of: datetime,
    denominator_metric: str,
    missing_denominator_flag: str,
    nonpositive_denominator_flag: str,
) -> FactorResult:
    """Shared `(price * shares_outstanding) / denominator` computation for feeder multiples."""
    price_fact = sole_fact(facts, entity_id, "price")
    shares_fact = sole_fact(facts, entity_id, "shares_outstanding")
    denom_fact = sole_fact(facts, entity_id, denominator_metric)
    missing: list[str] = []
    if price_fact is None or price_fact.value is None or shares_fact is None or shares_fact.value is None:
        missing.append("missing_market_value_input")
    if denom_fact is None or denom_fact.value is None:
        missing.append(missing_denominator_flag)
    if missing:
        return refused_factor_result(factor_name, entity_id, as_of, missing, dedupe_sorted=True)
    assert price_fact is not None and shares_fact is not None and denom_fact is not None
    assert price_fact.value is not None and shares_fact.value is not None and denom_fact.value is not None
    if price_fact.value <= 0 or shares_fact.value <= 0:
        return refused_factor_result(factor_name, entity_id, as_of, ["nonpositive_market_value"])
    if denom_fact.value <= 0:
        return refused_factor_result(factor_name, entity_id, as_of, [nonpositive_denominator_flag])
    try:
        market_value = times(price_fact.to_measure(), shares_fact.to_measure()).value
    except UnitMismatchError as exc:
        return refused_factor_result(factor_name, entity_id, as_of, [exc.reason])
    return FactorResult(
        factor=factor_name,
        entity_id=entity_id,
        value=market_value / denom_fact.value,
        unit_family=UnitFamily.RATIO,
        confidence=min(price_fact.confidence, shares_fact.confidence, denom_fact.confidence),
        as_of=as_of,
        data_availability="unverified",
    )
