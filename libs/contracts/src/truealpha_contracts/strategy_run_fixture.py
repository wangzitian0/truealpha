"""Fixture-backed `StrategyRunReadRepository` — see `#347`.

Reads the checked-in, deterministic mirror of `make strategy-smoke`'s output
(`data/strategy_run_preview.v1.json`) rather than a Postgres `mart` row. This
is explicitly the provisional data source named in `#347`'s acceptance
criteria; a Postgres-backed sibling implementing the same protocol is
follow-up work gated on `#26` landing real mart persistence.
"""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources
from typing import Any

from truealpha_contracts.access import AccessContext
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)

_FIXTURE_PACKAGE = "truealpha_contracts.data"
_FIXTURE_NAME = "strategy_run_preview.v1.json"


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _decision_from_json(payload: dict[str, Any]) -> StrategyRunDecision:
    return StrategyRunDecision(
        issuer_id=payload["issuer_id"],
        cutoff_at=payload["cutoff_at"],
        outcome=StrategyRunOutcome(payload["outcome"]),
        eligible=payload["eligible"],
        tier=ValuationTier(payload["tier"]) if payload["tier"] is not None else None,
        capital_adjusted_labor_efficiency=_decimal_or_none(payload["capital_adjusted_labor_efficiency"]),
        current_price_to_sales=_decimal_or_none(payload["current_price_to_sales"]),
        target_price_to_sales=_decimal_or_none(payload["target_price_to_sales"]),
        valuation_gap=_decimal_or_none(payload["valuation_gap"]),
        confidence=_decimal_or_none(payload["confidence"]),
        exclusion_reason=payload["exclusion_reason"],
        rank=payload["rank"],
        target_weight=_decimal_or_none(payload["target_weight"]),
    )


class FixtureStrategyRunRepository:
    """Loads the one checked-in `large_model_value_v0` preview fixture."""

    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunReport | StrategyRunUnavailable:
        del context  # reserved for a future authorization decision; unused today
        raw = resources.files(_FIXTURE_PACKAGE).joinpath(_FIXTURE_NAME).read_bytes()
        payload = json.loads(raw)
        if strategy_id != payload["strategy_id"]:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="unknown_strategy_id")
        return StrategyRunReport(
            strategy_id=payload["strategy_id"],
            source=payload["source"],
            corpus_sha256=payload["corpus_sha256"],
            decisions=tuple(_decision_from_json(d) for d in payload["decisions"]),
            golden_mismatches=tuple(payload["golden_mismatches"]),
        )
