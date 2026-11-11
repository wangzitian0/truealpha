"""Fixture-backed `StrategyRunReadRepository` (#347)."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from importlib import resources
from typing import Any

from pydantic import ValidationError

from truealpha_contracts.access import AccessContext
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)

_FIXTURE_CORRUPTION_ERRORS = (json.JSONDecodeError, KeyError, ValueError, TypeError, InvalidOperation, ValidationError)
_DEC_KEYS = (
    "capital_adjusted_labor_efficiency",
    "current_price_to_sales",
    "target_price_to_sales",
    "current_price_to_gross_profit",
    "target_price_to_gross_profit",
    "valuation_gap",
    "peg",
    "confidence",
    "target_weight",
)


def _decision_from_json(payload: dict[str, Any]) -> StrategyRunDecision:
    dec = {k: (Decimal(payload[k]) if payload.get(k) is not None else None) for k in _DEC_KEYS}
    return StrategyRunDecision(
        issuer_id=payload["issuer_id"],
        cutoff_at=payload["cutoff_at"],
        outcome=StrategyRunOutcome(payload["outcome"]),
        eligible=payload["eligible"],
        tier=ValuationTier(payload["tier"]) if payload["tier"] is not None else None,
        peg_rank=payload.get("peg_rank"),
        labor_efficiency_metric=payload.get("labor_efficiency_metric"),
        exclusion_reason=payload["exclusion_reason"],
        rank=payload["rank"],
        **dec,
    )


class FixtureStrategyRunRepository:
    def get_latest(self, *, strategy_id: str, context: AccessContext) -> StrategyRunReport | StrategyRunUnavailable:
        del context
        try:
            raw = resources.files("truealpha_contracts.data").joinpath("strategy_run_preview.v1.json").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError):
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="fixture_missing")
        try:
            payload = json.loads(raw)
            if strategy_id != payload["strategy_id"]:
                return StrategyRunUnavailable(strategy_id=strategy_id, reason="unknown_strategy_id")
            return StrategyRunReport(
                strategy_id=payload["strategy_id"],
                source=payload["source"],
                corpus_sha256=payload["corpus_sha256"],
                decisions=tuple(_decision_from_json(d) for d in payload["decisions"]),
                golden_mismatches=tuple(payload["golden_mismatches"]),
                strategy_run_id="strategy_smoke_fixture",
                governed=False,
            )
        except _FIXTURE_CORRUPTION_ERRORS:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="fixture_hash_mismatch")
