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
`price_to_sales`, `price_to_gross_profit`, `three_tier_valuation`); this evaluator
orchestrates them and owns only the portfolio-decision rules, reading every parameter
(quantization, thresholds, bands, selection count, valuation basis) from the versioned
`LargeModelValueV0Definition` — no implicit defaults.

The valuation basis comes from the definition (#1235). Definition 0.1.0 compares P/S
with P/S bands. Definition 1.0.0 compares P/GP with P/GP bands and still records P/S.
No other rule changes between them: the tier, the above-band rejection, the ranking,
the selection count and the weights read the same definition fields.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType

from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS, input_key_for_metric
from truealpha_contracts.strategy import (
    DecimalQuantization,
    ExclusionReason,
    GoldenDecisionOutcome,
    LargeModelValueV0Definition,
    ValuationBasis,
)

from factors.base.gross_profit_per_employee import (
    BANKING_TCE_INPUTS,
    gross_profit_per_employee,
    gross_profit_per_employee_banking_tce,
)
from factors.base.peg import peg
from factors.base.price_to_gross_profit import price_to_gross_profit
from factors.base.price_to_sales import price_to_sales
from factors.composite.three_tier_valuation import three_tier_valuation, tier_band
from factors.forest import IssuerClass
from factors.registry import FACTOR_REGISTRY
from factors.types import Fact, FactorResult, GrowthConvention


def _input_keys(factor_name: str) -> tuple[str, ...]:
    """The strategy input keys a registered factor's declared metrics are spelled as here.

    The factor declares METRICS (`@factor(..., inputs=...)`, #855 B2); the golden corpus and
    the DataHub inputs spell two of them differently (`headcount`, `last_close`), and
    `input_key_for_metric` is the one inverse of that alias map. This module used to keep a
    key tuple per factor — the list that made every new factor an edit here.
    """
    return tuple(input_key_for_metric(metric) for metric in FACTOR_REGISTRY[factor_name].inputs)


# Owner decision 2026-08-17 (#284): three years, recency-weighted. The factor takes it
# as a required argument so no default can quietly become the convention.
_PEG_CAGR_YEARS = 3
#: `factors.base.peg`'s window metadata flags: what the rate was measured over, never a reason.
_PEG_WINDOW_FLAG_PREFIXES = ("cagr_years:", "window:")
# `truealpha_contracts.metrics.INPUT_KEY_ALIASES` is the one declaration of this mapping
# (init.md rule 22, #770) -- this module used to keep its own copy under this name.
_METRIC_FOR_KEY = INPUT_KEY_ALIASES

# Fixed evaluation order for a missing required input -> its exact reason code.
_REQUIRED_INPUT_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    ("gross_profit", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
    ("total_assets", ExclusionReason.MISSING_TOTAL_ASSETS_FACT),
    ("headcount", ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE),
    ("revenue", ExclusionReason.MISSING_REVENUE_FACT),
    ("shares_outstanding", ExclusionReason.MISSING_MARKET_VALUE_INPUT),
    ("last_close", ExclusionReason.MISSING_MARKET_VALUE_INPUT),
)


def _recorded_only_input_keys(basis: ValuationBasis) -> frozenset[str]:
    """Input keys that only P/S consumes when P/S is not the valuation basis (#1235 audit).

    Under definition 1.0.0, P/S is recorded and decides nothing, so its own inputs (revenue)
    cannot exclude an issuer, by absence or by confidence. Derived from the registry, so the
    set follows the factors' declared inputs.
    """
    if basis is ValuationBasis.PRICE_TO_SALES:
        return frozenset()
    return frozenset(_input_keys("price_to_sales")) - frozenset(_input_keys(basis.value))


@dataclass(frozen=True)
class LaborEfficiencyMetric:
    """One named GPPE metric the strategy can rank an issuer on (#1176)."""

    metric: str
    compute: Callable[..., FactorResult]
    input_keys: tuple[str, ...]


_UNIFORM_CHARGE_V0 = LaborEfficiencyMetric(
    metric="gppe_uniform_charge_v0",
    compute=gross_profit_per_employee,
    input_keys=_input_keys("gross_profit_per_employee"),
)
_BANKING_TCE_V1 = LaborEfficiencyMetric(
    metric="gppe_banking_tce_v1",
    compute=gross_profit_per_employee_banking_tce,
    input_keys=tuple(input_key_for_metric(metric) for metric in BANKING_TCE_INPUTS),
)

#: The one binding table for the live path (owner decision 2026-10-09, #1176): FINANCIAL issuers
#: rank on measured tangible common equity; every other class ranks on the uniform total-assets
#: charge. An issuer with no class has no entry: it gets `missing_issuer_class`, never a fallback.
_LIVE_LABOR_EFFICIENCY: dict[IssuerClass | None, LaborEfficiencyMetric] = {
    IssuerClass.FINANCIAL: _BANKING_TCE_V1,
    IssuerClass.NON_FINANCIAL: _UNIFORM_CHARGE_V0,
    IssuerClass.INSURANCE: _UNIFORM_CHARGE_V0,
}
LABOR_EFFICIENCY_BY_CLASS: Mapping[IssuerClass | None, LaborEfficiencyMetric] = MappingProxyType(_LIVE_LABOR_EFFICIENCY)

#: The frozen #21 replay binding, passed by name by the replay callers. Every class, and a
#: classless input, ranks on the uniform charge the #21 golden was frozen with.
UNIFORM_LABOR_EFFICIENCY_BINDING: Mapping[IssuerClass | None, LaborEfficiencyMetric] = MappingProxyType(
    {
        None: _UNIFORM_CHARGE_V0,
        IssuerClass.FINANCIAL: _UNIFORM_CHARGE_V0,
        IssuerClass.NON_FINANCIAL: _UNIFORM_CHARGE_V0,
        IssuerClass.INSURANCE: _UNIFORM_CHARGE_V0,
    }
)

#: Factor flags that name a missing banking input, and the exclusion reason each one carries.
_MISSING_TCE_FLAG = "missing_tangible_common_equity"
_MISSING_PREFERRED_FLAG = "missing_preferred_stock_value"
_BANKING_FLAG_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    (_MISSING_PREFERRED_FLAG, ExclusionReason.MISSING_PREFERRED_STOCK_VALUE),
    (_MISSING_TCE_FLAG, ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
)
#: Refusal flags of the valuation multiples that have an exact exclusion reason (#1235).
_MULTIPLE_FLAG_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    ("nonpositive_gross_profit", ExclusionReason.NONPOSITIVE_GROSS_PROFIT),
)
#: The feeder that computes each valuation basis. The basis is the factor's registered name.
_MULTIPLE_FACTOR = {
    ValuationBasis.PRICE_TO_SALES: price_to_sales,
    ValuationBasis.PRICE_TO_GROSS_PROFIT: price_to_gross_profit,
}


