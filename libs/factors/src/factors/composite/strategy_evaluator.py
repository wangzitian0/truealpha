"""Single-source evaluator for the large_model_value_v0 decision algorithm (#393).

The strategy DECISION algorithm — missing-input exclusion, the consumed-confidence
floor, the capital-adjusted labor-efficiency / price-to-sales / three-tier factor
computation, the tier-band valuation verdict, ranking, top-N selection and equal
weighting — used to be hand-written twice: once in the #26 replay
(`data_engine.core_strategy_replay`) and once in the #21 golden oracle
(`truealpha_contracts` `test_strategy`). The two copies could (and did) diverge —
the replay rejected at the band *midpoint* while the spec rejects at the band
*upper bound*, and the replay hard-coded quantization while the oracle read it
from the definition. This module is the one implementation both consume, so the
algorithm can no longer be re-derived per consumer.

The factor arithmetic itself stays in the base factors (`gross_profit_per_employee`,
`price_to_sales`, `three_tier_valuation`); this evaluator orchestrates them and
owns only the portfolio-decision rules, reading every parameter (quantization,
thresholds, bands, selection count) from the versioned
`LargeModelValueV0Definition` — no implicit defaults.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from truealpha_contracts.metrics import METRICS
from truealpha_contracts.strategy import (
    DecimalQuantization,
    ExclusionReason,
    GoldenDecisionOutcome,
    LargeModelValueV0Definition,
)

from factors.base.gross_profit_per_employee import gross_profit_per_employee
from factors.base.price_to_sales import price_to_sales
from factors.composite.three_tier_valuation import three_tier_valuation
from factors.types import Fact

# Input-key vocabulary of the golden corpus / DataHub factor inputs, mapped to the
# canonical metric registry names the base factors consume.
_GPPE_KEYS = ("gross_profit", "total_assets", "headcount")
_PS_KEYS = ("last_close", "shares_outstanding", "revenue")
_METRIC_FOR_KEY = {"headcount": "employees_total", "last_close": "price"}

# Fixed evaluation order for a missing required input -> its exact reason code.
_REQUIRED_INPUT_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    ("gross_profit", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
    ("total_assets", ExclusionReason.MISSING_TOTAL_ASSETS_FACT),
    ("headcount", ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE),
    ("revenue", ExclusionReason.MISSING_REVENUE_FACT),
    ("shares_outstanding", ExclusionReason.MISSING_MARKET_VALUE_INPUT),
    ("last_close", ExclusionReason.MISSING_MARKET_VALUE_INPUT),
)


@dataclass(frozen=True)
class IssuerInput:
    """One issuer's provenance-neutral factor inputs at a single cutoff.

    ``records`` maps input key -> (value, confidence); a missing key means the
    input was unavailable at the cutoff.
    """

    issuer_id: str
    records: Mapping[str, tuple[Decimal, Decimal]]


@dataclass(frozen=True)
class EvaluatedDecision:
    issuer_id: str
    capital_adjusted_labor_efficiency: Decimal | None
    tier: str | None
    current_price_to_sales: Decimal | None
    target_price_to_sales: Decimal | None
    valuation_gap: Decimal | None
    eligible: bool
    outcome: GoldenDecisionOutcome
    exclusion_reason: ExclusionReason | None
    confidence: Decimal | None
    rank: int | None = None
    target_weight: Decimal | None = None


def _quantize(value: Decimal, quantization: DecimalQuantization) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-quantization.decimal_places), ROUND_HALF_EVEN)


def _facts_for(issuer: IssuerInput, keys: Sequence[str], *, as_of: datetime) -> list[Fact]:
    facts: list[Fact] = []
    for key in keys:
        record = issuer.records.get(key)
        if record is None:
            continue
        metric = _METRIC_FOR_KEY.get(key, key)
        value, confidence = record
        facts.append(
            Fact(
                entity_id=issuer.issuer_id,
                metric=metric,
                value=value,
                unit_family=METRICS[metric].unit_family,
                confidence=confidence,
                as_of=as_of,
            )
        )
    return facts


def _evaluate_issuer(
    issuer: IssuerInput,
    *,
    definition: LargeModelValueV0Definition,
    risk_free_rate: Decimal,
    as_of: datetime,
) -> tuple[EvaluatedDecision, Decimal | None]:
    """Return this issuer's pre-ranking decision plus its valuation gap (or None
    when it is not a ranking candidate)."""

    labor_q = definition.labor_efficiency.quantization
    ps_q = definition.price_to_sales.quantization
    tier_q = definition.tier_valuation.quantization
    gap_q = definition.valuation_gap.quantization

    present = set(issuer.records)
    missing = next((reason for key, reason in _REQUIRED_INPUT_REASONS if key not in present), None)
    if missing is not None:
        return _excluded(issuer.issuer_id, missing, confidence=None), None

    consumed_confidence = min(confidence for _value, confidence in issuer.records.values())
    if consumed_confidence < definition.eligibility.minimum_confidence:
        return (
            _excluded(issuer.issuer_id, ExclusionReason.BELOW_CONFIDENCE_FLOOR, confidence=consumed_confidence),
            None,
        )

    gppe_result = gross_profit_per_employee(
        _facts_for(issuer, _GPPE_KEYS, as_of=as_of),
        entity_id=issuer.issuer_id,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
    )
    ps_result = price_to_sales(_facts_for(issuer, _PS_KEYS, as_of=as_of), entity_id=issuer.issuer_id, as_of=as_of)
    if gppe_result.value is None or ps_result.value is None:
        return _excluded(issuer.issuer_id, ExclusionReason.STALE_REQUIRED_INPUT, confidence=consumed_confidence), None

    labor_efficiency = _quantize(gppe_result.value, labor_q)
    current_ps = _quantize(ps_result.value, ps_q)
    tier_result = three_tier_valuation(
        [
            gppe_result.model_copy(update={"value": labor_efficiency}),
            ps_result.model_copy(update={"value": current_ps}),
        ],
        entity_id=issuer.issuer_id,
        as_of=as_of,
        definition=definition.tier_valuation,
    )
    band = definition.tier_valuation.band_for(labor_efficiency)
    target_ps = _quantize((band.target_ps_lower_bound + band.target_ps_upper_bound) / Decimal(2), tier_q)
    valuation_gap = _quantize(tier_result.value, gap_q)
    confidence = tier_result.confidence

    # "Above tier band" == P/S above the band's upper bound (#21 spec / #393 L0).
    if current_ps > band.target_ps_upper_bound:
        return (
            EvaluatedDecision(
                issuer_id=issuer.issuer_id,
                capital_adjusted_labor_efficiency=labor_efficiency,
                tier=band.tier.value,
                current_price_to_sales=current_ps,
                target_price_to_sales=target_ps,
                valuation_gap=valuation_gap,
                eligible=True,
                outcome=GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND,
                exclusion_reason=None,
                confidence=confidence,
            ),
            None,
        )
    return (
        EvaluatedDecision(
            issuer_id=issuer.issuer_id,
            capital_adjusted_labor_efficiency=labor_efficiency,
            tier=band.tier.value,
            current_price_to_sales=current_ps,
            target_price_to_sales=target_ps,
            valuation_gap=valuation_gap,
            eligible=True,
            outcome=GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT,
            exclusion_reason=None,
            confidence=confidence,
        ),
        valuation_gap,
    )


def _excluded(issuer_id: str, reason: ExclusionReason, *, confidence: Decimal | None) -> EvaluatedDecision:
    return EvaluatedDecision(
        issuer_id=issuer_id,
        capital_adjusted_labor_efficiency=None,
        tier=None,
        current_price_to_sales=None,
        target_price_to_sales=None,
        valuation_gap=None,
        eligible=False,
        outcome=GoldenDecisionOutcome.EXCLUDED,
        exclusion_reason=reason,
        confidence=confidence,
    )


def rank_and_select(
    decisions: Sequence[EvaluatedDecision], *, definition: LargeModelValueV0Definition
) -> list[EvaluatedDecision]:
    """Rank the eligible, in-band candidates (RANKED_BEYOND_SELECTION_COUNT) by
    descending valuation gap — tie-break ascending issuer id — select the top-N
    and assign equal weight. Excluded and valuation-rejected decisions pass
    through unchanged. Returns the full set sorted by issuer id."""

    candidates = [d for d in decisions if d.outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT]
    resolved = [d for d in decisions if d.outcome is not GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT]
    ranked = sorted(candidates, key=lambda d: (-(d.valuation_gap or Decimal(0)), d.issuer_id))
    selection_count = definition.selection.selection_count
    weight = _quantize(Decimal(1) / Decimal(max(min(len(ranked), selection_count), 1)), definition.sizing.quantization)
    for position, decision in enumerate(ranked, start=1):
        if position <= selection_count:
            resolved.append(
                EvaluatedDecision(
                    **{
                        **decision.__dict__,
                        "outcome": GoldenDecisionOutcome.SELECTED,
                        "rank": position,
                        "target_weight": weight,
                    }
                )
            )
        else:  # pragma: no cover - the frozen corpus selects every ranked issuer
            resolved.append(EvaluatedDecision(**{**decision.__dict__, "rank": position}))
    return sorted(resolved, key=lambda d: d.issuer_id)


def evaluate_cutoff(
    issuers: Sequence[IssuerInput],
    *,
    definition: LargeModelValueV0Definition,
    cutoff_at: datetime,
    risk_free_rate: Decimal,
) -> list[EvaluatedDecision]:
    """Evaluate every issuer at one cutoff, then rank/select/weight the eligible,
    in-band candidates. Returns decisions sorted by issuer id."""

    decisions = [
        _evaluate_issuer(issuer, definition=definition, risk_free_rate=risk_free_rate, as_of=cutoff_at)[0]
        for issuer in issuers
    ]
    return rank_and_select(decisions, definition=definition)
