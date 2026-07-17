"""Preview replay: run large_model_value_v0 against #335's golden fixture.

This reproduces the ten hand-verified golden decisions (5 issuers x 2
cutoffs) from `libs/contracts/tests/fixtures/large_model_value_v0_strategy.v1.json`
by calling the real registered factors -- `gross_profit_per_employee` (#24),
`price_to_sales` (#24), and `three_tier_valuation` (#25) -- and assembling
their outputs into eligibility, ranking, selection, and sizing per the
strategy definition's own versioned rules (confidence floor, top-N select,
equal weight).

This is explicitly a **preview**, not #26's full acceptance evidence:
- Facts come from the hand-verified golden corpus, not a live capture/
  snapshot pipeline (that's #171/#205/#271's DataHub chain, a separate,
  unrelated dependency -- see the #24/#25/#26 coordination notes).
- There is no BacktestDataGateway, DecisionSnapshot/ReplayEventStream, or
  persisted StrategyRun/Trade/PortfolioValuation record. Those remain real,
  tracked gaps toward #26's full acceptance.
- Quantization (labor-efficiency/P-S/valuation-gap decimal places) is
  applied here, at assembly time, rather than inside the individual factor
  functions -- the factors return exact unrounded Decimal arithmetic; a
  future real runner is expected to apply the strategy definition's
  quantization rules at the same assembly point this module does.

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

from factors.base.gross_profit_per_employee import gross_profit_per_employee
from factors.base.price_to_sales import price_to_sales
from factors.composite.three_tier_valuation import three_tier_valuation
from factors.types import Fact
from truealpha_contracts.metrics import METRICS
from truealpha_contracts.strategy import LargeModelValueV0Definition, ThreeTierValuationDefinition

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
CORPUS_SHA256 = "0d110a3adc94500cba2bc35d5cd33a788a18bc76ef66895c5625489be6ea50e6"
STRATEGY_ID = "large_model_value_v0"

# The golden fixture's input_key vocabulary predates #24's real factor
# implementations and does not match their Fact.metric names exactly. Targets
# must be registered in truealpha_contracts.metrics.METRICS — Fact rejects an
# unregistered metric or a mismatched unit_family, so this mapping cannot
# silently drift from the canonical registry the way the pre-SSOT
# "employee_headcount" name once did.
_GPPE_KEYS = ("gross_profit", "total_assets", "headcount")
_PS_KEYS = ("last_close", "shares_outstanding", "revenue")
_GPPE_KEY_MAP = {"headcount": "employees_total"}
_PS_KEY_MAP = {"last_close": "price"}
_MISSING_REASON_ORDER = (
    ("gross_profit", "missing_gross_profit_fact"),
    ("total_assets", "missing_total_assets_fact"),
    ("headcount", "missing_headcount_disclosure"),
    ("revenue", "missing_revenue_fact"),
    ("shares_outstanding", "missing_market_value_input"),
    ("last_close", "missing_market_value_input"),
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


def _facts_for(
    input_records: list[dict[str, Any]],
    keys: tuple[str, ...],
    key_map: dict[str, str],
    entity_id: str,
    as_of: datetime,
) -> list[Fact]:
    by_key = {record["input_key"]: record for record in input_records}
    facts = []
    for key in keys:
        record = by_key.get(key)
        if record is None:
            continue
        metric = key_map.get(key, key)
        facts.append(
            Fact(
                entity_id=entity_id,
                metric=metric,
                value=Decimal(str(record["value"])),
                unit_family=METRICS[metric].unit_family,
                confidence=Decimal(str(record["confidence"])),
                as_of=as_of,
            )
        )
    return facts


def _missing_reason(present_keys: set[str]) -> str | None:
    for key, reason in _MISSING_REASON_ORDER:
        if key not in present_keys:
            return reason
    return None


def _risk_free_rate(definition_rates: list[dict[str, Any]], cutoff_at: str) -> Decimal:
    for rate in definition_rates:
        if rate["cutoff_at"] == cutoff_at:
            return Decimal(str(rate["annualized_rate"]))
    raise ValueError(f"no risk-free rate declared for cutoff {cutoff_at}")


def _compute_one(
    issuer_id: str,
    cutoff_at: str,
    input_records: list[dict[str, Any]],
    risk_free_rate: Decimal,
    tier_definition: ThreeTierValuationDefinition,
    minimum_confidence: Decimal,
) -> Decision:
    as_of = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
    present_keys = {record["input_key"] for record in input_records}
    reason = _missing_reason(present_keys)
    if reason is not None:
        return Decision(issuer_id, cutoff_at, None, None, None, None, None, False, "excluded", reason)

    gppe_facts = _facts_for(input_records, _GPPE_KEYS, _GPPE_KEY_MAP, issuer_id, as_of)
    ps_facts = _facts_for(input_records, _PS_KEYS, _PS_KEY_MAP, issuer_id, as_of)
    gppe_result = gross_profit_per_employee(gppe_facts, entity_id=issuer_id, as_of=as_of, risk_free_rate=risk_free_rate)
    ps_result = price_to_sales(ps_facts, entity_id=issuer_id, as_of=as_of)

    if gppe_result.value is None or ps_result.value is None:
        return Decision(
            issuer_id, cutoff_at, None, None, None, None, None, False, "excluded", "unavailable_required_input"
        )

    labor_efficiency_q = gppe_result.value.quantize(Decimal("0.01"))
    current_ps_q = ps_result.value.quantize(Decimal("0.0001"))
    tier_result = three_tier_valuation(
        [
            gppe_result.model_copy(update={"value": labor_efficiency_q}),
            ps_result.model_copy(update={"value": current_ps_q}),
        ],
        entity_id=issuer_id,
        as_of=as_of,
        definition=tier_definition,
    )
    if tier_result.value is None:
        return Decision(
            issuer_id,
            cutoff_at,
            labor_efficiency_q,
            None,
            current_ps_q,
            None,
            None,
            False,
            "excluded",
            "unavailable_required_input",
            confidence=tier_result.confidence,
        )

    if tier_result.confidence < minimum_confidence:
        return Decision(
            issuer_id,
            cutoff_at,
            labor_efficiency_q,
            None,
            None,
            None,
            None,
            False,
            "excluded",
            "below_confidence_floor",
            confidence=tier_result.confidence,
        )

    band = tier_definition.band_for(labor_efficiency_q)
    midpoint_q = ((band.target_ps_lower_bound + band.target_ps_upper_bound) / Decimal(2)).quantize(Decimal("0.0001"))
    valuation_gap_q = tier_result.value.quantize(Decimal("0.0001"))

    if valuation_gap_q < 0:
        return Decision(
            issuer_id,
            cutoff_at,
            labor_efficiency_q,
            band.tier.value,
            current_ps_q,
            midpoint_q,
            valuation_gap_q,
            True,
            "rejected_valuation_above_tier_band",
            None,
            confidence=tier_result.confidence,
        )

    return Decision(
        issuer_id,
        cutoff_at,
        labor_efficiency_q,
        band.tier.value,
        current_ps_q,
        midpoint_q,
        valuation_gap_q,
        True,
        "ranked_beyond_selection_count",  # provisional; ranking below may promote to "selected"
        None,
        confidence=tier_result.confidence,
    )


def _ranking_key(item: Decision) -> tuple[Decimal, str]:
    assert item.valuation_gap is not None, "ranked_beyond_selection_count decisions always carry a valuation_gap"
    return (-item.valuation_gap, item.issuer_id)


def _rank_and_select(decisions: list[Decision], selection_count: int) -> list[Decision]:
    by_cutoff: dict[str, list[Decision]] = {}
    for decision in decisions:
        by_cutoff.setdefault(decision.cutoff_at, []).append(decision)

    resolved: list[Decision] = []
    for cutoff_at, group in by_cutoff.items():
        rankable = [item for item in group if item.outcome == "ranked_beyond_selection_count"]
        other = [item for item in group if item.outcome != "ranked_beyond_selection_count"]
        rankable.sort(key=_ranking_key)
        for index, item in enumerate(rankable, start=1):
            if index <= selection_count:
                weight = (Decimal(1) / Decimal(selection_count)).quantize(Decimal("0.000001"))
                resolved.append(
                    Decision(
                        item.issuer_id,
                        item.cutoff_at,
                        item.capital_adjusted_labor_efficiency,
                        item.tier,
                        item.current_price_to_sales,
                        item.target_price_to_sales,
                        item.valuation_gap,
                        True,
                        "selected",
                        None,
                        rank=index,
                        target_weight=weight,
                        confidence=item.confidence,
                    )
                )
            else:
                resolved.append(
                    Decision(
                        item.issuer_id,
                        item.cutoff_at,
                        item.capital_adjusted_labor_efficiency,
                        item.tier,
                        item.current_price_to_sales,
                        item.target_price_to_sales,
                        item.valuation_gap,
                        True,
                        "ranked_beyond_selection_count",
                        None,
                        rank=index,
                        target_weight=None,
                        confidence=item.confidence,
                    )
                )
        resolved.extend(other)
    resolved.sort(key=lambda item: (item.cutoff_at, item.issuer_id))
    return resolved


def run() -> tuple[list[Decision], LargeModelValueV0Definition]:
    corpus = _load_corpus()
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
    minimum_confidence = definition.eligibility.minimum_confidence
    selection_count = definition.selection.selection_count

    decisions: list[Decision] = []
    for golden in corpus["golden_decision_set"]["decisions"]:
        issuer_id = golden["issuer"]["id"]
        cutoff_at = golden["cutoff_at"]
        risk_free_rate = _risk_free_rate(corpus["golden_decision_set"]["risk_free_rates"], cutoff_at)
        decisions.append(
            _compute_one(
                issuer_id, cutoff_at, golden["inputs"], risk_free_rate, definition.tier_valuation, minimum_confidence
            )
        )

    return _rank_and_select(decisions, selection_count), definition


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