@dataclass(frozen=True)
class IssuerInput:
    """One issuer's provenance-neutral factor inputs at a single cutoff.

    ``records`` maps input key -> (value, confidence); a missing key means the
    input was unavailable at the cutoff.

    ``periodic_records`` maps input key -> fiscal period -> (value, confidence) for the
    inputs that describe a PERIOD rather than an instant. Additive and defaulted: the six
    point-in-time inputs never had periods and still do not. It exists so a multi-period
    factor can be expressed in ``libs/factors`` at all — before migration 0043 the
    transport carried one value per key, so module 1's growth rate had to be reduced in
    the capture adapter, which is factor arithmetic in L0 (init.md rule 2).
    """

    issuer_id: str
    records: Mapping[str, tuple[Decimal, Decimal]]
    periodic_records: Mapping[str, Mapping[str, tuple[Decimal, Decimal]]] = field(default_factory=dict)
    #: The issuer's operating class. It selects the labor-efficiency metric. None means no class.
    issuer_class: IssuerClass | None = None


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
    # Module 1, recorded but not selecting (#284 step 4). Defaults to None so an excluded
    # issuer needs no growth basis, and so its absence can never change eligibility —
    # landing the factor must not move the portfolio before the owner decides how it
    # enters selection.
    peg: Decimal | None = None
    # Module 1's OWN ordering, lowest PEG first (#284, owner decision 2026-08-17: ranking
    # only, selection later). Deliberately separate from `rank`: folding PEG into the
    # primary rank would change which issuers are selected, and the selection rule has not
    # been decided. None when the issuer has no PEG — an absent value must not be ranked as
    # though it were the worst.
    peg_rank: int | None = None
    # Why `peg` is None, in the PEG factor's own flag vocabulary (`non_positive_growth`,
    # `insufficient_earnings_history`, ...). Empty when PEG is present or was never computed
    # (an excluded issuer's reason is `exclusion_reason`). Kept because the factor already
    # names every degenerate case, and dropping the name here left the question-coverage
    # report able to say only `unrecorded_reason` (#837).
    peg_reason_codes: tuple[str, ...] = ()
    # #1176: the labor-efficiency metric this decision was computed with, from the binding that selected it.
    # None when no metric was selected (no class, or excluded before the selection).
    labor_efficiency_metric: str | None = None
    # #1235: the P/GP multiple and its band midpoint, set only under a P/GP definition. Under it
    # `target_price_to_sales` is None (no P/S band exists) and `current_price_to_sales` is still recorded.
    current_price_to_gross_profit: Decimal | None = None
    target_price_to_gross_profit: Decimal | None = None


