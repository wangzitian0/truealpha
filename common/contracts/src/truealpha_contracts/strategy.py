"""Versioned ``large_model_value_v0`` strategy definitions and golden decisions."""

from __future__ import annotations

import functools
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from importlib import resources
from importlib.resources.abc import Traversable
from itertools import pairwise
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, Self

from pydantic import AfterValidator, Field, ValidationInfo, field_validator, model_validator

from truealpha_contracts.common import (
    AwareDatetime,
    ContentHash,
    OmitNoneFields,
    ParsedSafeDecimal,
    Sha256,
    StrictFrozenModel,
    StrictIdentifiedFrozenModel,
    content_id,
    content_ref,
    require_aware,
    require_stable_and_immutable,
)
from truealpha_contracts.common import (
    require_checks as _require_checks,
)
from truealpha_contracts.research import (
    StrategyExecutionPolicy,
    StrategyTotalReturnPolicy,
    StrategyTransactionCostPolicy,
    TargetPsSelection,
    ValuationTier,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

_STABLE_KEY_PATTERN = r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$"


def _immutable_field(value: str, info: ValidationInfo) -> str:
    return require_stable_and_immutable(
        value,
        info.field_name or "coordinate",
        mutable_tokens=frozenset({"current", "default", "head", "latest", "main", "master", "stable"}),
        pattern=None,
    )


_ImmutableKey = Annotated[str, AfterValidator(_immutable_field)]


def _require_all_or_none(model: Any, fields: tuple[str, ...], group_label: str) -> None:
    stated = {name: getattr(model, name) is not None for name in fields}
    if len(set(stated.values())) != 1:
        raise ValueError(f"the {group_label} fields {sorted(fields)} must be all present or all absent, got {stated}")


class FactorDimension(StrEnum):
    DIMENSIONLESS = "dimensionless"
    REPORTING_CURRENCY_PER_EMPLOYEE = "reporting_currency_per_employee"
    YEARS = "years"


class FactorUnitSpec(StrictFrozenModel):
    dimension: FactorDimension
    unit_code: str = Field(pattern=_STABLE_KEY_PATTERN)


class DecimalQuantization(StrictFrozenModel):
    decimal_places: int = Field(ge=0, le=12)
    rounding: Literal["half_even"]

    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.decimal_places)


class ExclusionReason(StrEnum):
    MISSING_GROSS_PROFIT_FACT = "missing_gross_profit_fact"
    MISSING_TOTAL_ASSETS_FACT = "missing_total_assets_fact"
    MISSING_HEADCOUNT_DISCLOSURE = "missing_headcount_disclosure"
    MISSING_LABOR_COST_DISCLOSURE = "missing_labor_cost_disclosure"
    MISSING_REVENUE_FACT = "missing_revenue_fact"
    MISSING_MARKET_VALUE_INPUT = "missing_market_value_input"
    MISSING_TANGIBLE_COMMON_EQUITY = "missing_tangible_common_equity"
    MISSING_PREFERRED_STOCK_VALUE = "missing_preferred_stock_value"
    NEGATIVE_TANGIBLE_COMMON_EQUITY = "negative_tangible_common_equity"
    MISSING_ISSUER_CLASS = "missing_issuer_class"
    MISSING_RISK_FREE_RATE_PARAMETER = "missing_risk_free_rate_parameter"
    BELOW_CONFIDENCE_FLOOR = "below_confidence_floor"
    STALE_REQUIRED_INPUT = "stale_required_input"
    STALE_PERIOD = "stale_period"
    NONPOSITIVE_HEADCOUNT = "nonpositive_headcount"
    NONPOSITIVE_LABOR_COST = "nonpositive_labor_cost"
    NONPOSITIVE_REVENUE = "nonpositive_revenue"
    NONPOSITIVE_GROSS_PROFIT = "nonpositive_gross_profit"
    PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY = "plausibility_refused_node_sign_policy"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    PRIMARY_LISTING_UNDECLARED = "primary_listing_undeclared"


class RiskFreeInstrument(StrEnum):
    US_TREASURY_BILL_3_MONTH = "us_treasury_bill_3_month"


