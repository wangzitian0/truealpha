from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.research import (
    StrategyExecutionPolicy,
    StrategyTotalReturnPolicy,
    StrategyTransactionCostPolicy,
    TargetPsSelection,
    ValuationTier,
)


def test_valuation_tier_and_target_ps_selection_values_are_stable() -> None:
    assert tuple(tier.value for tier in ValuationTier) == ("traditional", "tech", "large_model_native")
    assert tuple(sel.value for sel in TargetPsSelection) == ("band_midpoint", "band_lower_bound", "band_upper_bound")


def test_strategy_execution_and_cost_policies_enforce_strict_bounds() -> None:
    execution = StrategyExecutionPolicy(
        rule_id="next_eligible_market_session_open",
        lag_sessions=1,
        price_field="unadjusted_open",
        unfilled_order_behavior="remain_cash",
    )
    cost = StrategyTransactionCostPolicy(
        rule_id="per_side_notional_basis_points",
        basis_points=Decimal("10"),
    )
    total_return = StrategyTotalReturnPolicy(
        rule_id="unadjusted_bars_explicit_corporate_actions",
        price_basis="unadjusted",
        corporate_action_processing="explicit_event_lifecycle_once",
        adjusted_prices_affect_returns=False,
    )
    assert execution.lag_sessions == 1
    assert cost.basis_points == Decimal("10")
    assert total_return.adjusted_prices_affect_returns is False

    with pytest.raises(ValidationError):
        StrategyTransactionCostPolicy(
            rule_id="per_side_notional_basis_points",
            basis_points=Decimal("-1"),
        )