def peg_reason_codes(result: FactorResult) -> tuple[str, ...]:
    """Why a PEG is absent, in the factor's own flags; empty when it is present (#837).

    A present PEG carries only window metadata (`cagr_years:3`, `window:…`), and an absent one
    carries its refusal plus, sometimes, that metadata. Filtered by the metadata's own prefixes
    rather than by shape, because a reason may be parameterised too
    (`growth_convention_unsourced:<convention>`, review on #838).
    """
    if result.value is not None:
        return ()
    return tuple(flag for flag in result.flags if not flag.startswith(_PEG_WINDOW_FLAG_PREFIXES))


def _quantize(value: Decimal, quantization: DecimalQuantization) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-quantization.decimal_places), ROUND_HALF_EVEN)


def _facts_for(issuer: IssuerInput, keys: Sequence[str], *, as_of: datetime) -> list[Fact]:
    facts: list[Fact] = []
    for key in keys:
        # A periodic input contributes one Fact PER PERIOD, carrying the period tag the
        # factor parses. The scalar branch below still answers for the same key (the
        # latest period), which is what keeps the coverage check and the multiple working
        # off one vocabulary.
        for period, (value, confidence) in sorted(issuer.periodic_records.get(key, {}).items()):
            metric = _METRIC_FOR_KEY.get(key, key)
            facts.append(
                Fact(
                    entity_id=issuer.issuer_id,
                    metric=metric,
                    value=value,
                    unit_family=METRICS[metric].unit_family,
                    confidence=confidence,
                    as_of=as_of,
                    fiscal_period=period,
                )
            )
        if key in issuer.periodic_records:
            continue
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
    binding: Mapping[IssuerClass | None, LaborEfficiencyMetric],
) -> tuple[EvaluatedDecision, Decimal | None]:
    """Return this issuer's pre-ranking decision plus its valuation gap (or None
    when it is not a ranking candidate)."""

    labor_q = definition.labor_efficiency.quantization
    ps_q = definition.price_to_sales.quantization
    multiple_q = definition.valuation_quantization
    tier_q = definition.tier_valuation.quantization
    gap_q = definition.valuation_gap.quantization
    basis = definition.valuation_basis

    recorded_only = _recorded_only_input_keys(basis)
    present = set(issuer.records)
    missing = next(
        (reason for key, reason in _REQUIRED_INPUT_REASONS if key not in present and key not in recorded_only), None
    )
    if missing is not None:
        return _excluded(issuer.issuer_id, missing, confidence=None), None

    consumed_confidence = min(
        confidence for key, (_value, confidence) in issuer.records.items() if key not in recorded_only
    )
    if consumed_confidence < definition.eligibility.minimum_confidence:
        return (
            _excluded(issuer.issuer_id, ExclusionReason.BELOW_CONFIDENCE_FLOOR, confidence=consumed_confidence),
            None,
        )

    labor = binding.get(issuer.issuer_class)
    if labor is None:
        return _excluded(issuer.issuer_id, ExclusionReason.MISSING_ISSUER_CLASS, confidence=consumed_confidence), None
    gppe_result = labor.compute(
        _facts_for(issuer, labor.input_keys, as_of=as_of),
        entity_id=issuer.issuer_id,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
    )
    ps_result = price_to_sales(
        _facts_for(issuer, _input_keys("price_to_sales"), as_of=as_of), entity_id=issuer.issuer_id, as_of=as_of
    )
    multiple_result = (
        ps_result
        if basis is ValuationBasis.PRICE_TO_SALES
        else _MULTIPLE_FACTOR[basis](
            _facts_for(issuer, _input_keys(basis.value), as_of=as_of), entity_id=issuer.issuer_id, as_of=as_of
        )
    )
    # P/S gates only when it is the valuation basis (then it IS `multiple_result`). Under a P/GP
    # definition a P/S that cannot be computed is recorded as None and decides nothing.
    if gppe_result.value is None or multiple_result.value is None:
        reason = next(
            (
                reason
                for flag, reason in (*_BANKING_FLAG_REASONS, *_MULTIPLE_FLAG_REASONS)
                if flag in (*gppe_result.flags, *multiple_result.flags)
            ),
            ExclusionReason.STALE_REQUIRED_INPUT,
        )
        return (
            _excluded(issuer.issuer_id, reason, confidence=consumed_confidence, labor_efficiency_metric=labor.metric),
            None,
        )

    # Recorded, not selecting (#284 step 4): module 1 must not change who is eligible until
    # the owner decides how it enters selection, or landing the factor would silently move
    # the portfolio. So a missing or degenerate PEG leaves the decision otherwise untouched.
    peg_result = peg(
        _facts_for(issuer, _input_keys("peg"), as_of=as_of),
        entity_id=issuer.issuer_id,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=as_of,
        cagr_years=_PEG_CAGR_YEARS,
    )
    peg_value = peg_result.value
    peg_reasons = peg_reason_codes(peg_result)

    labor_efficiency = _quantize(gppe_result.value, labor_q)
    current_ps = None if ps_result.value is None else _quantize(ps_result.value, ps_q)
    current_multiple = _quantize(multiple_result.value, multiple_q)
    # Read by the 0.1.0 legacy margin gate only (`LEGACY_V0_MARGIN_GATE`, #1235); None when
    # revenue is absent or not positive, which leaves the band unchanged.
    revenue = issuer.records.get("revenue")
    gross_margin = issuer.records["gross_profit"][0] / revenue[0] if revenue is not None and revenue[0] > 0 else None
    tier_result = three_tier_valuation(
        [
            gppe_result.model_copy(update={"value": labor_efficiency}),
            multiple_result.model_copy(update={"value": current_multiple}),
        ],
        entity_id=issuer.issuer_id,
        as_of=as_of,
        definition=definition,
        gross_margin=gross_margin,
    )
    band = tier_band(definition, labor_efficiency, gross_margin=gross_margin)
    target_multiple = _quantize((band.target_lower_bound + band.target_upper_bound) / Decimal(2), tier_q)
    if tier_result.value is None:
        return (
            _excluded(
                issuer.issuer_id,
                ExclusionReason.MISSING_MARKET_VALUE_INPUT,
                confidence=tier_result.confidence,
                labor_efficiency_metric=labor.metric,
            ),
            None,
        )

    valuation_gap = _quantize(tier_result.value, gap_q)
    by_gross_profit = basis is ValuationBasis.PRICE_TO_GROSS_PROFIT
    decision = EvaluatedDecision(
        issuer_id=issuer.issuer_id,
        capital_adjusted_labor_efficiency=labor_efficiency,
        tier=band.tier.value,
        current_price_to_sales=current_ps,
        target_price_to_sales=None if by_gross_profit else target_multiple,
        current_price_to_gross_profit=current_multiple if by_gross_profit else None,
        target_price_to_gross_profit=target_multiple if by_gross_profit else None,
        valuation_gap=valuation_gap,
        peg=peg_value,
        peg_reason_codes=peg_reasons,
        eligible=True,
        outcome=GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT,
        exclusion_reason=None,
        confidence=tier_result.confidence,
        labor_efficiency_metric=labor.metric,
    )
    # "Above tier band" == the current multiple above the band's upper bound (#21 spec / #393 L0),
    # in the definition's valuation basis.
    if current_multiple > band.target_upper_bound:
        return replace(decision, outcome=GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND), None
    return decision, valuation_gap


