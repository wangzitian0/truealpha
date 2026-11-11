"""Single-source evaluator for the large_model_value_v0 decision algorithm (#393, #1235, #1518)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType

from truealpha_contracts.fiscal_period import FiscalPeriod as AnnualFiscalPeriod
from truealpha_contracts.fiscal_period import parse_annual
from truealpha_contracts.measure import FiscalPeriod, Measure
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
from factors.measure import UNIT_MISMATCH_REASON, measure_for_input_key
from factors.panel import (
    PANEL_METRICS,
    AliasKind,
    FactorVariantSpec,
    IssuerClass,
    PanelMetricSpec,
    bound_tree,
    definition_pins,
    input_key_of,
    operating_numerator_node,
    required_inputs,
)
from factors.registry import FACTOR_REGISTRY
from factors.types import Fact, FactorResult, GrowthConvention


def _input_keys(factor_name: str, *, issuer_class: IssuerClass | None = None) -> tuple[str, ...]:
    keys = tuple(input_key_for_metric(metric) for metric in FACTOR_REGISTRY[factor_name].inputs)
    if factor_name == "price_to_gross_profit" and issuer_class is IssuerClass.FINANCIAL:
        return tuple("pre_provision_profit" if k == "gross_profit" else k for k in keys)
    return keys


_PEG_CAGR_YEARS = 3
_PEG_WINDOW_FLAG_PREFIXES = ("cagr_years:", "window:")
_METRIC_FOR_KEY = INPUT_KEY_ALIASES

_REQUIRED_INPUT_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    ("gross_profit", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
    ("pre_provision_profit", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
    ("bank_net_revenue", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
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

_REFUSAL_FLAG_REASONS: tuple[tuple[str, ExclusionReason], ...] = (
    (NEGATIVE_TCE_FLAG, ExclusionReason.NEGATIVE_TANGIBLE_COMMON_EQUITY),
    (NONPOSITIVE_EMPLOYEES_FLAG, ExclusionReason.NONPOSITIVE_HEADCOUNT),
    ("nonpositive_gross_profit", ExclusionReason.NONPOSITIVE_GROSS_PROFIT),
    (UNIT_MISMATCH_REASON, ExclusionReason.MISSING_MARKET_VALUE_INPUT),
)

_MULTIPLE_FACTOR = {
    ValuationBasis.PRICE_TO_SALES: price_to_sales,
    ValuationBasis.PRICE_TO_GROSS_PROFIT: price_to_gross_profit,
}


def _recorded_only_input_keys(basis: ValuationBasis) -> frozenset[str]:
    if basis is ValuationBasis.PRICE_TO_SALES:
        return frozenset()
    return frozenset(_input_keys("price_to_sales")) - frozenset(_input_keys(basis.value))


def _required_inputs(basis: ValuationBasis, consumed: frozenset[str]) -> tuple[tuple[str, ExclusionReason], ...]:
    recorded_only = _recorded_only_input_keys(basis)
    return tuple(
        (key, reason) for key, reason in _REQUIRED_INPUT_REASONS if key in consumed and key not in recorded_only
    )


def _labor_efficiency_keys(
    metrics: Mapping[str, PanelMetricSpec],
    tree: FactorVariantSpec,
    issuer_class: IssuerClass,
) -> list[str]:
    return [
        input_key_of(node)
        for node in required_inputs(tree, issuer_class, metrics=metrics)
        if node.alias(AliasKind.DEFINITION_PARAMETER) is None
    ]


def _consumed_input_keys(
    metrics: Mapping[str, PanelMetricSpec],
    trees: Mapping[IssuerClass, FactorVariantSpec],
) -> frozenset[str]:
    keys = set(_input_keys("price_to_sales"))
    for issuer_class, tree in trees.items():
        keys.update(_labor_efficiency_keys(metrics, tree, issuer_class))
    return frozenset(keys)


def required_input_keys(
    definition: LargeModelValueV0Definition,
    issuer_class: IssuerClass | None = None,
    *,
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
    pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
) -> frozenset[str]:
    pinned = definition_pins(definition) if pins is None else pins
    classes = [IssuerClass(name) for name in pinned] if issuer_class is None else [issuer_class]
    return _consumed_input_keys(
        metrics, {each: bound_tree(definition, each, metrics=metrics, pins=pins) for each in classes}
    )


def deciding_input_keys(
    definition: LargeModelValueV0Definition,
    issuer_class: IssuerClass,
    *,
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
    pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
) -> frozenset[str]:
    return frozenset(
        key
        for key, _reason in _required_inputs(
            definition.valuation_basis, required_input_keys(definition, issuer_class, metrics=metrics, pins=pins)
        )
    )


def _as_measure(
    key: str,
    raw: Measure | Decimal,
    *,
    subject: str,
    knowable_at: datetime | None,
    period: FiscalPeriod | None = None,
) -> Measure:
    if isinstance(raw, Measure):
        return raw
    return measure_for_input_key(key, raw, subject=subject, period=period, knowable_at=knowable_at)


@dataclass(frozen=True)
class IssuerInput:
    """One issuer's provenance-neutral factor inputs at a single cutoff."""

    issuer_id: str
    records: Mapping[str, tuple[Measure | Decimal, Decimal]]
    knowable_at: Mapping[str, datetime]
    periodic_records: Mapping[str, Mapping[str, tuple[Measure | Decimal, Decimal]]] = field(default_factory=dict)
    issuer_class: IssuerClass | None = None

    def __post_init__(self) -> None:
        normalized_records = {
            key: (_as_measure(key, val, subject=self.issuer_id, knowable_at=self.knowable_at.get(key)), conf)
            for key, (val, conf) in self.records.items()
        }
        object.__setattr__(self, "records", MappingProxyType(normalized_records))
        normalized_periodic = {
            key: MappingProxyType(
                {
                    period: (
                        _as_measure(
                            key, val, subject=self.issuer_id, period=period, knowable_at=self.knowable_at.get(key)
                        ),
                        conf,
                    )
                    for period, (val, conf) in by_period.items()
                }
            )
            for key, by_period in self.periodic_records.items()
        }
        object.__setattr__(self, "periodic_records", MappingProxyType(normalized_periodic))


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
    peg: Decimal | None = None
    peg_rank: int | None = None
    peg_reason_codes: tuple[str, ...] = ()
    labor_efficiency_metric: str | None = None
    current_price_to_gross_profit: Decimal | None = None
    target_price_to_gross_profit: Decimal | None = None