class RiskFreeRatePolicy(StrictFrozenModel):
    rule_id: Literal["per_cutoff_versioned_parameter"]
    instrument: RiskFreeInstrument
    rate_unit: Literal["annualized_decimal_fraction"]
    missing_rate_reason: ExclusionReason

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.missing_rate_reason is not ExclusionReason.MISSING_RISK_FREE_RATE_PARAMETER:
            raise ValueError("a missing risk-free parameter must use its exact reason code")
        return self


class CutoffRiskFreeRate(StrictFrozenModel):
    cutoff_at: AwareDatetime
    annualized_rate: ParsedSafeDecimal = Field(ge=0, le=1)
    provisional: Literal[True]


class LaborEfficiencyNumerator(StrEnum):
    GROSS_PROFIT = "gross_profit"


class LaborEfficiencyDenominator(StrEnum):
    HEADCOUNT = "headcount"
    LABOR_COST_SALARIES_PLUS_ESOP = "labor_cost_salaries_plus_esop"


class CapitalChargeBase(StrEnum):
    TOTAL_ASSETS = "total_assets"


class _FactorDefinitionBase(StrictIdentifiedFrozenModel):
    id_field = "factor_definition_id"
    id_prefix = "factor-definition"
    factor_definition_id: str = content_id("factor-definition")
    content_sha256: ContentHash = ""
    factor_version: _ImmutableKey = Field(pattern=_VERSION_PATTERN)
    unit: FactorUnitSpec
    quantization: DecimalQuantization


class CapitalAdjustedLaborEfficiencyDefinition(OmitNoneFields, _FactorDefinitionBase):
    omit_none_fields = (
        "numerator",
        "capital_charge_base",
        "denominator",
        "formula",
        "missing_numerator_reason",
        "missing_capital_base_reason",
        "missing_denominator_reason",
        "nonpositive_denominator_reason",
    )
    factor_key: Literal["capital_adjusted_labor_efficiency"]
    numerator: LaborEfficiencyNumerator | None = None
    capital_charge_base: CapitalChargeBase | None = None
    denominator: LaborEfficiencyDenominator | None = None
    risk_free: RiskFreeRatePolicy
    formula: Literal["(numerator-capital_charge_base*risk_free_rate)/denominator"] | None = None
    missing_numerator_reason: ExclusionReason | None = None
    missing_capital_base_reason: ExclusionReason | None = None
    missing_denominator_reason: ExclusionReason | None = None
    nonpositive_denominator_reason: ExclusionReason | None = None

    def _canonicalize(self) -> None:
        _require_all_or_none(self, self.omit_none_fields, "formula")
        if self.formula is None:
            return
        expected_dim, expected_missing, expected_nonpos = (
            (
                FactorDimension.REPORTING_CURRENCY_PER_EMPLOYEE,
                ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE,
                ExclusionReason.NONPOSITIVE_HEADCOUNT,
            )
            if self.denominator is LaborEfficiencyDenominator.HEADCOUNT
            else (
                FactorDimension.DIMENSIONLESS,
                ExclusionReason.MISSING_LABOR_COST_DISCLOSURE,
                ExclusionReason.NONPOSITIVE_LABOR_COST,
            )
        )
        _require_checks(
            (
                self.numerator is LaborEfficiencyNumerator.GROSS_PROFIT
                and self.missing_numerator_reason is not ExclusionReason.MISSING_GROSS_PROFIT_FACT,
                "gross-profit numerator requires the missing_gross_profit_fact reason",
            ),
            (
                self.missing_capital_base_reason is not ExclusionReason.MISSING_TOTAL_ASSETS_FACT,
                "capital-charge base and its missing-input reason code are inconsistent",
            ),
            (
                self.unit.dimension is not expected_dim,
                "labor-efficiency unit dimension does not match the configured denominator",
            ),
            (
                self.missing_denominator_reason is not expected_missing,
                "denominator and its missing-input reason code are inconsistent",
            ),
            (
                self.nonpositive_denominator_reason is not expected_nonpos,
                "denominator and its nonpositive reason code are inconsistent",
            ),
        )


class LaborEfficiencyTreePin(StrictFrozenModel):
    tree_key: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    tree_version: _ImmutableKey = Field(min_length=1)
    tree_sha256: Sha256


