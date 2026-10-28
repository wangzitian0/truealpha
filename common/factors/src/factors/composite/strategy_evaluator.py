"""Single-source evaluator for the large_model_value_v0 decision algorithm (#393).

The strategy DECISION algorithm — missing-input exclusion, the input-age limit, the
consumed-confidence floor, the capital-adjusted labor-efficiency / price-to-sales /
three-tier factor computation, the tier-band valuation verdict, ranking, top-N selection
and equal weighting — used to be hand-written twice: once in the #26 replay
(`data_engine.core_strategy_replay`) and once in the #21 golden oracle
(`truealpha_contracts` `test_strategy`). The two copies could (and did) diverge —
the replay rejected at the band *midpoint* while the spec rejects at the band
*upper bound*, and the replay hard-coded quantization while the oracle read it
from the definition. This module is the one implementation both consume, so the
algorithm can no longer be re-derived per consumer.

The factor arithmetic itself stays in the base factors (`price_to_sales`,
`price_to_gross_profit`, `three_tier_valuation`) and, for labor efficiency, in the tree of the
metric forest that the definition pins for the issuer class (#1518). This evaluator
orchestrates them and owns only the portfolio-decision rules, reading every parameter
(quantization, thresholds, bands, selection count, valuation basis) from the versioned
`LargeModelValueV0Definition` — no implicit defaults.

The valuation basis comes from the definition (#1235). Definition 0.1.0 compares P/S
with P/S bands. Definition 1.0.0 compares P/GP with P/GP bands and still records P/S.
No other rule changes between them: the tier, the above-band rejection, the ranking,
the selection count and the weights read the same definition fields. Definition 1.1.0
(#1518) pins one labor-efficiency tree for each issuer class: banks charge the risk-free rate on
tangible common equity, every other class on total assets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType

from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS, input_key_for_metric
from truealpha_contracts.strategy import (
    DecimalQuantization,
    ExclusionReason,
    GoldenDecisionOutcome,
    LaborEfficiencyTreePin,
    LargeModelValueV0Definition,
    ValuationBasis,
)

from factors.base.gross_profit_per_employee import (
    NEGATIVE_TCE_FLAG,
    NONPOSITIVE_EMPLOYEES_FLAG,
    gross_profit_per_employee,
)
from factors.base.peg import peg
from factors.base.price_to_gross_profit import price_to_gross_profit
from factors.base.price_to_sales import price_to_sales
from factors.composite.three_tier_valuation import three_tier_valuation, tier_band
from factors.forest import (
    FOREST,
    AliasKind,
    Forest,
    IssuerClass,
    MetricTree,
    bound_tree,
    definition_pins,
    input_key_of,
    required_inputs,
)
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

# Fixed evaluation order for a missing required input -> its exact reason code. The order lists every
# input that any pinned tree consumes. An issuer is tested on the inputs of its own class only (#1518).
_REQUIRED_INPUT_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    ("gross_profit", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
    ("total_assets", ExclusionReason.MISSING_TOTAL_ASSETS_FACT),
    ("stockholders_equity", ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
    ("goodwill", ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
    ("intangible_assets_net_excluding_goodwill", ExclusionReason.MISSING_TANGIBLE_COMMON_EQUITY),
    ("preferred_stock_value", ExclusionReason.MISSING_PREFERRED_STOCK_VALUE),
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


def _required_inputs(basis: ValuationBasis, consumed: frozenset[str]) -> tuple[tuple[str, ExclusionReason], ...]:
    """The inputs whose absence excludes an issuer under ``basis``, with their exclusion reasons.

    ``consumed`` is the set that the pinned tree of the issuer class reads (`required_input_keys`). The
    missing-input rule, the input-age limit (#1255) and the confidence floor all read this list. A key that
    cannot exclude by absence cannot exclude by age or by confidence.
    """
    recorded_only = _recorded_only_input_keys(basis)
    return tuple(
        (key, reason) for key, reason in _REQUIRED_INPUT_REASONS if key in consumed and key not in recorded_only
    )


#: Factor flags that name a banking input that is not usable, and the exclusion reason each one carries.
_BANKING_FLAG_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    (NEGATIVE_TCE_FLAG, ExclusionReason.NEGATIVE_TANGIBLE_COMMON_EQUITY),
)
#: Refusal flags of the labor-efficiency tree and of the valuation multiples that have an exact exclusion reason.
#: The core gives the same reason for a headcount that is not positive (#1518).
_MULTIPLE_FLAG_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    (NONPOSITIVE_EMPLOYEES_FLAG, ExclusionReason.NONPOSITIVE_HEADCOUNT),
    ("nonpositive_gross_profit", ExclusionReason.NONPOSITIVE_GROSS_PROFIT),
)
#: The feeder that computes each valuation basis. The basis is the factor's registered name.
_MULTIPLE_FACTOR = {
    ValuationBasis.PRICE_TO_SALES: price_to_sales,
    ValuationBasis.PRICE_TO_GROSS_PROFIT: price_to_gross_profit,
}


def _labor_efficiency_keys(forest: Forest, tree: MetricTree, issuer_class: IssuerClass) -> list[str]:
    """The strategy input keys of the captured leaves of ``tree`` for ``issuer_class`` (the rate is no leaf)."""
    return [
        input_key_of(node)
        for node in required_inputs(forest, tree, issuer_class)
        if node.alias(AliasKind.DEFINITION_PARAMETER) is None
    ]


def _consumed_input_keys(forest: Forest, trees: Mapping[IssuerClass, MetricTree]) -> frozenset[str]:
    """The input keys that the given trees consume, plus the inputs of price-to-sales.

    The decision rules (missing input, age, confidence floor) and the coverage metric read this one
    derivation. Market value is shares times the last close. P/GP divides the same market value by the
    gross profit that the tree already consumes.
    """
    keys = set(_input_keys("price_to_sales"))
    for issuer_class, tree in trees.items():
        keys.update(_labor_efficiency_keys(forest, tree, issuer_class))
    return frozenset(keys)


def required_input_keys(
    definition: LargeModelValueV0Definition,
    issuer_class: IssuerClass | None = None,
    *,
    forest: Forest = FOREST,
    pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
) -> frozenset[str]:
    """The per-issuer input keys that ``definition`` consumes (#496, #1518).

    The keys come from the pinned trees: every captured input of the tree of the class, plus the
    inputs of price-to-sales (market value is shares times the last close; P/GP divides the same
    market value by the gross profit that the tree already consumes). Without a class the set is
    the union over every pinned class. The L2 coverage metric measures staging presence against
    the set of the issuer's class, and the output invariant `every-consumed-input-is-populated`
    reads the union.
    """
    pinned = definition_pins(definition) if pins is None else pins
    classes = [IssuerClass(name) for name in pinned] if issuer_class is None else [issuer_class]
    return _consumed_input_keys(
        forest, {each: bound_tree(definition, each, forest=forest, pins=pins) for each in classes}
    )


def deciding_input_keys(
    definition: LargeModelValueV0Definition,
    issuer_class: IssuerClass,
    *,
    forest: Forest = FOREST,
    pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
) -> frozenset[str]:
    """The input keys whose absence, age or confidence can exclude an issuer of ``issuer_class`` (#1518).

    These are the `required_input_keys` of the class less the inputs that only a recorded factor reads.
    The evaluator tests exactly this set. The TOPT core snapshot reads it too, so an input that is too old
    for the decision is too old for the core.
    """
    return frozenset(
        key
        for key, _reason in _required_inputs(
            definition.valuation_basis, required_input_keys(definition, issuer_class, forest=forest, pins=pins)
        )
    )


@dataclass(frozen=True)
class IssuerInput:
    """One issuer's provenance-neutral factor inputs at a single cutoff.

    ``records`` maps input key -> (value, confidence); a missing key means the
    input was unavailable at the cutoff.

    ``knowable_at`` maps input key -> the instant when the winning observation of that key
    became knowable (#1255). A periodic key uses the observation that answers as its scalar
    in ``records``: the latest period. The field has no default. A default would let a
    producer skip the input-age limit without an error.

    ``periodic_records`` maps input key -> fiscal period -> (value, confidence) for the
    inputs that describe a PERIOD rather than an instant. Additive and defaulted: the six
    point-in-time inputs never had periods and still do not. It exists so a multi-period
    factor can be expressed in ``common/factors`` at all — before migration 0043 the
    transport carried one value per key, so module 1's growth rate had to be reduced in
    the capture adapter, which is factor arithmetic in L0 (init.md rule 2).
    """

    issuer_id: str
    records: Mapping[str, tuple[Decimal, Decimal]]
    knowable_at: Mapping[str, datetime]
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


class LookAheadError(ValueError):
    """Raised when an input's `knowable_at` is strictly after the evaluation `cutoff_at` (#1413 D6)."""


def _has_stale_required_input(
    issuer: IssuerInput, keys: Sequence[str], *, as_of: datetime, maximum_age: timedelta
) -> bool:
    """Whether the latest observation of any required input is older than ``maximum_age`` (#1255).

    The test reads one instant per key. A periodic key answers with its latest period, so its older
    periods never count. An age equal to the limit is not stale. A required key without a
    ``knowable_at`` entry raises: a producer that skips the rule must fail, never pass.
    Any input with ``knowable_at > as_of`` raises ``LookAheadError`` immediately (#1413 D6).
    """
    for key, knowable_instant in issuer.knowable_at.items():
        if knowable_instant > as_of:
            raise LookAheadError(
                f"{issuer.issuer_id}: input {key!r} has knowable_at {knowable_instant.isoformat()} > cutoff {as_of.isoformat()}"
            )
    unknown = [key for key in keys if key not in issuer.knowable_at]
    if unknown:
        raise ValueError(f"{issuer.issuer_id}: knowable_at lacks the required inputs {unknown}")
    return any(as_of - issuer.knowable_at[key] > maximum_age for key in keys)


def _evaluate_issuer(
    issuer: IssuerInput,
    *,
    definition: LargeModelValueV0Definition,
    risk_free_rate: Decimal,
    as_of: datetime,
    trees: Mapping[IssuerClass, MetricTree],
    forest: Forest,
) -> tuple[EvaluatedDecision, Decimal | None]:
    """Return this issuer's pre-ranking decision plus its valuation gap (or None
    when it is not a ranking candidate)."""

    labor_q = definition.labor_efficiency.quantization
    ps_q = definition.price_to_sales.quantization
    multiple_q = definition.valuation_quantization
    tier_q = definition.tier_valuation.quantization
    gap_q = definition.valuation_gap.quantization
    basis = definition.valuation_basis

    # An issuer with no class, or a class with no pin, has no tree and so no list of consumed inputs. It is
    # excluded first. Every other issuer is tested on the inputs of its own pinned tree (#1518).
    tree = None if issuer.issuer_class is None else trees.get(issuer.issuer_class)
    if issuer.issuer_class is None or tree is None:
        return _excluded(issuer.issuer_id, ExclusionReason.MISSING_ISSUER_CLASS, confidence=None), None
    metric = tree.root
    required = _required_inputs(basis, _consumed_input_keys(forest, {issuer.issuer_class: tree}))
    missing = next((reason for key, reason in required if key not in issuer.records), None)
    if missing is not None:
        return _excluded(issuer.issuer_id, missing, confidence=None, labor_efficiency_metric=metric), None

    # The age limit comes after the missing-input rule and before the confidence floor (#1255).
    if _has_stale_required_input(
        issuer,
        [key for key, _reason in required],
        as_of=as_of,
        maximum_age=timedelta(days=definition.eligibility.maximum_input_age_days),
    ):
        return (
            _excluded(
                issuer.issuer_id,
                ExclusionReason.STALE_REQUIRED_INPUT,
                confidence=None,
                labor_efficiency_metric=metric,
            ),
            None,
        )

    consumed_confidence = min(issuer.records[key][1] for key, _reason in required)
    if consumed_confidence < definition.eligibility.minimum_confidence:
        return (
            _excluded(
                issuer.issuer_id,
                ExclusionReason.BELOW_CONFIDENCE_FLOOR,
                confidence=consumed_confidence,
                labor_efficiency_metric=metric,
            ),
            None,
        )

    gppe_result = gross_profit_per_employee(
        _facts_for(issuer, _labor_efficiency_keys(forest, tree, issuer.issuer_class), as_of=as_of),
        entity_id=issuer.issuer_id,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
        forest=forest,
        tree=tree,
        issuer_class=issuer.issuer_class,
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
            _excluded(issuer.issuer_id, reason, confidence=consumed_confidence, labor_efficiency_metric=tree.root),
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
    tier_result = three_tier_valuation(
        [
            gppe_result.model_copy(update={"value": labor_efficiency}),
            multiple_result.model_copy(update={"value": current_multiple}),
        ],
        entity_id=issuer.issuer_id,
        as_of=as_of,
        definition=definition,
    )
    band = tier_band(definition, labor_efficiency)
    target_multiple = _quantize((band.target_lower_bound + band.target_upper_bound) / Decimal(2), tier_q)
    if tier_result.value is None:
        return (
            _excluded(
                issuer.issuer_id,
                ExclusionReason.MISSING_MARKET_VALUE_INPUT,
                confidence=tier_result.confidence,
                labor_efficiency_metric=tree.root,
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
        labor_efficiency_metric=tree.root,
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
    tree_pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
    forest: Forest = FOREST,
    plausibility_refusals: Mapping[str, ExclusionReason] = MappingProxyType({}),
) -> list[EvaluatedDecision]:
    """Evaluate every issuer at one cutoff, then rank/select/weight the eligible,
    in-band candidates. Returns decisions sorted by issuer id.

    The labor-efficiency tree of each issuer class is the tree that ``definition`` pins
    (`factors.forest.bound_tree`). A pin that differs from the registered tree raises before any
    issuer is evaluated. An issuer whose class has no pin, or no class, is excluded with
    `missing_issuer_class`. A definition that states a formula and pins no tree (1.0.0) raises,
    unless the caller passes ``tree_pins``. Only the replay of the frozen #21 golden does that,
    with `factors.forest.uniform_tree_pins()`.

    An issuer is excluded with `stale_required_input` when any required input became knowable more
    than `eligibility.maximum_input_age_days` before `cutoff_at` (#1255).

    `plausibility_refusals` maps an issuer id to the reason the plausibility gate refused its
    published row (#1323). That issuer is excluded with the reason before ranking, so the other
    issuers are ranked and weighted without it."""
    pins = definition_pins(definition) if tree_pins is None else tree_pins
    trees = {
        IssuerClass(name): bound_tree(definition, IssuerClass(name), forest=forest, pins=tree_pins) for name in pins
    }

    decisions = [
        _excluded(issuer.issuer_id, plausibility_refusals[issuer.issuer_id], confidence=None)
        if issuer.issuer_id in plausibility_refusals
        else _evaluate_issuer(
            issuer,
            definition=definition,
            risk_free_rate=risk_free_rate,
            as_of=cutoff_at,
            trees=trees,
            forest=forest,
        )[0]
        for issuer in issuers
    ]
    return rank_and_select(decisions, definition=definition)