def peg_reason_codes(result: FactorResult) -> tuple[str, ...]:
    if result.value is not None:
        return ()
    return tuple(flag for flag in result.flags if not flag.startswith(_PEG_WINDOW_FLAG_PREFIXES))


def _quantize(value: Decimal, quantization: DecimalQuantization) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-quantization.decimal_places), ROUND_HALF_EVEN)


def _resolve_bank_numerator_key(key: str, mapping: Mapping[str, object]) -> str:
    if key in mapping:
        return key
    if key in ("pre_provision_profit", "bank_net_revenue"):
        for fallback in ("pre_provision_profit", "gross_profit"):
            if fallback in mapping:
                return fallback
    return key


def _make_fact(
    issuer_id: str, key: str, measure: Measure, confidence: Decimal, as_of: datetime, period: str | None = None
) -> Fact:
    metric = _METRIC_FOR_KEY.get(key, key)
    return Fact(
        entity_id=issuer_id,
        metric=metric,
        value=measure.value,
        unit_family=METRICS[metric].unit_family,
        unit=measure.unit,
        confidence=confidence,
        as_of=as_of,
        fiscal_period=period,
    )


def _facts_for(issuer: IssuerInput, keys: Sequence[str], *, as_of: datetime) -> list[Fact]:
    facts: list[Fact] = []
    for key in keys:
        if key in issuer.periodic_records:
            for period, (raw_measure, confidence) in sorted(issuer.periodic_records[key].items()):
                measure = _as_measure(key, raw_measure, subject=issuer.issuer_id, period=period, knowable_at=as_of)
                facts.append(_make_fact(issuer.issuer_id, key, measure, confidence, as_of, period=period))
            continue
        record = issuer.records.get(_resolve_bank_numerator_key(key, issuer.records))
        if record is None:
            continue
        raw_measure, confidence = record
        measure = _as_measure(key, raw_measure, subject=issuer.issuer_id, knowable_at=as_of)
        facts.append(_make_fact(issuer.issuer_id, key, measure, confidence, as_of))
    return facts


def _period_end_of(period: object) -> date | None:
    if isinstance(period, AnnualFiscalPeriod):
        return period.end
    if isinstance(period, date):
        return period
    if isinstance(period, tuple) and len(period) == 2 and isinstance(period[1], date):
        return period[1]
    if isinstance(period, str):
        if parsed := parse_annual(period):
            return parsed.end
        try:
            return date.fromisoformat(period)
        except ValueError:
            return None
    return None


def _has_stale_period(issuer: IssuerInput, keys: Sequence[str], *, as_of: datetime, maximum_age: timedelta) -> bool:
    cutoff_day = as_of.astimezone(UTC).date()
    for key in keys:
        record = issuer.records.get(_resolve_bank_numerator_key(key, issuer.records))
        if record is None or not isinstance(record[0], Measure):
            continue
        end = _period_end_of(record[0].period)
        if end is not None and (cutoff_day - end) > maximum_age:
            return True
    return False


class LookAheadError(ValueError):
    """Raised when an input's `knowable_at` is strictly after the evaluation `cutoff_at`."""