class _MarketMultipleDefinitionBase(_FactorDefinitionBase):
    _multiple_spec: ClassVar[tuple[str, str, ExclusionReason, ExclusionReason]]
    market_value_rule: Literal["shares_outstanding_times_last_unadjusted_close"]
    price_rule: Literal["last_unadjusted_close_at_or_before_cutoff"]
    shares_rule: Literal["latest_knowable_shares_outstanding_at_or_before_cutoff"]
    missing_market_value_reason: ExclusionReason

    def _canonicalize(self) -> None:
        stem, label, exp_missing, exp_nonpos = self._multiple_spec
        if self.unit.dimension is not FactorDimension.DIMENSIONLESS:
            raise ValueError(f"{label} is a dimensionless ratio")
        item_name = stem.replace("_", " ")
        _require_checks(
            (
                self.missing_market_value_reason is not ExclusionReason.MISSING_MARKET_VALUE_INPUT,
                "missing market-value inputs must use their exact reason code",
            ),
            (
                getattr(self, f"missing_{stem}_reason") is not exp_missing,
                f"missing {item_name} must use its exact reason code",
            ),
            (
                getattr(self, f"nonpositive_{stem}_reason") is not exp_nonpos,
                f"nonpositive {item_name} must use its exact reason code",
            ),
        )


class PriceToSalesDefinition(_MarketMultipleDefinitionBase):
    _multiple_spec = (
        "revenue",
        "price-to-sales",
        ExclusionReason.MISSING_REVENUE_FACT,
        ExclusionReason.NONPOSITIVE_REVENUE,
    )
    factor_key: Literal["price_to_sales"]
    revenue_rule: Literal["latest_complete_fiscal_year_total_revenue"]
    missing_revenue_reason: ExclusionReason
    nonpositive_revenue_reason: ExclusionReason


class PriceToGrossProfitDefinition(_MarketMultipleDefinitionBase):
    _multiple_spec = (
        "gross_profit",
        "price-to-gross-profit",
        ExclusionReason.MISSING_GROSS_PROFIT_FACT,
        ExclusionReason.NONPOSITIVE_GROSS_PROFIT,
    )
    factor_key: Literal["price_to_gross_profit"]
    gross_profit_rule: Literal["latest_complete_fiscal_year_gross_profit"]
    missing_gross_profit_reason: ExclusionReason
    nonpositive_gross_profit_reason: ExclusionReason


class ValuationBasis(StrEnum):
    PRICE_TO_SALES = "price_to_sales"
    PRICE_TO_GROSS_PROFIT = "price_to_gross_profit"


_TIER_ORDER = {ValuationTier.TRADITIONAL: 0, ValuationTier.TECH: 1, ValuationTier.LARGE_MODEL_NATIVE: 2}


class _TierBandBase(StrictFrozenModel):
    target_stem: ClassVar[str]
    target_label: ClassVar[str]
    tier: ValuationTier
    labor_efficiency_lower_bound: ParsedSafeDecimal | None
    labor_efficiency_upper_bound: ParsedSafeDecimal | None

    @property
    def target_lower_bound(self) -> Decimal:
        return getattr(self, f"{self.target_stem}_lower_bound")

    @property
    def target_upper_bound(self) -> Decimal:
        return getattr(self, f"{self.target_stem}_upper_bound")

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if (
            self.labor_efficiency_lower_bound is not None
            and self.labor_efficiency_upper_bound is not None
            and self.labor_efficiency_lower_bound >= self.labor_efficiency_upper_bound
        ):
            raise ValueError("tier labor-efficiency lower bound must be below its upper bound")
        if self.target_lower_bound >= self.target_upper_bound:
            raise ValueError(f"tier target {self.target_label} lower bound must be below its upper bound")
        return self


class ProvisionalTierBand(_TierBandBase):
    target_stem = "target_ps"
    target_label = "P/S"
    target_ps_lower_bound: ParsedSafeDecimal = Field(gt=0)
    target_ps_upper_bound: ParsedSafeDecimal = Field(gt=0)


class PriceToGrossProfitTierBand(_TierBandBase):
    target_stem = "target_pgp"
    target_label = "P/GP"
    target_pgp_lower_bound: ParsedSafeDecimal = Field(gt=0)
    target_pgp_upper_bound: ParsedSafeDecimal = Field(gt=0)


