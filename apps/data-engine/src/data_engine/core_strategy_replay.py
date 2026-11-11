"""Preview replay: run large_model_value_v0 against #335's golden fixture."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from factors.composite.strategy_evaluator import EvaluatedDecision, IssuerInput, evaluate_cutoff
from factors.measure import measure_for_input_key
from factors.panel import IssuerClass, uniform_tree_pins
from truealpha_contracts.strategy import LargeModelValueV0Definition

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_FIXTURE_PACKAGE = "truealpha_contracts.data"
_FIXTURE_NAME = "large_model_value_v0_definition_1_0_0.json"
CORPUS_SHA256 = "004e716a11f1a9604275d2da5b13160676411fbb8fc7846b25489b43aef31496"
STRATEGY_ID = "large_model_value_v0"


def _opt_str(val: Decimal | None) -> str | None:
    return None if val is None else str(val)


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
            "issuer_id": self.issuer_id,
            "cutoff_at": self.cutoff_at,
            "capital_adjusted_labor_efficiency": _opt_str(self.capital_adjusted_labor_efficiency),
            "tier": self.tier,
            "current_price_to_sales": _opt_str(self.current_price_to_sales),
            "target_price_to_sales": _opt_str(self.target_price_to_sales),
            "valuation_gap": _opt_str(self.valuation_gap),
            "eligible": self.eligible,
            "outcome": self.outcome,
            "exclusion_reason": self.exclusion_reason,
            "rank": self.rank,
            "target_weight": _opt_str(self.target_weight),
            "confidence": _opt_str(self.confidence),
            "peg": _opt_str(self.peg),
            "peg_rank": self.peg_rank,
        }
        for name in ("current_price_to_gross_profit", "target_price_to_gross_profit"):
            if (value := getattr(self, name)) is not None:
                payload[name] = str(value)
        return payload


def _load_corpus() -> dict[str, Any]:
    raw = resources.files(_FIXTURE_PACKAGE).joinpath(_FIXTURE_NAME).read_bytes()
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


def _to_decision(evaluated: EvaluatedDecision, cutoff_at: str) -> Decision:
    return Decision(
        evaluated.issuer_id,
        cutoff_at,
        evaluated.capital_adjusted_labor_efficiency,
        evaluated.tier,
        evaluated.current_price_to_sales,
        evaluated.target_price_to_sales,
        evaluated.valuation_gap,
        evaluated.eligible,
        evaluated.outcome.value,
        None if evaluated.exclusion_reason is None else evaluated.exclusion_reason.value,
        rank=evaluated.rank,
        target_weight=evaluated.target_weight,
        confidence=evaluated.confidence,
        peg=evaluated.peg,
        peg_rank=evaluated.peg_rank,
        peg_reason_codes=evaluated.peg_reason_codes,
        labor_efficiency_metric=evaluated.labor_efficiency_metric,
        current_price_to_gross_profit=evaluated.current_price_to_gross_profit,
        target_price_to_gross_profit=evaluated.target_price_to_gross_profit,
    )


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
                    record["input_key"]: (
                        measure_for_input_key(
                            record["input_key"],
                            Decimal(str(record["value"])),
                            subject=item["issuer"]["id"],
                            knowable_at=_instant(record["knowable_at"]),
                        ),
                        Decimal(str(record["confidence"])),
                    )
                    for record in item["inputs"]
                },
                knowable_at={record["input_key"]: _instant(record["knowable_at"]) for record in item["inputs"]},
                issuer_class=IssuerClass(item["issuer_branch"]),
            )
            for item in group
        ]
        evaluated = evaluate_cutoff(
            issuers,
            definition=definition,
            cutoff_at=_instant(cutoff_at),
            risk_free_rate=_risk_free_rate(rates, cutoff_at),
            tree_pins=uniform_tree_pins(),
        )
        decisions.extend(_to_decision(item, cutoff_at) for item in evaluated)

    decisions.sort(key=lambda item: (item.cutoff_at, item.issuer_id))
    return decisions, definition


def _compare_against_golden(decisions: list[Decision], corpus: dict[str, Any]) -> list[str]:
    by_key = {(d.issuer_id, d.cutoff_at): d for d in decisions}
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
            act_val = getattr(actual, attr)
            if act_val != exp_val:
                mismatches.append(f"{key}: {attr} {act_val!r} != golden {exp_val!r}")
    return mismatches


def render_markdown(decisions: list[Decision]) -> str:
    lines = [
        "# Strategy smoke preview: large_model_value_v0",
        "",
        "Preview replay against #335's golden fixture. Not a performance claim; "
        "see the module docstring for what this does and does not prove.",
        "",
        "| Issuer | Cutoff | Outcome | Tier | Valuation gap | Rank | Weight |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in decisions:
        lines.append(
            f"| {d.issuer_id} | {d.cutoff_at} | {d.outcome} | {d.tier or '-'} | "
            f"{d.valuation_gap if d.valuation_gap is not None else '-'} | "
            f"{d.rank if d.rank is not None else '-'} | "
            f"{d.target_weight if d.target_weight is not None else '-'} |"
        )
    return "\n".join(lines) + "\n"
