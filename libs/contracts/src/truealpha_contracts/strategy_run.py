"""Provisional, read-only Core Strategy run reports shared by MCP and the App.

This module intentionally does **not** implement `#41`'s full seven-module
`ResearchReadRepository`. It defines the smallest typed slice needed to let a
Python (MCP) adapter and a TypeScript (App) adapter agree on one strategy's
run result while the real `mart` projection (`#26`) and the seven-module
read-contract freeze (`#41`, gated on `#40`) are still in progress. See `#347`
for the full rationale.

`StrategyRunReport` mirrors exactly the fields `apps/data-engine/scripts/
run_strategy_smoke.py` already produces (issuer/cutoff-keyed decisions with
outcome, tier, valuation gap, rank, target weight, and now confidence); it
performs no new computation. `outcome` and `exclusion_reason` remain loosely
typed (`StrategyRunOutcome` enum, but a free-form `str` for exclusion reason)
because the preview script does not yet bind every reason to the formal
`truealpha_contracts.strategy.ExclusionReason` enum (for example
``"unavailable_required_input"`` has no corresponding member there); widening
this to the frozen enum is follow-up work once the real #24/#25 candidates
close.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from truealpha_contracts.access import AccessContext
from truealpha_contracts.models import _require_aware
from truealpha_contracts.research import ValuationTier

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class StrategyRunOutcome(StrEnum):
    SELECTED = "selected"
    RANKED_BEYOND_SELECTION_COUNT = "ranked_beyond_selection_count"
    REJECTED_VALUATION_ABOVE_TIER_BAND = "rejected_valuation_above_tier_band"
    EXCLUDED = "excluded"


class StrategyRunDecision(_StrictFrozenModel):
    """One issuer's decision at one cutoff, verbatim from the strategy-smoke artifact."""

    issuer_id: str = Field(min_length=1)
    cutoff_at: datetime
    outcome: StrategyRunOutcome
    eligible: bool
    tier: ValuationTier | None = None
    capital_adjusted_labor_efficiency: Decimal | None = None
    current_price_to_sales: Decimal | None = None
    target_price_to_sales: Decimal | None = None
    valuation_gap: Decimal | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    exclusion_reason: str | None = None
    rank: int | None = Field(default=None, ge=1)
    target_weight: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("cutoff_at", mode="before")
    @classmethod
    def parse_cutoff_at(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("cutoff_at")
    @classmethod
    def validate_cutoff_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff_at")


class StrategyRunReport(_StrictFrozenModel):
    """A provisional strategy-run result, sourced from a checked-in fixture, not mart."""

    strategy_id: Literal["large_model_value_v0"]
    source: Literal["strategy_smoke_fixture"] = "strategy_smoke_fixture"
    corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    decisions: tuple[StrategyRunDecision, ...]
    golden_mismatches: tuple[str, ...] = ()


class StrategyRunUnavailable(_StrictFrozenModel):
    strategy_id: str = Field(min_length=1)
    reason: Literal["unknown_strategy_id", "fixture_missing", "fixture_hash_mismatch"]


class StrategyRunReadRepository(Protocol):
    """Read boundary shared by the MCP tool (`#348`) and the admin page (`#349`).

    ``context`` is accepted so callers never need a later signature break once
    a real authorization decision is wired in, but no implementation of this
    protocol shipped by `#347` evaluates it — it performs no policy decision.
    """

    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunReport | StrategyRunUnavailable: ...