class _ThreeTierValuationBase(_FactorDefinitionBase):
    target_label: ClassVar[str]
    factor_key: Literal["three_tier_valuation"]
    boundary_rule: Literal["lower_inclusive_upper_exclusive"]

    def _canonicalize(self) -> None:
        if self.unit.dimension is not FactorDimension.DIMENSIONLESS:
            raise ValueError(f"target {self.target_label} is a dimensionless ratio")
        ordered: tuple[_TierBandBase, ...] = tuple(
            sorted(getattr(self, "bands"), key=lambda item: _TIER_ORDER[item.tier])
        )
        if {band.tier for band in ordered} != set(ValuationTier):
            raise ValueError("tier bands require exactly traditional, tech, and large-model-native")
        if ordered[0].labor_efficiency_lower_bound is not None or ordered[-1].labor_efficiency_upper_bound is not None:
            raise ValueError("tier bands must cover the complete labor-efficiency domain")
        for left, right in pairwise(ordered):
            if left.labor_efficiency_upper_bound != right.labor_efficiency_lower_bound:
                raise ValueError("tier bands must be contiguous and unambiguous")
        object.__setattr__(self, "bands", ordered)

    def band_for(self, labor_efficiency: Decimal) -> Any:
        for band in getattr(self, "bands"):
            lo_ok = band.labor_efficiency_lower_bound is None or labor_efficiency >= band.labor_efficiency_lower_bound
            hi_ok = band.labor_efficiency_upper_bound is None or labor_efficiency < band.labor_efficiency_upper_bound
            if lo_ok and hi_ok:
                return band
        raise ValueError("tier bands do not cover the supplied labor efficiency")


class ThreeTierValuationDefinition(_ThreeTierValuationBase):
    target_label = "P/S"
    bands: tuple[ProvisionalTierBand, ...] = Field(min_length=3, max_length=3)
    target_ps_selection: TargetPsSelection
    provisional_thresholds: Literal[True]

    @property
    def valuation_basis(self) -> ValuationBasis:
        return ValuationBasis.PRICE_TO_SALES


class ThreeTierPgpValuationDefinition(_ThreeTierValuationBase):
    target_label = "P/GP"
    valuation_basis: Literal[ValuationBasis.PRICE_TO_GROSS_PROFIT]
    bands: tuple[PriceToGrossProfitTierBand, ...] = Field(min_length=3, max_length=3)
    target_pgp_selection: Literal["band_midpoint"]
    provisional_thresholds: bool

    @field_validator("valuation_basis", mode="before")
    @classmethod
    def parse_basis(cls, value: object) -> object:
        return ValuationBasis(value) if isinstance(value, str) else value


_GAP_RULES: dict[ValuationBasis, tuple[str, str]] = {
    ValuationBasis.PRICE_TO_SALES: ("target_ps_over_current_ps_minus_one", "target_ps/current_ps-1"),
    ValuationBasis.PRICE_TO_GROSS_PROFIT: ("target_pgp_over_current_pgp_minus_one", "target_pgp/current_pgp-1"),
}


class ValuationGapRule(StrictFrozenModel):
    rule_id: Literal["target_ps_over_current_ps_minus_one", "target_pgp_over_current_pgp_minus_one"]
    formula: Literal["target_ps/current_ps-1", "target_pgp/current_pgp-1"]
    quantization: DecimalQuantization
    above_band_outcome: Literal["rejected_valuation_above_tier_band"]
    ranking_direction: Literal["descending_valuation_gap"]
    tie_break: Literal["ascending_issuer_id"]


class ConfidenceEligibilityRule(OmitNoneFields, StrictFrozenModel):
    omit_none_fields = ("rule_id", "minimum_confidence", "confidence_semantics", "below_floor_reason")
    rule_id: Literal["minimum_consumed_confidence_floor"] | None = None
    minimum_confidence: ParsedSafeDecimal | None = Field(default=None, ge=0, le=1)
    confidence_semantics: Literal["minimum_consumed_input_confidence"] | None = None
    maximum_input_age_days: int = Field(ge=1, le=730)
    below_floor_reason: ExclusionReason | None = None
    stale_input_reason: ExclusionReason

    @model_validator(mode="after")
    def validate_reasons(self) -> Self:
        _require_all_or_none(self, self.omit_none_fields, "confidence-floor")
        _require_checks(
            (
                self.below_floor_reason is not None
                and self.below_floor_reason is not ExclusionReason.BELOW_CONFIDENCE_FLOOR,
                "the confidence floor must use its exact reason code",
            ),
            (
                self.stale_input_reason is not ExclusionReason.STALE_REQUIRED_INPUT,
                "stale inputs must use their exact reason code",
            ),
        )
        return self


