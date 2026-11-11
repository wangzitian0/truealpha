"""Valuation tier and strategy execution policy contracts."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ValuationTier(StrEnum):
    TRADITIONAL = "traditional"
    TECH = "tech"
    LARGE_MODEL_NATIVE = "large_model_native"


class TargetPsSelection(StrEnum):
    BAND_MIDPOINT = "band_midpoint"
    BAND_LOWER_BOUND = "band_lower_bound"
    BAND_UPPER_BOUND = "band_upper_bound"


class StrategyExecutionPolicy(_StrictFrozenModel):
    rule_id: Literal["next_eligible_market_session_open"]
    lag_sessions: Literal[1]
    price_field: Literal["unadjusted_open"]
    unfilled_order_behavior: Literal["remain_cash"]


class StrategyTransactionCostPolicy(_StrictFrozenModel):
    rule_id: Literal["per_side_notional_basis_points"]
    basis_points: Decimal = Field(ge=0, le=1000)


class StrategyTotalReturnPolicy(_StrictFrozenModel):
    rule_id: Literal["unadjusted_bars_explicit_corporate_actions"]
    price_basis: Literal["unadjusted"]
    corporate_action_processing: Literal["explicit_event_lifecycle_once"]
    adjusted_prices_affect_returns: Literal[False]


__all__ = [
    "StrategyExecutionPolicy",
    "StrategyTotalReturnPolicy",
    "StrategyTransactionCostPolicy",
    "TargetPsSelection",
    "ValuationTier",
]
