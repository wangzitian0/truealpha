"""Preview replay: run large_model_value_v0 against #335's golden fixture."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from factors.composite.strategy_evaluator import EvaluatedDecision, IssuerInput, evaluate_cutoff
from factors.measure import measure_for_input_key
from factors.panel import IssuerClass, uniform_tree_pins
from truealpha_contracts.strategy import LargeModelValueV0Definition

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN_FIXTURE = REPOSITORY_ROOT / "common/contracts/tests/fixtures/large_model_value_v0_golden_1_0_0.json"
CORPUS_SHA256 = "004e716a11f1a9604275d2da5b13160676411fbb8fc7846b25489b43aef31496"
STRATEGY_ID = "large_model_value_v0"


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


def _load_corpus() -> dict[str, Any]:
    raw = _GOLDEN_FIXTURE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CORPUS_SHA256:
        raise ValueError(f"corpus sha256 mismatch: expected {CORPUS_SHA256}, got {digest}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"corpus must decode to a JSON object, got {type(payload).__name__}")
    return payload


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _risk_free_rate(definition_rates: list[dict[str, Any]], cutoff_at: str) -> Decimal:
    for rate in definition_rates:
        if rate["cutoff_at"] == cutoff_at:
            return Decimal(str(rate["annualized_rate"]))
    raise ValueError(f"no risk-free rate declared for cutoff {cutoff_at}")


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


def evaluate_cutoff_decisions(
    issuers: list[IssuerInput],
    *,
    cutoff_at: str,
    definition: LargeModelValueV0Definition,
    rates: list[dict[str, Any]],
) -> list[Decision]:
    evaluated = evaluate_cutoff(
        issuers,
        definition=definition,
        cutoff_at=_instant(cutoff_at),
        risk_free_rate=_risk_free_rate(rates, cutoff_at),
        tree_pins=uniform_tree_pins(),
    )
    return [_to_decision(item, cutoff_at) for item in evaluated]


def run() -> tuple[list[Decision], LargeModelValueV0Definition]:
    corpus = _load_corpus()
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
    rates = corpus["golden_decision_set"]["risk_free_rates"]
    by_cutoff: dict[str, list[dict[str, Any]]] = {}
    for golden in corpus["golden_decision_set"]["decisions"]:
        by_cutoff.setdefault(golden["cutoff_at"], []).append(golden)
    decisions: list[Decision] = []
    for cutoff_at, group in by_cutoff.items():
        issuers = [
            IssuerInput(
                issuer_id=item["issuer"]["id"],
                records={
                    rec["input_key"]: (
                        measure_for_input_key(
                            rec["input_key"],
                            Decimal(str(rec["value"])),
                            subject=item["issuer"]["id"],
                            knowable_at=_instant(rec["knowable_at"]),
                        ),
                        Decimal(str(rec["confidence"])),
                    )
                    for rec in item["inputs"]
                },
                knowable_at={rec["input_key"]: _instant(rec["knowable_at"]) for rec in item["inputs"]},
                issuer_class=IssuerClass(item["issuer_branch"]),
            )
            for item in group
        ]
        decisions.extend(evaluate_cutoff_decisions(issuers, cutoff_at=cutoff_at, definition=definition, rates=rates))
    decisions.sort(key=lambda item: (item.cutoff_at, item.issuer_id))
    return decisions, definition


def _compare_against_golden(decisions: list[Decision], corpus: dict[str, Any]) -> list[str]:
    by_key = {(decision.issuer_id, decision.cutoff_at): decision for decision in decisions}
    mismatches: list[str] = []
    for golden in corpus["golden_decision_set"]["decisions"]:
        key = (golden["issuer"]["id"], golden["cutoff_at"])
        actual, expected = by_key.get(key), golden["expected"]
        if actual is None:
            mismatches.append(f"{key}: no computed decision")
            continue
        expected_gap = Decimal(str(expected["valuation_gap"])) if expected["valuation_gap"] is not None else None
        for attr, exp_val in (
            ("outcome", expected["outcome"]),
            ("exclusion_reason", expected["exclusion_reason"]),
            ("rank", expected["rank"]),
            ("valuation_gap", expected_gap),
        ):
            if (act_val := getattr(actual, attr)) != exp_val:
                mismatches.append(f"{key}: {attr} {act_val!r} != golden {exp_val!r}")
    return mismatches


def render_markdown(decisions: list[Decision]) -> str:
    lines = [
        "# Strategy smoke preview: large_model_value_v0",
        "",
        "Preview replay against #335's golden fixture.",
        "",
        "| Issuer | Cutoff | Outcome | Tier | Valuation gap | Rank | Weight |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in decisions:
        gap = item.valuation_gap if item.valuation_gap is not None else "-"
        rank = item.rank if item.rank is not None else "-"
        weight = item.target_weight if item.target_weight is not None else "-"
        lines.append(
            f"| {item.issuer_id} | {item.cutoff_at} | {item.outcome} | {item.tier or '-'} | {gap} | {rank} | {weight} |"
        )
    return "\n".join(lines) + "\n"