class SelectionRule(StrictFrozenModel):
    rule_id: Literal["top_n_ranked_eligible"]
    selection_count: int = Field(ge=1, le=1000)


class EqualWeightSizingRule(StrictFrozenModel):
    rule_id: Literal["equal_weight_selected"]
    weight_formula: Literal["1/selected_count"]
    quantization: DecimalQuantization
    empty_selection_behavior: Literal["retain_all_cash"]


class QuarterlyCutoffSchedule(StrictFrozenModel):
    rule_id: Literal["calendar_quarter_end_utc"]
    first_cutoff_at: datetime
    cutoff_inclusive: Literal[True]

    @field_validator("first_cutoff_at")
    @classmethod
    def validate_first_cutoff_at(cls, value: datetime) -> datetime:
        canonical = require_aware(value, "first_cutoff_at").astimezone(UTC)
        _require_checks(
            (canonical.month not in {3, 6, 9, 12}, "quarterly cutoffs must land on calendar quarter ends"),
            (
                (canonical + timedelta(days=1)).month == canonical.month,
                "quarterly cutoffs must use the last calendar day of the quarter",
            ),
            (
                (canonical.hour, canonical.minute, canonical.second, canonical.microsecond) != (23, 59, 59, 0),
                "quarterly cutoffs must close the day at 23:59:59 UTC",
            ),
        )
        return canonical


class LargeModelValueV0Definition(OmitNoneFields, StrictIdentifiedFrozenModel):
    id_field = "strategy_definition_id"
    id_prefix = "strategy-definition"
    omit_none_fields = ("price_to_gross_profit", "universe", "labor_efficiency_tree_keys")
    strategy_definition_id: str = content_id("strategy-definition")
    content_sha256: ContentHash = ""
    strategy_id: Literal["large_model_value_v0"]
    definition_version: _ImmutableKey = Field(pattern=_VERSION_PATTERN)
    provisional_semantics: bool
    universe: UniverseRef | None = None
    schedule: QuarterlyCutoffSchedule
    labor_efficiency: CapitalAdjustedLaborEfficiencyDefinition
    labor_efficiency_tree_keys: dict[str, LaborEfficiencyTreePin] | None = None
    price_to_sales: PriceToSalesDefinition
    price_to_gross_profit: PriceToGrossProfitDefinition | None = None
    tier_valuation: ThreeTierValuationDefinition | ThreeTierPgpValuationDefinition
    valuation_gap: ValuationGapRule
    eligibility: ConfidenceEligibilityRule
    selection: SelectionRule
    sizing: EqualWeightSizingRule
    execution: StrategyExecutionPolicy
    transaction_cost: StrategyTransactionCostPolicy
    total_return: StrategyTotalReturnPolicy

    def _canonicalize(self) -> None:
        basis = self.tier_valuation.valuation_basis
        _require_checks(
            (
                (basis is ValuationBasis.PRICE_TO_GROSS_PROFIT) != (self.price_to_gross_profit is not None),
                "a P/GP tier valuation requires exactly the price_to_gross_profit factor definition",
            ),
            (
                (self.valuation_gap.rule_id, self.valuation_gap.formula) != _GAP_RULES[basis],
                "the valuation-gap rule must use the multiple of the tier bands",
            ),
            (
                self.provisional_semantics != self.tier_valuation.provisional_thresholds,
                "provisional_semantics must equal the tier valuation's provisional_thresholds",
            ),
            (
                (self.labor_efficiency.formula is None) != (self.labor_efficiency_tree_keys is not None),
                "labor efficiency has one owner: the formula of labor_efficiency, or the trees of "
                "labor_efficiency_tree_keys. Exactly one must be present",
            ),
            (
                self.labor_efficiency_tree_keys is not None and not self.labor_efficiency_tree_keys,
                "labor_efficiency_tree_keys must pin at least one issuer class",
            ),
        )

    @property
    def valuation_basis(self) -> ValuationBasis:
        return self.tier_valuation.valuation_basis

    @property
    def valuation_quantization(self) -> DecimalQuantization:
        return (self.price_to_gross_profit or self.price_to_sales).quantization


LIVE_STRATEGY_DEFINITION_RESOURCE = "large_model_value_v0_definition_1_1_0.json"


