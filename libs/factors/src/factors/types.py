"""Core value types shared by every factor.

Every fact that reaches a factor is a (entity_id, value, unit_family, confidence,
as_of) tuple — provenance stays in raw/staging (via raw_ref), never in factor
logic (init.md Section 1, rule 3).
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from truealpha_contracts.metrics import METRICS, UnitFamily

# Matches the data availability matrix (init.md Section 8): every factor return value
# carries this field so the App and LLM answers can show whether the number behind
# them has been verified against a real sample.
DataAvailability = Literal["verified", "unverified"]

__all__ = [
    "DataAvailability",
    "Fact",
    "FactorError",
    "FactorResult",
    "GrowthConvention",
    "InsufficientDataError",
    "UnitFamily",
]


class GrowthConvention(StrEnum):
    """PEG growth-rate conventions — switchable, since different conventions can
    point to different conclusions (init.md Section 0)."""

    ANALYST_CONSENSUS = "analyst_consensus"
    HISTORICAL_CAGR = "historical_cagr"
    COMPANY_GUIDANCE = "company_guidance"


class Fact(BaseModel):
    """A single point-in-time fact as seen by a factor.

    `as_of` is the transaction-time cutoff the fact was resolved at — the staging
    layer has already applied the point-in-time query (init.md Section 6), so a
    factor never re-decides which vintage to use. `metric` must name an entry in
    the canonical registry (`truealpha_contracts.metrics.METRICS`, the single
    source of truth for what a field IS and its unit) and `unit_family` must
    match that registration — a factor never has to guess or re-declare it.
    """

    entity_id: str  # staging.kg_entities.id (unified_id)
    metric: str
    value: Decimal | None
    unit_family: UnitFamily
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    fiscal_period: str | None = None

    @model_validator(mode="after")
    def validate_registered_metric(self) -> "Fact":
        spec = METRICS.get(self.metric)
        if spec is None:
            raise ValueError(f"unregistered metric {self.metric!r} — add it to truealpha_contracts.metrics.METRICS")
        if self.unit_family is not spec.unit_family:
            raise ValueError(
                f"metric {self.metric!r} is registered as unit_family={spec.unit_family!r}, got {self.unit_family!r}"
            )
        return self


class FactorResult(BaseModel):
    """What every factor returns. Confidence is mandatory in and out."""

    factor: str
    entity_id: str
    value: Decimal | None
    unit_family: UnitFamily
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    data_availability: DataAvailability = "unverified"
    # Explicit gaps beat silent drops (e.g. module 2 flags missing headcount).
    flags: list[str] = Field(default_factory=list)


class FactorError(Exception):
    """Base class for factor-domain errors."""


class InsufficientDataError(FactorError):
    """Raised when required input facts are missing — never silently return a value."""

    def __init__(self, factor: str, entity_id: str, missing: list[str]):
        self.factor = factor
        self.entity_id = entity_id
        self.missing = missing
        super().__init__(f"{factor}({entity_id}): missing inputs {missing}")
