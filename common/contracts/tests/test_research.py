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

pytestmark = pytest.mark.contract


def test_research_enums_and_policies() -> None:
    assert tuple(t.value for t in ValuationTier) == ("traditional", "tech", "large_model_native")
    assert tuple(s.value for s in TargetPsSelection) == ("band_midpoint", "band_lower_bound", "band_upper_bound")
    assert (
        StrategyExecutionPolicy(
            rule_id="next_eligible_market_session_open",
            lag_sessions=1,
            price_field="unadjusted_open",
            unfilled_order_behavior="remain_cash",
        ).lag_sessions
        == 1
    )
    assert StrategyTransactionCostPolicy(
        rule_id="per_side_notional_basis_points", basis_points=Decimal("10")
    ).basis_points == Decimal("10")
    assert not StrategyTotalReturnPolicy(
        rule_id="unadjusted_bars_explicit_corporate_actions",
        price_basis="unadjusted",
        corporate_action_processing="explicit_event_lifecycle_once",
        adjusted_prices_affect_returns=False,
    ).adjusted_prices_affect_returns
    with pytest.raises(ValidationError):
        StrategyTransactionCostPolicy(rule_id="per_side_notional_basis_points", basis_points=Decimal("-1"))