def _excluded(
    issuer_id: str,
    reason: ExclusionReason,
    *,
    confidence: Decimal | None,
    labor_efficiency_metric: str | None = None,
) -> EvaluatedDecision:
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
        labor_efficiency_metric=labor_efficiency_metric,
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
    # PEG's ordering is assigned over EVERY decision that has one, not only the ranked
    # candidates, so a valuation-rejected issuer still shows where its growth-adjusted
    # multiple sits. It never feeds `rank` or `target_weight`.
    peg_positions = {
        decision.issuer_id: position
        for position, decision in enumerate(
            sorted((d for d in decisions if d.peg is not None), key=lambda d: (d.peg, d.issuer_id)), start=1
        )
    }
    resolved = [EvaluatedDecision(**{**d.__dict__, "peg_rank": peg_positions.get(d.issuer_id)}) for d in resolved]
    ranked = [EvaluatedDecision(**{**d.__dict__, "peg_rank": peg_positions.get(d.issuer_id)}) for d in ranked]

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
        else:
            resolved.append(EvaluatedDecision(**{**decision.__dict__, "rank": position}))
    return sorted(resolved, key=lambda d: d.issuer_id)


def evaluate_cutoff(
    issuers: Sequence[IssuerInput],
    *,
    definition: LargeModelValueV0Definition,
    cutoff_at: datetime,
    risk_free_rate: Decimal,
    labor_efficiency_by_class: Mapping[IssuerClass | None, LaborEfficiencyMetric] = LABOR_EFFICIENCY_BY_CLASS,
) -> list[EvaluatedDecision]:
    """Evaluate every issuer at one cutoff, then rank/select/weight the eligible,
    in-band candidates. Returns decisions sorted by issuer id.

    `labor_efficiency_by_class` is the binding that selects each issuer's labor metric. An issuer
    whose class has no entry is excluded with `missing_issuer_class`. Replays of the frozen #21
    corpus pass `UNIFORM_LABOR_EFFICIENCY_BINDING` by name."""

    decisions = [
        _evaluate_issuer(
            issuer,
            definition=definition,
            risk_free_rate=risk_free_rate,
            as_of=cutoff_at,
            binding=labor_efficiency_by_class,
        )[0]
        for issuer in issuers
    ]
    return rank_and_select(decisions, definition=definition)
