"""Test-only fixture-backed `StrategyRunReadRepository`."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.research import ValuationTier
from truealpha_runtime.access import AccessContext
from truealpha_runtime.strategy_run import (
    StrategyRunDecision,
    StrategyRunOutcome,
    StrategyRunReport,
    StrategyRunUnavailable,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "strategy_run_preview.v1.json"
_ERRORS = (json.JSONDecodeError, KeyError, ValueError, TypeError, InvalidOperation, ValidationError)
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


def _dec(p: dict[str, Any]) -> StrategyRunDecision:
    dec = {k: (Decimal(p[k]) if p.get(k) is not None else None) for k in _DEC_KEYS}
    return StrategyRunDecision(
        issuer_id=p["issuer_id"],
        cutoff_at=p["cutoff_at"],
        outcome=StrategyRunOutcome(p["outcome"]),
        eligible=p["eligible"],
        tier=ValuationTier(p["tier"]) if p["tier"] is not None else None,
        peg_rank=p.get("peg_rank"),
        labor_efficiency_metric=p.get("labor_efficiency_metric"),
        exclusion_reason=p["exclusion_reason"],
        availability_status=(
            AvailabilityStatus(p["availability_status"]) if p.get("availability_status") is not None else None
        ),
        rank=p["rank"],
        **dec,
    )


class FixtureStrategyRunRepository:
    def get_latest(
        self, *, strategy_id: str, context: AccessContext, universe: str | None = None
    ) -> StrategyRunReport | StrategyRunUnavailable:
        del context, universe
        try:
            raw = FIXTURE_PATH.read_bytes()
        except FileNotFoundError:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")
        try:
            payload = json.loads(raw)
            if strategy_id != payload["strategy_id"]:
                return StrategyRunUnavailable(strategy_id=strategy_id, reason="unknown_strategy_id")
            return StrategyRunReport(
                strategy_id=payload["strategy_id"],
                source="mart",
                corpus_sha256=payload["corpus_sha256"],
                decisions=tuple(_dec(d) for d in payload["decisions"]),
                golden_mismatches=tuple(payload["golden_mismatches"]),
                strategy_run_id="test_fixture",
                governed=False,
            )
        except _ERRORS:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="schema_mismatch")
