"""Preview replay: run large_model_value_v0 against #335's golden fixture.

This reproduces the ten hand-verified golden decisions (5 issuers x 2
cutoffs) from `libs/contracts/tests/fixtures/large_model_value_v0_strategy.v1.json`
by projecting the golden corpus into `factors.composite.strategy_evaluator`
inputs and mapping its decisions onto the mart `Decision` dataclass. The whole
decision algorithm -- factor orchestration, eligibility, the tier-band verdict,
ranking, selection, weighting, and the definition-sourced quantization -- lives
once in that evaluator, so this replay can no longer diverge from the #21 golden
oracle the way it did before #393.

This is explicitly a **preview**, not #26's full acceptance evidence:
- Facts come from the hand-verified golden corpus, not a live capture/
  snapshot pipeline (that's #171/#205/#271's DataHub chain, a separate,
  unrelated dependency -- see the #24/#25/#26 coordination notes).
- There is no BacktestDataGateway, DecisionSnapshot/ReplayEventStream, or
  persisted StrategyRun/Trade/PortfolioValuation record. Those remain real,
  tracked gaps toward #26's full acceptance.

`run()` is the pure, side-effect-free entry point both the CLI script
(`apps/data-engine/scripts/run_strategy_smoke.py`) and the Dagster asset
(`data_engine.core_strategy_replay_assets`) call -- this module has no
Dagster import and no file I/O of its own beyond reading the checked-in
golden corpus, so it stays trivially testable and reusable from either
caller without one depending on the other's concerns.
"""

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
from truealpha_contracts.strategy import LargeModelValueV0Definition

# CANONICAL_FIXTURE_PATH (the CLI script's *output* golden preview) is a
# repo-write concern and stays repo-root-relative; the corpus this module
# *reads* at runtime must not be, since data_engine ships as a wheel
# (`packages = ["src/data_engine"]`) that won't have the monorepo checked
# out beside it -- see #357's review. It is packaged inside
# truealpha_contracts.data instead (that package is fully included in the
# truealpha-contracts wheel) and read back with importlib.resources, the
# same pattern truealpha_contracts.strategy_run_fixture already uses.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_FIXTURE_PACKAGE = "truealpha_contracts.data"
_FIXTURE_NAME = "large_model_value_v0_strategy.v1.json"
CORPUS_SHA256 = "8cdb081d887ff7754ac52a1eb02679b94a1c1c71b1eb32c606c06f5d6fe96083"
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

    def to_json(self) -> dict[str, Any]:
        return {
            "issuer_id": self.issuer_id,
            "cutoff_at": self.cutoff_at,
            "capital_adjusted_labor_efficiency": str(self.capital_adjusted_labor_efficiency)
            if self.capital_adjusted_labor_efficiency is not None
            else None,
            "tier": self.tier,
            "current_price_to_sales": str(self.current_price_to_sales)
            if self.current_price_to_sales is not None
            else None,
            "target_price_to_sales": str(self.target_price_to_sales)
            if self.target_price_to_sales is not None
            else None,
            "valuation_gap": str(self.valuation_gap) if self.valuation_gap is not None else None,
            "eligible": self.eligible,
            "outcome": self.outcome,
            "exclusion_reason": self.exclusion_reason,
            "rank": self.rank,
            "target_weight": str(self.target_weight) if self.target_weight is not None else None,
            "confidence": str(self.confidence) if self.confidence is not None else None,
        }


def _load_corpus() -> dict[str, Any]:
    raw = resources.files(_FIXTURE_PACKAGE).joinpath(_FIXTURE_NAME).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CORPUS_SHA256:
        raise ValueError(f"corpus sha256 mismatch: expected {CORPUS_SHA256}, got {digest}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"corpus must decode to a JSON object, got {type(payload).__name__}")
    return payload


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
    )


def run() -> tuple[list[Decision], LargeModelValueV0Definition]:
    corpus = _load_corpus()
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
    rates = corpus["golden_decision_set"]["risk_free_rates"]

    by_cutoff: dict[str, list[dict[str, Any]]] = {}
    for golden in corpus["golden_decision_set"]["decisions"]:
        by_cutoff.setdefault(golden["cutoff_at"], []).append(golden)

    # The per-issuer + ranking/selection algorithm lives once in
    # factors.composite.strategy_evaluator; the replay only projects the golden
    # corpus into its inputs and maps the evaluator's decisions onto the mart
    # Decision dataclass (#393).
    decisions: list[Decision] = []
    for cutoff_at, group in by_cutoff.items():
        as_of = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
        risk_free_rate = _risk_free_rate(rates, cutoff_at)
        issuers = [
            IssuerInput(
                issuer_id=item["issuer"]["id"],
                records={
                    record["input_key"]: (Decimal(str(record["value"])), Decimal(str(record["confidence"])))
                    for record in item["inputs"]
                },
            )
            for item in group
        ]
        evaluated = evaluate_cutoff(issuers, definition=definition, cutoff_at=as_of, risk_free_rate=risk_free_rate)
        decisions.extend(_to_decision(item, cutoff_at) for item in evaluated)

    decisions.sort(key=lambda item: (item.cutoff_at, item.issuer_id))
    return decisions, definition


def _compare_against_golden(decisions: list[Decision], corpus: dict[str, Any]) -> list[str]:
    by_key = {(d.issuer_id, d.cutoff_at): d for d in decisions}
    mismatches: list[str] = []
    for golden in corpus["golden_decision_set"]["decisions"]:
        key = (golden["issuer"]["id"], golden["cutoff_at"])
        actual = by_key.get(key)
        expected = golden["expected"]
        if actual is None:
            mismatches.append(f"{key}: no computed decision")
            continue
        if actual.outcome != expected["outcome"]:
            mismatches.append(f"{key}: outcome {actual.outcome!r} != golden {expected['outcome']!r}")
        if actual.exclusion_reason != expected["exclusion_reason"]:
            mismatches.append(
                f"{key}: exclusion_reason {actual.exclusion_reason!r} != golden {expected['exclusion_reason']!r}"
            )
        if actual.rank != expected["rank"]:
            mismatches.append(f"{key}: rank {actual.rank!r} != golden {expected['rank']!r}")
        expected_gap = Decimal(str(expected["valuation_gap"])) if expected["valuation_gap"] is not None else None
        if actual.valuation_gap != expected_gap:
            mismatches.append(f"{key}: valuation_gap {actual.valuation_gap!r} != golden {expected_gap!r}")
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
    for decision in decisions:
        lines.append(
            f"| {decision.issuer_id} | {decision.cutoff_at} | {decision.outcome} | {decision.tier or '-'} | "
            f"{decision.valuation_gap if decision.valuation_gap is not None else '-'} | "
            f"{decision.rank if decision.rank is not None else '-'} | "
            f"{decision.target_weight if decision.target_weight is not None else '-'} |"
        )
    return "\n".join(lines) + "\n"