@functools.lru_cache(maxsize=16)
def load_strategy_definition_corpus(resource: str) -> dict[str, Any]:
    payload = json.loads(resources.files("truealpha_contracts.data").joinpath(resource).read_bytes())
    if not isinstance(payload, dict):
        raise ValueError(f"{resource} must decode to a JSON object, got {type(payload).__name__}")
    return payload


def _definition_of_corpus(resource: str, corpus: dict[str, Any]) -> LargeModelValueV0Definition:
    definition = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
    pinned = corpus["expected"]["strategy_definition_id"]
    if definition.strategy_definition_id != pinned:
        raise ValueError(f"{resource}: definition id {definition.strategy_definition_id} != pinned {pinned}")
    return definition


@functools.lru_cache(maxsize=16)
def load_strategy_definition(resource: str) -> LargeModelValueV0Definition:
    return _definition_of_corpus(resource, load_strategy_definition_corpus(resource))


def load_live_strategy_definition() -> LargeModelValueV0Definition:
    return load_strategy_definition(LIVE_STRATEGY_DEFINITION_RESOURCE)


def load_live_strategy_corpus() -> dict[str, Any]:
    return load_strategy_definition_corpus(LIVE_STRATEGY_DEFINITION_RESOURCE)


class StrategyDefinitionIdentity(NamedTuple):
    version: str
    content_sha256: str
    provisional_semantics: bool


DefinitionReviewState = Literal["reviewed", "provisional", "unknown"]


def _identity_of(d: LargeModelValueV0Definition) -> StrategyDefinitionIdentity:
    return StrategyDefinitionIdentity(d.definition_version, d.content_sha256, d.provisional_semantics)


def strategy_definitions_in(directory: Traversable) -> tuple[StrategyDefinitionIdentity, ...]:
    entries: list[StrategyDefinitionIdentity] = []
    for item in sorted(directory.iterdir(), key=lambda e: e.name):
        if item.name.endswith(".json"):
            corpus = json.loads(item.read_bytes())
            if isinstance(corpus, dict) and "strategy_definition" in corpus:
                definition = _definition_of_corpus(item.name, corpus)
                if any(e.version == definition.definition_version for e in entries):
                    raise ValueError(
                        f"{item.name}: a second definition file has version {definition.definition_version}"
                    )
                entries.append(_identity_of(definition))
    return tuple(sorted(entries))


@functools.cache
def _packaged_strategy_definitions() -> tuple[StrategyDefinitionIdentity, ...]:
    return strategy_definitions_in(resources.files("truealpha_contracts.data"))


_REGISTERED_IN_CONTEXT: ContextVar[tuple[StrategyDefinitionIdentity, ...]] = ContextVar(
    "truealpha_registered_strategy_definitions", default=()
)


def strategy_definition_registry() -> tuple[StrategyDefinitionIdentity, ...]:
    return tuple(sorted(_packaged_strategy_definitions() + _REGISTERED_IN_CONTEXT.get()))


@contextmanager
def registered_strategy_definition(definition: LargeModelValueV0Definition) -> Iterator[StrategyDefinitionIdentity]:
    identity = _identity_of(LargeModelValueV0Definition.model_validate_json(definition.model_dump_json()))
    if any(
        e.version == identity.version or e.content_sha256 == identity.content_sha256
        for e in strategy_definition_registry()
    ):
        raise ValueError(f"the registry already holds version {identity.version} or its content hash")
    token = _REGISTERED_IN_CONTEXT.set(_REGISTERED_IN_CONTEXT.get() + (identity,))
    try:
        yield identity
    finally:
        _REGISTERED_IN_CONTEXT.reset(token)


def definition_review_state(version: str, content_sha256: str) -> DefinitionReviewState:
    for entry in strategy_definition_registry():
        if entry.content_sha256 == content_sha256 and entry.version == version:
            return "provisional" if entry.provisional_semantics else "reviewed"
    return "unknown"


class GoldenDecisionOutcome(StrEnum):
    SELECTED = "selected"
    RANKED_BEYOND_SELECTION_COUNT = "ranked_beyond_selection_count"
    REJECTED_VALUATION_ABOVE_TIER_BAND = "rejected_valuation_above_tier_band"
    EXCLUDED = "excluded"


class GoldenInputRecord(StrictFrozenModel):
    input_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    value: ParsedSafeDecimal
    unit_code: str = Field(pattern=_STABLE_KEY_PATTERN)
    confidence: ParsedSafeDecimal = Field(gt=0, le=1)
    knowable_at: AwareDatetime
    grounding: str = Field(min_length=1)


