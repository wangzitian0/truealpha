"""Preview replay: run large_model_value_v0 against #335's golden fixture."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.measure import measure_for_input_key
from factors.panel import IssuerClass, uniform_tree_pins
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.datahub.strategy_decisions import CORPUS_SHA256, Decision, _to_decision

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN_FIXTURE = REPOSITORY_ROOT / "common/contracts/tests/fixtures/large_model_value_v0_golden_1_0_0.json"
STRATEGY_ID = "large_model_value_v0"


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
