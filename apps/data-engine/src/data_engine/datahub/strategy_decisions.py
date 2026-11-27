"""Typed strategy decision record shared across production replay and preview scripts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from factors.composite.strategy_evaluator import EvaluatedDecision

CORPUS_SHA256 = "004e716a11f1a9604275d2da5b13160676411fbb8fc7846b25489b43aef31496"

# These fields persist to mart.metric_node_values, not to the decision row.
_METRIC_NODE_ONLY_FIELDS = (
    "peg_consensus",
    "peg_consensus_rank",
    "peg_consensus_reason_codes",
    "peg_consensus_confidence",
    "peg_geometric_cagr",
    "peg_geometric_cagr_rank",
    "peg_geometric_cagr_reason_codes",
    "peg_geometric_cagr_confidence",
)


@dataclass(frozen=True)
class Decision:
    issuer_id: str
    cutoff_at: str
    capital_adjusted_labor_efficiency: Decimal | None
    tier: str | None
    current_price_to_sales: Decimal | None
    target_price_to_sales: Decimal | None
    valuation_gap: Decimal | None
    eligible: bool
    outcome: str
    exclusion_reason: str | None
    rank: int | None = None
    target_weight: Decimal | None = None
    confidence: Decimal | None = None
    peg: Decimal | None = None
    peg_rank: int | None = None
    peg_reason_codes: tuple[str, ...] = ()
    labor_efficiency_metric: str | None = None
    current_price_to_gross_profit: Decimal | None = None
    target_price_to_gross_profit: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            name: (str(val) if isinstance(val := getattr(self, name), Decimal) else val)
            for name in (
                "issuer_id",
                "cutoff_at",
                "capital_adjusted_labor_efficiency",
                "tier",
                "current_price_to_sales",
                "target_price_to_sales",
                "valuation_gap",
                "eligible",
                "outcome",
                "exclusion_reason",
                "rank",
                "target_weight",
                "confidence",
                "peg",
                "peg_rank",
            )
        }
        for name in ("current_price_to_gross_profit", "target_price_to_gross_profit"):
            if (value := getattr(self, name)) is not None:
                payload[name] = str(value)
        return payload


def _to_decision(evaluated: EvaluatedDecision, cutoff_at: str) -> Decision:
    attrs = {
        k: getattr(evaluated, k)
        for k in EvaluatedDecision.__dataclass_fields__
        if k not in ("issuer_id", "outcome", "exclusion_reason", *_METRIC_NODE_ONLY_FIELDS)
    }
    return Decision(
        issuer_id=evaluated.issuer_id,
        cutoff_at=cutoff_at,
        outcome=evaluated.outcome.value,
        exclusion_reason=None if evaluated.exclusion_reason is None else evaluated.exclusion_reason.value,
        **attrs,
    )