_OUTCOME_SHAPE = {
    GoldenDecisionOutcome.SELECTED: (True, True, "a selected decision requires a rank and a target weight"),
    GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT: (
        True,
        False,
        "a ranked-beyond decision carries a rank and no weight",
    ),
    GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND: (
        False,
        False,
        "a valuation-rejected decision carries no rank or weight",
    ),
}


class GoldenExpectation(OmitNoneFields, StrictFrozenModel):
    omit_none_fields = ("current_price_to_gross_profit", "target_price_to_gross_profit")
    capital_adjusted_labor_efficiency: ParsedSafeDecimal | None
    tier: ValuationTier | None
    current_price_to_sales: ParsedSafeDecimal | None
    target_price_to_sales: ParsedSafeDecimal | None
    valuation_gap: ParsedSafeDecimal | None
    eligible: bool
    outcome: GoldenDecisionOutcome
    exclusion_reason: ExclusionReason | None
    rank: int | None = Field(ge=1)
    target_weight: ParsedSafeDecimal | None = Field(ge=0, le=1)
    current_price_to_gross_profit: ParsedSafeDecimal | None = None
    target_price_to_gross_profit: ParsedSafeDecimal | None = None

    @model_validator(mode="after")
    def validate_outcome_consistency(self) -> Self:
        pgp = (self.current_price_to_gross_profit, self.target_price_to_gross_profit)
        if self.outcome is GoldenDecisionOutcome.EXCLUDED:
            _require_checks(
                (self.exclusion_reason is None, "an excluded decision requires a machine-readable reason code"),
                (
                    self.eligible or self.rank is not None or self.target_weight is not None,
                    "an excluded decision cannot be eligible, ranked, or weighted",
                ),
                (any(v is not None for v in pgp), "an excluded decision carries no P/GP value"),
            )
            return self
        by_ps = self.target_price_to_sales is not None and all(v is None for v in pgp)
        by_pgp = self.target_price_to_sales is None and all(v is not None for v in pgp)
        factors = (self.capital_adjusted_labor_efficiency, self.tier, self.current_price_to_sales, self.valuation_gap)
        exp_rank, exp_weight, shape_msg = _OUTCOME_SHAPE[self.outcome]
        _require_checks(
            (self.exclusion_reason is not None, "only excluded decisions carry an exclusion reason"),
            (not self.eligible, "non-excluded decisions passed eligibility"),
            (any(v is None for v in factors), "eligible decisions require every expected factor value"),
            (not (by_ps or by_pgp), "eligible decisions require the target multiple of exactly one valuation basis"),
            ((self.rank is not None) != exp_rank or (self.target_weight is not None) != exp_weight, shape_msg),
        )
        return self


class GoldenDecision(StrictFrozenModel):
    decision_key: _ImmutableKey = Field(pattern=_STABLE_KEY_PATTERN)
    issuer: SubjectRef
    issuer_branch: Literal["non_financial", "financial"]
    cutoff_at: AwareDatetime
    inputs: tuple[GoldenInputRecord, ...] = Field(min_length=1)
    derivation: str = Field(min_length=1)
    expected: GoldenExpectation

    @model_validator(mode="after")
    def validate_and_canonicalize(self) -> Self:
        inputs = tuple(sorted(self.inputs, key=lambda i: i.input_key))
        _require_checks(
            (self.issuer.kind is not SubjectKind.ISSUER, "golden decisions rank issuer subjects"),
            (len({i.input_key for i in inputs}) != len(inputs), "golden decision input keys must be unique"),
            (
                any(r.knowable_at > self.cutoff_at for r in inputs),
                "golden inputs must be knowable at or before the decision cutoff",
            ),
        )
        object.__setattr__(self, "inputs", inputs)
        return self


_REQUIRED_GOLDEN_OUTCOMES = frozenset(
    {
        GoldenDecisionOutcome.SELECTED,
        GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND,
        GoldenDecisionOutcome.EXCLUDED,
    }
)