def _has_stale_required_input(
    issuer: IssuerInput, keys: Sequence[str], *, as_of: datetime, maximum_age: timedelta
) -> bool:
    for key, knowable_instant in issuer.knowable_at.items():
        if knowable_instant > as_of:
            raise LookAheadError(
                f"{issuer.issuer_id}: input {key!r} has knowable_at {knowable_instant.isoformat()} > cutoff {as_of.isoformat()}"
            )
    resolved_keys = [_resolve_bank_numerator_key(key, issuer.knowable_at) for key in keys]
    if unknown := [key for key in resolved_keys if key not in issuer.knowable_at]:
        raise ValueError(f"{issuer.issuer_id}: knowable_at lacks the required inputs {unknown}")
    return any(as_of - issuer.knowable_at[key] > maximum_age for key in resolved_keys)


def _missing_required_reason(
    issuer: IssuerInput, required: Sequence[tuple[str, ExclusionReason]]
) -> ExclusionReason | None:
    return next(
        (reason for key, reason in required if _resolve_bank_numerator_key(key, issuer.records) not in issuer.records),
        None,
    )


def _consumed_confidence_for(issuer: IssuerInput, required: Sequence[tuple[str, ExclusionReason]]) -> Decimal:
    return min(issuer.records[_resolve_bank_numerator_key(key, issuer.records)][1] for key, _reason in required)


def _multiple_facts_for(
    issuer: IssuerInput,
    basis: ValuationBasis,
    as_of: datetime,
    *,
    tree: FactorVariantSpec | None = None,
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
) -> list[Fact]:
    keys = list(_input_keys(basis.value, issuer_class=issuer.issuer_class))
    if basis is ValuationBasis.PRICE_TO_GROSS_PROFIT and tree is not None and issuer.issuer_class is not None:
        num_key = input_key_of(operating_numerator_node(tree, issuer.issuer_class, metrics=metrics))
        keys = [num_key if k in ("gross_profit", "pre_provision_profit") else k for k in keys]
    facts = _facts_for(issuer, keys, as_of=as_of)
    if basis is ValuationBasis.PRICE_TO_GROSS_PROFIT and not any(f.metric == "gross_profit" for f in facts):
        proxy = next((f for f in facts if f.metric in ("pre_provision_profit", "bank_net_revenue")), None)
        if proxy is not None:
            return [*facts, proxy.model_copy(update={"metric": "gross_profit"})]
    return facts


def _check_issuer_eligibility(
    issuer: IssuerInput,
    *,
    definition: LargeModelValueV0Definition,
    as_of: datetime,
    tree: FactorVariantSpec,
    metrics: Mapping[str, PanelMetricSpec],
) -> tuple[EvaluatedDecision | None, Decimal]:
    assert issuer.issuer_class is not None
    metric = tree.root
    consumed = _consumed_input_keys(metrics, {issuer.issuer_class: tree})
    required = _required_inputs(definition.valuation_basis, consumed)
    if (missing := _missing_required_reason(issuer, required)) is not None:
        return _excluded(issuer.issuer_id, missing, confidence=None, labor_efficiency_metric=metric), Decimal(0)
    max_age = timedelta(days=definition.eligibility.maximum_input_age_days)
    if _has_stale_required_input(issuer, [k for k, _ in required], as_of=as_of, maximum_age=max_age):
        return (
            _excluded(
                issuer.issuer_id,
                ExclusionReason.STALE_REQUIRED_INPUT,
                confidence=None,
                labor_efficiency_metric=metric,
            ),
            Decimal(0),
        )
    if _has_stale_period(issuer, sorted(consumed), as_of=as_of, maximum_age=max_age):
        return (
            _excluded(issuer.issuer_id, ExclusionReason.STALE_PERIOD, confidence=None, labor_efficiency_metric=metric),
            Decimal(0),
        )
    consumed_confidence = _consumed_confidence_for(issuer, required)
    if (
        definition.eligibility.minimum_confidence is not None
        and consumed_confidence < definition.eligibility.minimum_confidence
    ):
        return (
            _excluded(
                issuer.issuer_id,
                ExclusionReason.BELOW_CONFIDENCE_FLOOR,
                confidence=consumed_confidence,
                labor_efficiency_metric=metric,
            ),
            Decimal(0),
        )
    return None, consumed_confidence


