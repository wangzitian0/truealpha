"""Read-only Core Strategy run reports shared by MCP and the App (#347, #361)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.access import AccessContext
from truealpha_contracts.common import (
    AwareDatetime,
    Sha256,
    StrictFrozenModel,
)
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy import DefinitionReviewState, definition_review_state


class StrategyRunOutcome(StrEnum):
    SELECTED = "selected"
    RANKED_BEYOND_SELECTION_COUNT = "ranked_beyond_selection_count"
    REJECTED_VALUATION_ABOVE_TIER_BAND = "rejected_valuation_above_tier_band"
    EXCLUDED = "excluded"


class StrategyRunDecision(StrictFrozenModel):
    issuer_id: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    outcome: StrategyRunOutcome
    eligible: bool
    tier: ValuationTier | None = None
    capital_adjusted_labor_efficiency: Decimal | None = None
    current_price_to_sales: Decimal | None = None
    target_price_to_sales: Decimal | None = None
    current_price_to_gross_profit: Decimal | None = None
    target_price_to_gross_profit: Decimal | None = None
    valuation_gap: Decimal | None = None
    peg: Decimal | None = None
    peg_rank: int | None = None
    labor_efficiency_metric: str | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    exclusion_reason: str | None = None
    rank: int | None = Field(default=None, ge=1)
    target_weight: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("cutoff_at", mode="before")
    @classmethod
    def parse_cutoff_at(cls, value: object) -> object:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_one_valuation_basis(self) -> Self:
        if self.target_price_to_sales is not None and self.target_price_to_gross_profit is not None:
            raise ValueError("a decision targets the multiple of one valuation basis, not two")
        return self

    @property
    def valuation_basis(self) -> Literal["price_to_sales", "price_to_gross_profit"] | None:
        if self.target_price_to_gross_profit is not None:
            return "price_to_gross_profit"
        return "price_to_sales" if self.target_price_to_sales is not None else None


class StrategyRunDefinition(StrictFrozenModel):
    version: str
    content_sha256: Sha256
    review_state: DefinitionReviewState

    @classmethod
    def resolve(cls, *, version: str, content_sha256: str) -> Self:
        return cls(
            version=version,
            content_sha256=content_sha256,
            review_state=definition_review_state(version, content_sha256),
        )

    @model_validator(mode="after")
    def validate_review_state(self) -> Self:
        if self.review_state != definition_review_state(self.version, self.content_sha256):
            raise ValueError("review_state must be the registry state of this version and content hash")
        return self


class StrategyRunReport(StrictFrozenModel):
    strategy_id: Literal["large_model_value_v0"]
    source: Literal["strategy_smoke_fixture", "mart"] = "strategy_smoke_fixture"
    corpus_sha256: Sha256
    decisions: tuple[StrategyRunDecision, ...]
    golden_mismatches: tuple[str, ...] = ()
    strategy_run_id: str | None = None
    governed: bool = False
    definition: StrategyRunDefinition | None = None


class StrategyRunUnavailable(StrictFrozenModel):
    strategy_id: str = Field(min_length=1)
    reason: Literal[
        "unknown_strategy_id",
        "fixture_missing",
        "fixture_hash_mismatch",
        "fixture_not_found",
        "fixture_corrupted",
        "no_runs_recorded",
        "database_unavailable",
        "schema_mismatch",
        "empty_decisions",
    ]


class StrategyRunReadRepository(Protocol):
    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunReport | StrategyRunUnavailable: ...