class GoldenDecisionSet(StrictIdentifiedFrozenModel):
    id_field = "golden_decision_set_id"
    id_prefix = "golden-decision-set"
    golden_decision_set_id: str = content_id("golden-decision-set")
    content_sha256: ContentHash = ""
    strategy_definition_id: str = content_ref("strategy-definition")
    strategy_definition_sha256: Sha256
    risk_free_rates: tuple[CutoffRiskFreeRate, ...] = Field(min_length=2)
    decisions: tuple[GoldenDecision, ...] = Field(min_length=6)

    def _canonicalize(self) -> None:
        rates = tuple(sorted(self.risk_free_rates, key=lambda i: i.cutoff_at))
        rate_cutoffs = {i.cutoff_at for i in rates}
        decisions = tuple(sorted(self.decisions, key=lambda i: (i.cutoff_at, i.issuer.id)))
        decision_cutoffs = {d.cutoff_at for d in decisions}
        _require_checks(
            (
                not self.strategy_definition_id.endswith(f":{self.strategy_definition_sha256}"),
                "strategy definition ID and hash do not match",
            ),
            (len(rates) != len(rate_cutoffs), "risk-free rate cutoffs must be unique"),
            (len({d.decision_key for d in decisions}) != len(decisions), "golden decision keys must be unique"),
            (
                len({(d.issuer.id, d.cutoff_at) for d in decisions}) != len(decisions),
                "golden decisions must be unique per issuer and cutoff",
            ),
            (decision_cutoffs != rate_cutoffs, "every golden cutoff requires exactly one declared risk-free rate"),
            (
                len({d.issuer.id for d in decisions}) < 3,
                "golden decisions require at least three materially different issuers",
            ),
            (len(decision_cutoffs) < 2, "golden decisions require at least two historical cutoffs"),
            (
                not _REQUIRED_GOLDEN_OUTCOMES <= {d.expected.outcome for d in decisions},
                "golden decisions must cover selection, valuation rejection, and data exclusion",
            ),
        )
        for cutoff in decision_cutoffs:
            ranked = sorted(
                (d for d in decisions if d.cutoff_at == cutoff and d.expected.rank is not None),
                key=lambda d: d.expected.rank or 0,
            )
            selected = [d for d in ranked if d.expected.outcome is GoldenDecisionOutcome.SELECTED]
            weights = [d.expected.target_weight for d in selected if d.expected.target_weight is not None]
            _require_checks(
                (
                    [d.expected.rank for d in ranked] != list(range(1, len(ranked) + 1)),
                    "golden ranks must be contiguous from one within each cutoff",
                ),
                (selected != ranked[: len(selected)], "selected golden ranks must precede ranked-beyond decisions"),
                (
                    bool(weights and sum(weights) > 1),
                    "selected golden weights cannot exceed a fully invested portfolio",
                ),
                (len(set(weights)) > 1, "equal-weight golden selections must share one target weight"),
            )
        object.__setattr__(self, "risk_free_rates", rates)
        object.__setattr__(self, "decisions", decisions)


__all__ = [
    "CapitalAdjustedLaborEfficiencyDefinition",
    "CapitalChargeBase",
    "ConfidenceEligibilityRule",
    "CutoffRiskFreeRate",
    "DecimalQuantization",
    "DefinitionReviewState",
    "EqualWeightSizingRule",
    "ExclusionReason",
    "FactorDimension",
    "FactorUnitSpec",
    "GoldenDecision",
    "GoldenDecisionOutcome",
    "GoldenDecisionSet",
    "GoldenExpectation",
    "GoldenInputRecord",
    "LIVE_STRATEGY_DEFINITION_RESOURCE",
    "LaborEfficiencyDenominator",
    "LaborEfficiencyNumerator",
    "LaborEfficiencyTreePin",
    "LargeModelValueV0Definition",
    "PriceToGrossProfitDefinition",
    "PriceToGrossProfitTierBand",
    "PriceToSalesDefinition",
    "ProvisionalTierBand",
    "QuarterlyCutoffSchedule",
    "RiskFreeInstrument",
    "RiskFreeRatePolicy",
    "SelectionRule",
    "StrategyDefinitionIdentity",
    "ThreeTierPgpValuationDefinition",
    "ThreeTierValuationDefinition",
    "ValuationBasis",
    "ValuationGapRule",
    "definition_review_state",
    "load_live_strategy_corpus",
    "load_live_strategy_definition",
    "load_strategy_definition",
    "load_strategy_definition_corpus",
    "registered_strategy_definition",
    "strategy_definition_registry",
    "strategy_definitions_in",
]