def _evaluate_issuer(
    issuer: IssuerInput,
    *,
    definition: LargeModelValueV0Definition,
    risk_free_rate: Decimal,
    as_of: datetime,
    trees: Mapping[IssuerClass, FactorVariantSpec],
    metrics: Mapping[str, PanelMetricSpec],
) -> tuple[EvaluatedDecision, Decimal | None]:
    tree = None if issuer.issuer_class is None else trees.get(issuer.issuer_class)
    if issuer.issuer_class is None or tree is None:
        return _excluded(issuer.issuer_id, ExclusionReason.MISSING_ISSUER_CLASS, confidence=None), None

    rejection, consumed_confidence = _check_issuer_eligibility(
        issuer, definition=definition, as_of=as_of, tree=tree, metrics=metrics
    )
    if rejection is not None:
        return rejection, None

    basis = definition.valuation_basis
    gppe_result = gross_profit_per_employee(
        _facts_for(issuer, _labor_efficiency_keys(metrics, tree, issuer.issuer_class), as_of=as_of),
        entity_id=issuer.issuer_id,
        as_of=as_of,
        risk_free_rate=risk_free_rate,
        tree=tree,
        issuer_class=issuer.issuer_class,
        metrics=metrics,
    )
    ps_result = price_to_sales(
        _facts_for(issuer, _input_keys("price_to_sales"), as_of=as_of), entity_id=issuer.issuer_id, as_of=as_of
    )
    multiple_result = (
        ps_result
        if basis is ValuationBasis.PRICE_TO_SALES
        else _MULTIPLE_FACTOR[basis](
            _multiple_facts_for(issuer, basis, as_of, tree=tree, metrics=metrics),
            entity_id=issuer.issuer_id,
            as_of=as_of,
        )
    )
    if gppe_result.value is None or multiple_result.value is None:
        flags = (*gppe_result.flags, *multiple_result.flags)
        reason = next(
            (r for flag, r in _REFUSAL_FLAG_REASONS if flag in flags),
            ExclusionReason.STALE_REQUIRED_INPUT,
        )
        return (
            _excluded(issuer.issuer_id, reason, confidence=consumed_confidence, labor_efficiency_metric=tree.root),
            None,
        )

    peg_result = peg(
        _facts_for(issuer, _input_keys("peg"), as_of=as_of),
        entity_id=issuer.issuer_id,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=as_of,
        cagr_years=_PEG_CAGR_YEARS,
    )
    labor_efficiency = _quantize(gppe_result.value, definition.labor_efficiency.quantization)
    current_ps = None if ps_result.value is None else _quantize(ps_result.value, definition.price_to_sales.quantization)
    current_multiple = _quantize(multiple_result.value, definition.valuation_quantization)
    tier_result = three_tier_valuation(
        [
            gppe_result.model_copy(update={"value": labor_efficiency}),
            multiple_result.model_copy(update={"value": current_multiple}),
        ],
        entity_id=issuer.issuer_id,
        as_of=as_of,
        definition=definition,
    )
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

    band = tier_band(definition, labor_efficiency)
    target_multiple = _quantize(
        (band.target_lower_bound + band.target_upper_bound) / Decimal(2), definition.tier_valuation.quantization
    )
    valuation_gap = _quantize(tier_result.value, definition.valuation_gap.quantization)
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
        peg=peg_result.value,
        peg_reason_codes=peg_reason_codes(peg_result),
        eligible=True,
        outcome=GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT,
        exclusion_reason=None,
        confidence=tier_result.confidence,
        labor_efficiency_metric=tree.root,
    )
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
    peg_positions = {
        d.issuer_id: pos
        for pos, d in enumerate(
            sorted((d for d in decisions if d.peg is not None), key=lambda d: (d.peg, d.issuer_id)), start=1
        )
    }
    with_peg = [replace(d, peg_rank=peg_positions.get(d.issuer_id)) for d in decisions]
    candidates = sorted(
        (d for d in with_peg if d.outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT),
        key=lambda d: (-(d.valuation_gap or Decimal(0)), d.issuer_id),
    )
    resolved = [d for d in with_peg if d.outcome is not GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT]
    selection_count = definition.selection.selection_count
    weight = _quantize(
        Decimal(1) / Decimal(max(min(len(candidates), selection_count), 1)), definition.sizing.quantization
    )
    for position, decision in enumerate(candidates, start=1):
        if position <= selection_count:
            resolved.append(
                replace(decision, outcome=GoldenDecisionOutcome.SELECTED, rank=position, target_weight=weight)
            )
        else:
            resolved.append(replace(decision, rank=position))
    return sorted(resolved, key=lambda d: d.issuer_id)


def evaluate_cutoff(
    issuers: Sequence[IssuerInput],
    *,
    definition: LargeModelValueV0Definition,
    cutoff_at: datetime,
    risk_free_rate: Decimal,
    tree_pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
    plausibility_refusals: Mapping[str, ExclusionReason] = MappingProxyType({}),
) -> list[EvaluatedDecision]:
    pins = definition_pins(definition) if tree_pins is None else tree_pins
    trees = {
        IssuerClass(name): bound_tree(definition, IssuerClass(name), metrics=metrics, pins=tree_pins) for name in pins
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
            metrics=metrics,
        )[0]
        for issuer in issuers
    ]
    return rank_and_select(decisions, definition=definition)
