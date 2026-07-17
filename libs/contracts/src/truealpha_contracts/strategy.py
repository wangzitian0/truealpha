"""Versioned ``large_model_value_v0`` strategy definition and golden decisions.

This module carries the first product artifact for the large_model_value_v0
strategy: a serializable, frozen, versioned definition of the v0 semantics
locked on 2026-07-17, plus typed golden-decision models that anchor
engineering regressions.  The v0 core metric is capital-adjusted gross profit
per employee::

    (gross_profit - total_assets * risk_free_rate) / headcount

Design invariants:

- Formula variants (for example the future labor-cost denominator) are new
  versioned definitions expressed purely as configuration; the schema needs
  zero structural change to hold them.
- Tier thresholds and eligibility parameters ship as provisional versioned
  research parameters, not validated truths.
- Engine identity (Qlib distribution, adapter, operator registry) binds
  through :class:`StrategyEngineBinding` and never contributes to the
  semantic content hash of the definition.
- Exclusions carry machine-readable reason codes bound to the configured
  metric inputs; a blanket sector exclusion is not expressible.
- Every numeric parameter is a Decimal; binary floating point is rejected at
  the schema boundary.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import _require_aware
from truealpha_contracts.qlib_expression import QlibExpressionExecutionBinding
from truealpha_contracts.research import (
    StrategyExecutionPolicy,
    StrategyTotalReturnPolicy,
    StrategyTransactionCostPolicy,
    TargetPsSelection,
    ValuationTier,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_KEY_PATTERN = r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_MUTABLE_TOKENS = frozenset({"current", "default", "head", "latest", "main", "master", "stable"})
_QUARTER_END_MONTHS = frozenset({3, 6, 9, 12})


def _reject_mutable_coordinate(value: str, field_name: str) -> str:
    tokens = tuple(token for token in re.split(r"[._:/@-]", value.lower()) if token)
    if set(tokens) & _MUTABLE_TOKENS:
        raise ValueError(f"{field_name} cannot contain a mutable reference")
    return value


def _validate_ref_hash(reference_id: str, content_sha256: str, field_name: str) -> None:
    if not reference_id.endswith(f":{content_sha256}"):
        raise ValueError(f"{field_name} ID and hash do not match")


def _identify(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, "content_sha256")
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError("content_sha256 does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, "content_sha256", expected_hash)
    object.__setattr__(model, id_field, expected_id)


def _reject_binary_float(value: object) -> object:
    """Admit Decimal text and integers; reject binary floating point outright."""

    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("decimal parameters must use decimal text or integers, never binary float")
    if isinstance(value, (Decimal, int, str)):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return value
        if not parsed.is_finite():
            raise ValueError("decimal parameters must be finite")
        return parsed
    return value


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class FactorDimension(StrEnum):
    """Physical dimension of a factor output (P/S dimensionless, P/E years)."""

    DIMENSIONLESS = "dimensionless"
    REPORTING_CURRENCY_PER_EMPLOYEE = "reporting_currency_per_employee"
    YEARS = "years"


class FactorUnitSpec(_StrictFrozenModel):
    dimension: FactorDimension
    unit_code: str = Field(pattern=_STABLE_KEY_PATTERN)


class DecimalQuantization(_StrictFrozenModel):
    """Exact output rounding so replays never depend on ambient context."""

    decimal_places: int = Field(ge=0, le=12)
    rounding: Literal["half_even"]

    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.decimal_places)


class ExclusionReason(StrEnum):
    """Machine-readable per-issuer exclusion codes; no blanket sector-eligibility
    code exists. FINANCIAL_VALUATION_NOT_COMPARABLE is not an exception to that
    rule: it is only valid *after* a financial issuer's mandatory #59 factor
    branch (`financial_issuers_use_financial_proxy`) actually produced a real
    capital_adjusted_labor_efficiency value -- it names a specific downstream
    methodology gap (no approved P/S-equivalent tier mapping for financial
    issuers yet), never a shortcut that skips computing the branch."""

    MISSING_GROSS_PROFIT_FACT = "missing_gross_profit_fact"
    MISSING_TOTAL_ASSETS_FACT = "missing_total_assets_fact"
    MISSING_FINANCIAL_ASSET_BASE_FACT = "missing_financial_asset_base_fact"
    MISSING_HEADCOUNT_DISCLOSURE = "missing_headcount_disclosure"
    MISSING_LABOR_COST_DISCLOSURE = "missing_labor_cost_disclosure"
    MISSING_REVENUE_FACT = "missing_revenue_fact"
    MISSING_MARKET_VALUE_INPUT = "missing_market_value_input"
    MISSING_RISK_FREE_RATE_PARAMETER = "missing_risk_free_rate_parameter"
    BELOW_CONFIDENCE_FLOOR = "below_confidence_floor"
    STALE_REQUIRED_INPUT = "stale_required_input"
    NONPOSITIVE_HEADCOUNT = "nonpositive_headcount"
    NONPOSITIVE_LABOR_COST = "nonpositive_labor_cost"
    NONPOSITIVE_REVENUE = "nonpositive_revenue"
    FINANCIAL_VALUATION_NOT_COMPARABLE = "financial_valuation_not_comparable"


class RiskFreeInstrument(StrEnum):
    US_TREASURY_BILL_3_MONTH = "us_treasury_bill_3_month"


class RiskFreeRatePolicy(_StrictFrozenModel):
    """The rate arrives as an explicit versioned per-cutoff parameter, never a fetch."""

    rule_id: Literal["per_cutoff_versioned_parameter"]
    instrument: RiskFreeInstrument
    rate_unit: Literal["annualized_decimal_fraction"]
    missing_rate_reason: ExclusionReason

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.missing_rate_reason is not ExclusionReason.MISSING_RISK_FREE_RATE_PARAMETER:
            raise ValueError("a missing risk-free parameter must use its exact reason code")
        return self


class CutoffRiskFreeRate(_StrictFrozenModel):
    cutoff_at: datetime
    annualized_rate: Decimal = Field(ge=0, le=1)
    provisional: Literal[True]

    @field_validator("annualized_rate", mode="before")
    @classmethod
    def reject_float_rate(cls, value: object) -> object:
        return _reject_binary_float(value)

    @field_validator("cutoff_at")
    @classmethod
    def validate_cutoff_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff_at")


class LaborEfficiencyNumerator(StrEnum):
    GROSS_PROFIT = "gross_profit"


class LaborEfficiencyDenominator(StrEnum):
    HEADCOUNT = "headcount"
    LABOR_COST_SALARIES_PLUS_ESOP = "labor_cost_salaries_plus_esop"


class CapitalChargeBase(StrEnum):
    TOTAL_ASSETS = "total_assets"
    AVERAGE_INVESTABLE_FINANCIAL_ASSETS = "average_investable_financial_assets"


class CapitalAdjustedLaborEfficiencyDefinition(_StrictFrozenModel):
    """v0 core metric: (numerator - capital_charge_base * risk_free_rate) / denominator.

    Variants (labor-cost denominator, financial asset base) are new versioned
    definitions of this same schema; the reason codes and unit dimension are
    forced to match the configured inputs so a variant cannot smuggle stale
    semantics.
    """

    factor_definition_id: str = Field(default="", pattern=r"^(?:|factor-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_key: Literal["capital_adjusted_labor_efficiency"]
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    numerator: LaborEfficiencyNumerator
    capital_charge_base: CapitalChargeBase
    denominator: LaborEfficiencyDenominator
    risk_free: RiskFreeRatePolicy
    formula: Literal["(numerator-capital_charge_base*risk_free_rate)/denominator"]
    unit: FactorUnitSpec
    quantization: DecimalQuantization
    missing_numerator_reason: ExclusionReason
    missing_capital_base_reason: ExclusionReason
    missing_denominator_reason: ExclusionReason
    nonpositive_denominator_reason: ExclusionReason

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.numerator is LaborEfficiencyNumerator.GROSS_PROFIT:
            if self.missing_numerator_reason is not ExclusionReason.MISSING_GROSS_PROFIT_FACT:
                raise ValueError("gross-profit numerator requires the missing_gross_profit_fact reason")
        expected_capital_reason = {
            CapitalChargeBase.TOTAL_ASSETS: ExclusionReason.MISSING_TOTAL_ASSETS_FACT,
            CapitalChargeBase.AVERAGE_INVESTABLE_FINANCIAL_ASSETS: (ExclusionReason.MISSING_FINANCIAL_ASSET_BASE_FACT),
        }[self.capital_charge_base]
        if self.missing_capital_base_reason is not expected_capital_reason:
            raise ValueError("capital-charge base and its missing-input reason code are inconsistent")
        if self.denominator is LaborEfficiencyDenominator.HEADCOUNT:
            expected_dimension = FactorDimension.REPORTING_CURRENCY_PER_EMPLOYEE
            expected_missing = ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE
            expected_nonpositive = ExclusionReason.NONPOSITIVE_HEADCOUNT
        else:
            expected_dimension = FactorDimension.DIMENSIONLESS
            expected_missing = ExclusionReason.MISSING_LABOR_COST_DISCLOSURE
            expected_nonpositive = ExclusionReason.NONPOSITIVE_LABOR_COST
        if self.unit.dimension is not expected_dimension:
            raise ValueError("labor-efficiency unit dimension does not match the configured denominator")
        if self.missing_denominator_reason is not expected_missing:
            raise ValueError("denominator and its missing-input reason code are inconsistent")
        if self.nonpositive_denominator_reason is not expected_nonpositive:
            raise ValueError("denominator and its nonpositive reason code are inconsistent")
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self


class PriceToSalesDefinition(_StrictFrozenModel):
    factor_definition_id: str = Field(default="", pattern=r"^(?:|factor-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_key: Literal["price_to_sales"]
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    market_value_rule: Literal["shares_outstanding_times_last_unadjusted_close"]
    price_rule: Literal["last_unadjusted_close_at_or_before_cutoff"]
    shares_rule: Literal["latest_knowable_shares_outstanding_at_or_before_cutoff"]
    revenue_rule: Literal["latest_complete_fiscal_year_total_revenue"]
    unit: FactorUnitSpec
    quantization: DecimalQuantization
    missing_market_value_reason: ExclusionReason
    missing_revenue_reason: ExclusionReason
    nonpositive_revenue_reason: ExclusionReason

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.unit.dimension is not FactorDimension.DIMENSIONLESS:
            raise ValueError("price-to-sales is a dimensionless ratio")
        if self.missing_market_value_reason is not ExclusionReason.MISSING_MARKET_VALUE_INPUT:
            raise ValueError("missing market-value inputs must use their exact reason code")
        if self.missing_revenue_reason is not ExclusionReason.MISSING_REVENUE_FACT:
            raise ValueError("missing revenue must use its exact reason code")
        if self.nonpositive_revenue_reason is not ExclusionReason.NONPOSITIVE_REVENUE:
            raise ValueError("nonpositive revenue must use its exact reason code")
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self


class ProvisionalTierBand(_StrictFrozenModel):
    tier: ValuationTier
    labor_efficiency_lower_bound: Decimal | None
    labor_efficiency_upper_bound: Decimal | None
    target_ps_lower_bound: Decimal = Field(gt=0)
    target_ps_upper_bound: Decimal = Field(gt=0)

    @field_validator(
        "labor_efficiency_lower_bound",
        "labor_efficiency_upper_bound",
        "target_ps_lower_bound",
        "target_ps_upper_bound",
        mode="before",
    )
    @classmethod
    def reject_float_bounds(cls, value: object) -> object:
        return None if value is None else _reject_binary_float(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.labor_efficiency_lower_bound is not None and self.labor_efficiency_upper_bound is not None:
            if self.labor_efficiency_lower_bound >= self.labor_efficiency_upper_bound:
                raise ValueError("tier labor-efficiency lower bound must be below its upper bound")
        if self.target_ps_lower_bound >= self.target_ps_upper_bound:
            raise ValueError("tier target P/S lower bound must be below its upper bound")
        return self


_TIER_ORDER = {
    ValuationTier.TRADITIONAL: 0,
    ValuationTier.TECH: 1,
    ValuationTier.LARGE_MODEL_NATIVE: 2,
}


class ThreeTierValuationDefinition(_StrictFrozenModel):
    """Three-tier P/S bands over the labor-efficiency domain; thresholds provisional."""

    factor_definition_id: str = Field(default="", pattern=r"^(?:|factor-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_key: Literal["three_tier_valuation"]
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    bands: tuple[ProvisionalTierBand, ...] = Field(min_length=3, max_length=3)
    boundary_rule: Literal["lower_inclusive_upper_exclusive"]
    target_ps_selection: TargetPsSelection
    provisional_thresholds: Literal[True]
    unit: FactorUnitSpec
    quantization: DecimalQuantization

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.unit.dimension is not FactorDimension.DIMENSIONLESS:
            raise ValueError("target P/S is a dimensionless ratio")
        bands = tuple(sorted(self.bands, key=lambda item: _TIER_ORDER[item.tier]))
        if {band.tier for band in bands} != set(ValuationTier):
            raise ValueError("tier bands require exactly traditional, tech, and large-model-native")
        if bands[0].labor_efficiency_lower_bound is not None or bands[-1].labor_efficiency_upper_bound is not None:
            raise ValueError("tier bands must cover the complete labor-efficiency domain")
        for left, right in pairwise(bands):
            if left.labor_efficiency_upper_bound != right.labor_efficiency_lower_bound:
                raise ValueError("tier bands must be contiguous and unambiguous")
        object.__setattr__(self, "bands", bands)
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self

    def band_for(self, labor_efficiency: Decimal) -> ProvisionalTierBand:
        for band in self.bands:
            lower_ok = (
                band.labor_efficiency_lower_bound is None or labor_efficiency >= band.labor_efficiency_lower_bound
            )
            upper_ok = band.labor_efficiency_upper_bound is None or labor_efficiency < band.labor_efficiency_upper_bound
            if lower_ok and upper_ok:
                return band
        raise ValueError("tier bands do not cover the supplied labor efficiency")  # pragma: no cover


class ValuationGapRule(_StrictFrozenModel):
    rule_id: Literal["target_ps_over_current_ps_minus_one"]
    formula: Literal["target_ps/current_ps-1"]
    quantization: DecimalQuantization
    above_band_outcome: Literal["rejected_valuation_above_tier_band"]
    ranking_direction: Literal["descending_valuation_gap"]
    tie_break: Literal["ascending_issuer_id"]


class ConfidenceEligibilityRule(_StrictFrozenModel):
    """Confidence is a first-class input: the floor is a versioned parameter.

    Evaluation order is fixed: a missing required input excludes the issuer
    with the factor definition's missing-input reason before the floor is
    evaluated; the floor then applies to the minimum confidence across all
    consumed inputs.
    """

    rule_id: Literal["minimum_consumed_confidence_floor"]
    minimum_confidence: Decimal = Field(ge=0, le=1)
    confidence_semantics: Literal["minimum_consumed_input_confidence"]
    maximum_input_age_days: int = Field(ge=1, le=730)
    below_floor_reason: ExclusionReason
    stale_input_reason: ExclusionReason
    # Only one behavior is implemented today: financial issuers compute the
    # mandatory #59 factor branch and are then marked ineligible for the P/S
    # tier comparison specifically (no approved financial target band exists
    # yet). A future versioned band would be a new Literal member, not a
    # default -- #21's "no implicit defaults" rule.
    financial_target_behavior: Literal["ineligible_financial_valuation_not_comparable"]

    @field_validator("minimum_confidence", mode="before")
    @classmethod
    def reject_float_confidence(cls, value: object) -> object:
        return _reject_binary_float(value)

    @model_validator(mode="after")
    def validate_reasons(self) -> Self:
        if self.below_floor_reason is not ExclusionReason.BELOW_CONFIDENCE_FLOOR:
            raise ValueError("the confidence floor must use its exact reason code")
        if self.stale_input_reason is not ExclusionReason.STALE_REQUIRED_INPUT:
            raise ValueError("stale inputs must use their exact reason code")
        return self

    @property
    def financial_ineligibility_reason(self) -> ExclusionReason:
        return ExclusionReason.FINANCIAL_VALUATION_NOT_COMPARABLE


class SelectionRule(_StrictFrozenModel):
    rule_id: Literal["top_n_ranked_eligible"]
    selection_count: int = Field(ge=1, le=1000)


class EqualWeightSizingRule(_StrictFrozenModel):
    rule_id: Literal["equal_weight_selected"]
    weight_formula: Literal["1/selected_count"]
    quantization: DecimalQuantization
    empty_selection_behavior: Literal["retain_all_cash"]


class QuarterlyCutoffSchedule(_StrictFrozenModel):
    """Calendar quarter-end UTC cutoffs; the first cutoff anchors the series."""

    rule_id: Literal["calendar_quarter_end_utc"]
    first_cutoff_at: datetime
    cutoff_inclusive: Literal[True]

    @field_validator("first_cutoff_at")
    @classmethod
    def validate_first_cutoff_at(cls, value: datetime) -> datetime:
        value = _require_aware(value, "first_cutoff_at")
        canonical = value.astimezone(UTC)
        if canonical.month not in _QUARTER_END_MONTHS:
            raise ValueError("quarterly cutoffs must land on calendar quarter ends")
        if (canonical + timedelta(days=1)).month == canonical.month:
            raise ValueError("quarterly cutoffs must use the last calendar day of the quarter")
        if (canonical.hour, canonical.minute, canonical.second, canonical.microsecond) != (23, 59, 59, 0):
            raise ValueError("quarterly cutoffs must close the day at 23:59:59 UTC")
        return canonical


class LargeModelValueV0Definition(_StrictFrozenModel):
    """Complete versioned semantics of the v0 strategy; engine identity lives elsewhere.

    The content hash covers every semantic parameter and no engine coordinate,
    so re-running the same definition on a different Qlib build provably reuses
    the same semantics.
    """

    strategy_definition_id: str = Field(default="", pattern=r"^(?:|strategy-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_id: Literal["large_model_value_v0"]
    definition_version: str = Field(pattern=_VERSION_PATTERN)
    provisional_semantics: Literal[True]
    universe: UniverseRef
    schedule: QuarterlyCutoffSchedule
    labor_efficiency: CapitalAdjustedLaborEfficiencyDefinition
    price_to_sales: PriceToSalesDefinition
    tier_valuation: ThreeTierValuationDefinition
    valuation_gap: ValuationGapRule
    eligibility: ConfidenceEligibilityRule
    selection: SelectionRule
    sizing: EqualWeightSizingRule
    execution: StrategyExecutionPolicy
    transaction_cost: StrategyTransactionCostPolicy
    total_return: StrategyTotalReturnPolicy
    # Mandatory-true, not a toggle: the #59 financial factor branch can never
    # be blanket-disabled for the whole strategy (a schema test asserts that
    # setting this to False is rejected). Whether any *specific* issuer takes
    # the branch is a per-decision fact (`GoldenDecision.issuer_branch`),
    # never a strategy-level switch.
    financial_issuers_use_financial_proxy: Literal[True]

    @field_validator("definition_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "definition_version")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        versions = {
            self.labor_efficiency.factor_key: self.labor_efficiency.factor_version,
            self.price_to_sales.factor_key: self.price_to_sales.factor_version,
            self.tier_valuation.factor_key: self.tier_valuation.factor_version,
        }
        if len(versions) != 3:
            raise ValueError("the strategy requires its three distinct factor definitions")  # pragma: no cover
        _identify(self, id_field="strategy_definition_id", prefix="strategy-definition")
        return self


class StrategyEngineBinding(_StrictFrozenModel):
    """Binds one exact strategy definition to one exact Qlib engine identity."""

    engine_binding_id: str = Field(default="", pattern=r"^(?:|strategy-engine-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_definition_id: str = Field(pattern=r"^strategy-definition:[0-9a-f]{64}$")
    strategy_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_binding: QlibExpressionExecutionBinding
    operator_registry_id: str = Field(pattern=r"^qlib-operator-registry:[0-9a-f]{64}$")
    operator_registry_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.strategy_definition_id, self.strategy_definition_sha256, "strategy definition")
        _validate_ref_hash(self.operator_registry_id, self.operator_registry_sha256, "operator registry")
        _identify(self, id_field="engine_binding_id", prefix="strategy-engine-binding")
        return self


class GoldenDecisionOutcome(StrEnum):
    SELECTED = "selected"
    RANKED_BEYOND_SELECTION_COUNT = "ranked_beyond_selection_count"
    REJECTED_VALUATION_ABOVE_TIER_BAND = "rejected_valuation_above_tier_band"
    EXCLUDED = "excluded"


class GoldenInputRecord(_StrictFrozenModel):
    """One typed input with source-class confidence and checked-in grounding."""

    input_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    value: Decimal
    unit_code: str = Field(pattern=_STABLE_KEY_PATTERN)
    confidence: Decimal = Field(gt=0, le=1)
    knowable_at: datetime
    grounding: str = Field(min_length=1)

    @field_validator("value", "confidence", mode="before")
    @classmethod
    def reject_float_values(cls, value: object) -> object:
        return _reject_binary_float(value)

    @field_validator("knowable_at")
    @classmethod
    def validate_knowable_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "knowable_at")


class GoldenExpectation(_StrictFrozenModel):
    capital_adjusted_labor_efficiency: Decimal | None
    tier: ValuationTier | None
    current_price_to_sales: Decimal | None
    target_price_to_sales: Decimal | None
    valuation_gap: Decimal | None
    eligible: bool
    outcome: GoldenDecisionOutcome
    exclusion_reason: ExclusionReason | None
    rank: int | None = Field(ge=1)
    target_weight: Decimal | None = Field(ge=0, le=1)

    @field_validator(
        "capital_adjusted_labor_efficiency",
        "current_price_to_sales",
        "target_price_to_sales",
        "valuation_gap",
        "target_weight",
        mode="before",
    )
    @classmethod
    def reject_float_values(cls, value: object) -> object:
        return None if value is None else _reject_binary_float(value)

    @model_validator(mode="after")
    def validate_outcome_consistency(self) -> Self:
        factor_fields = (
            self.capital_adjusted_labor_efficiency,
            self.tier,
            self.current_price_to_sales,
            self.target_price_to_sales,
            self.valuation_gap,
        )
        if self.outcome is GoldenDecisionOutcome.EXCLUDED:
            if self.exclusion_reason is None:
                raise ValueError("an excluded decision requires a machine-readable reason code")
            if self.eligible or self.rank is not None or self.target_weight is not None:
                raise ValueError("an excluded decision cannot be eligible, ranked, or weighted")
            return self
        if self.exclusion_reason is not None:
            raise ValueError("only excluded decisions carry an exclusion reason")
        if not self.eligible:
            raise ValueError("non-excluded decisions passed eligibility")
        if any(value is None for value in factor_fields):
            raise ValueError("eligible decisions require every expected factor value")
        if self.outcome is GoldenDecisionOutcome.SELECTED:
            if self.rank is None or self.target_weight is None:
                raise ValueError("a selected decision requires a rank and a target weight")
        elif self.outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT:
            if self.rank is None or self.target_weight is not None:
                raise ValueError("a ranked-beyond decision carries a rank and no weight")
        else:
            if self.rank is not None or self.target_weight is not None:
                raise ValueError("a valuation-rejected decision carries no rank or weight")
        return self


class GoldenDecision(_StrictFrozenModel):
    decision_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    issuer: SubjectRef
    # Explicit per-decision classification, not inferred from the issuer or
    # from which facts happen to be present -- the mandatory #59 branch
    # (`gross_profit_per_employee`'s capital-charge skip) is selected by this
    # field, never by a missing-fact fallthrough.
    issuer_branch: Literal["non_financial", "financial"]
    cutoff_at: datetime
    inputs: tuple[GoldenInputRecord, ...] = Field(min_length=1)
    derivation: str = Field(min_length=1)
    expected: GoldenExpectation

    @field_validator("decision_key")
    @classmethod
    def reject_mutable_key(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "decision_key")

    @field_validator("cutoff_at")
    @classmethod
    def validate_cutoff_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff_at")

    @model_validator(mode="after")
    def validate_and_canonicalize(self) -> Self:
        if self.issuer.kind is not SubjectKind.ISSUER:
            raise ValueError("golden decisions rank issuer subjects")
        inputs = tuple(sorted(self.inputs, key=lambda item: item.input_key))
        if len({item.input_key for item in inputs}) != len(inputs):
            raise ValueError("golden decision input keys must be unique")
        for record in inputs:
            if record.knowable_at > self.cutoff_at:
                raise ValueError("golden inputs must be knowable at or before the decision cutoff")
        object.__setattr__(self, "inputs", inputs)
        return self


_REQUIRED_GOLDEN_OUTCOMES = frozenset(
    {
        GoldenDecisionOutcome.SELECTED,
        GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND,
        GoldenDecisionOutcome.EXCLUDED,
    }
)


class GoldenDecisionSet(_StrictFrozenModel):
    """Engineering regression anchors for one exact strategy definition.

    Golden decisions are hand-verified expected decisions over checked-in
    sample evidence.  They are not performance claims and carry no holdout or
    custody semantics.
    """

    golden_decision_set_id: str = Field(default="", pattern=r"^(?:|golden-decision-set:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_definition_id: str = Field(pattern=r"^strategy-definition:[0-9a-f]{64}$")
    strategy_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    risk_free_rates: tuple[CutoffRiskFreeRate, ...] = Field(min_length=2)
    decisions: tuple[GoldenDecision, ...] = Field(min_length=6)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.strategy_definition_id, self.strategy_definition_sha256, "strategy definition")
        rates = tuple(sorted(self.risk_free_rates, key=lambda item: item.cutoff_at))
        rate_cutoffs = tuple(item.cutoff_at for item in rates)
        if len(rate_cutoffs) != len(set(rate_cutoffs)):
            raise ValueError("risk-free rate cutoffs must be unique")
        decisions = tuple(sorted(self.decisions, key=lambda item: (item.cutoff_at, item.issuer.id)))
        if len({decision.decision_key for decision in decisions}) != len(decisions):
            raise ValueError("golden decision keys must be unique")
        pairs = {(decision.issuer.id, decision.cutoff_at) for decision in decisions}
        if len(pairs) != len(decisions):
            raise ValueError("golden decisions must be unique per issuer and cutoff")
        decision_cutoffs = {decision.cutoff_at for decision in decisions}
        if decision_cutoffs != set(rate_cutoffs):
            raise ValueError("every golden cutoff requires exactly one declared risk-free rate")
        if len({decision.issuer.id for decision in decisions}) < 3:
            raise ValueError("golden decisions require at least three materially different issuers")
        if len(decision_cutoffs) < 2:
            raise ValueError("golden decisions require at least two historical cutoffs")
        outcomes = {decision.expected.outcome for decision in decisions}
        if not _REQUIRED_GOLDEN_OUTCOMES <= outcomes:
            raise ValueError("golden decisions must cover selection, valuation rejection, and data exclusion")
        for cutoff in decision_cutoffs:
            at_cutoff = [decision for decision in decisions if decision.cutoff_at == cutoff]
            ranked = sorted(
                (decision for decision in at_cutoff if decision.expected.rank is not None),
                key=lambda decision: decision.expected.rank or 0,
            )
            ranks = [decision.expected.rank for decision in ranked]
            if ranks != list(range(1, len(ranks) + 1)):
                raise ValueError("golden ranks must be contiguous from one within each cutoff")
            selected = [decision for decision in ranked if decision.expected.outcome is GoldenDecisionOutcome.SELECTED]
            if selected != ranked[: len(selected)]:
                raise ValueError("selected golden ranks must precede ranked-beyond decisions")
            weights = [decision.expected.target_weight for decision in selected]
            if weights and sum(weight for weight in weights if weight is not None) > 1:
                raise ValueError("selected golden weights cannot exceed a fully invested portfolio")
            if len({weight for weight in weights if weight is not None}) > 1:
                raise ValueError("equal-weight golden selections must share one target weight")
        object.__setattr__(self, "risk_free_rates", rates)
        object.__setattr__(self, "decisions", decisions)
        _identify(self, id_field="golden_decision_set_id", prefix="golden-decision-set")
        return self

    def rate_for(self, cutoff_at: datetime) -> CutoffRiskFreeRate:
        for rate in self.risk_free_rates:
            if rate.cutoff_at == cutoff_at:
                return rate
        raise ValueError("no risk-free rate is declared for the requested cutoff")  # pragma: no cover


class EvaluationSplitRule(StrEnum):
    """#71: how history is partitioned into train / validation / reserved.
    Chronological is the only rule today -- each boundary is a single
    cutoff, and every instant falls on exactly one side of each.
    """

    CHRONOLOGICAL_TRAIN_VALIDATION_RESERVE = "chronological_train_validation_reserve"


class EvaluationPartition(StrEnum):
    """The three disjoint partitions #71 requires named separately:
    `TRAIN` (fits formulas/thresholds/parameters), `VALIDATION` (still
    in-sample -- chooses among candidate parameter sets, never fits them),
    and `RESERVED_OUT_OF_SAMPLE` (touched only after the strategy
    definition is frozen)."""

    TRAIN = "train"
    VALIDATION = "validation"
    RESERVED_OUT_OF_SAMPLE = "reserved_out_of_sample"


class WalkForwardWindow(_StrictFrozenModel):
    """Rolling re-evaluation cadence over the reserved window, so a single
    lucky split cannot pass as general validity (#71's own wording)."""

    window_length_days: int = Field(gt=0)
    step_days: int = Field(gt=0)
    minimum_history_days: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.minimum_history_days < self.window_length_days:
            raise ValueError("minimum history must cover at least one full walk-forward window")
        if self.step_days > self.window_length_days:
            raise ValueError("step cannot exceed window length, or evaluation windows would leave gaps")
        return self


class ReportedEvaluationMetric(StrEnum):
    """What #61's dashboard surfaces per split/window. Deliberately a
    distinct, lighter vocabulary from research.EvaluationMetric (that type's
    known_reference_baseline/confidence_level/logical_minimum shape belongs
    to the sealed-holdout machinery #59's 2026-07-16 decision dropped) --
    reuses #26's existing coverage/return/drawdown/turnover definitions
    rather than inventing new performance semantics."""

    COVERAGE = "coverage"
    ACTIVE_DECISION_RATE = "active_decision_rate"
    RETURN = "return"
    DRAWDOWN = "drawdown"
    TURNOVER = "turnover"


class CoreStrategyEvaluationProtocol(_StrictFrozenModel):
    """#71: the standard out-of-sample evaluation protocol for the Core
    Strategy, replacing the dropped custodian/freeze/conflict-disclosure
    process with standard backtest discipline -- train/validation
    separation, an out-of-sample reserve, walk-forward evaluation.

    Names the train/validation split explicitly, not just an in-sample vs.
    reserved boundary: `validation_start` marks where fitting stops and
    candidate-selection-only validation begins, and
    `out_of_sample_reserve_start` marks where even validation stops and the
    reserve -- touched only after the definition is frozen -- begins. Three
    disjoint partitions, not two, per #71's explicit acceptance criterion
    naming "the train/validation split rule" as distinct from "the
    out-of-sample reserve."

    This defines the protocol only. Running it against real replay history
    is separate follow-up work gated on #26 producing enough historical
    decisions to evaluate (#71's own non-goal) -- an empty or single-run
    replay has nothing to split.
    """

    protocol_id: str = Field(default="", pattern=r"^(?:|core-strategy-evaluation-protocol:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    protocol_version: str = Field(pattern=_VERSION_PATTERN)
    split_rule: EvaluationSplitRule
    validation_start: datetime
    out_of_sample_reserve_start: datetime
    walk_forward: WalkForwardWindow
    reported_metrics: tuple[ReportedEvaluationMetric, ...] = Field(min_length=1)
    not_a_profitability_claim: Literal[True]

    @field_validator("protocol_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "protocol_version")

    @field_validator("validation_start", "out_of_sample_reserve_start")
    @classmethod
    def validate_aware_boundaries(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.validation_start >= self.out_of_sample_reserve_start:
            raise ValueError("validation_start must strictly precede out_of_sample_reserve_start")
        metrics = tuple(sorted(set(self.reported_metrics), key=lambda item: item.value))
        if len(metrics) != len(self.reported_metrics):
            raise ValueError("reported_metrics must not repeat an entry")
        object.__setattr__(self, "reported_metrics", metrics)
        _identify(self, id_field="protocol_id", prefix="core-strategy-evaluation-protocol")
        return self

    def partition_for(self, as_of: datetime) -> EvaluationPartition:
        """Disjoint-by-construction: every `as_of` lands in exactly one of
        three partitions -- never both, never neither. Fails closed (a
        clear ValueError, not a raw TypeError) on a naive datetime, since
        `>=` between naive and aware datetimes would otherwise raise an
        inconsistent, uncontrolled error."""

        as_of = _require_aware(as_of, "as_of")
        if as_of >= self.out_of_sample_reserve_start:
            return EvaluationPartition.RESERVED_OUT_OF_SAMPLE
        if as_of >= self.validation_start:
            return EvaluationPartition.VALIDATION
        return EvaluationPartition.TRAIN

    def is_reserved_out_of_sample(self, as_of: datetime) -> bool:
        """Convenience predicate over `partition_for` for callers that only
        care about the reserved/not-reserved boundary."""

        return self.partition_for(as_of) is EvaluationPartition.RESERVED_OUT_OF_SAMPLE


__all__ = [
    "CapitalAdjustedLaborEfficiencyDefinition",
    "CapitalChargeBase",
    "ConfidenceEligibilityRule",
    "CoreStrategyEvaluationProtocol",
    "CutoffRiskFreeRate",
    "DecimalQuantization",
    "EqualWeightSizingRule",
    "EvaluationPartition",
    "EvaluationSplitRule",
    "ExclusionReason",
    "FactorDimension",
    "FactorUnitSpec",
    "GoldenDecision",
    "GoldenDecisionOutcome",
    "GoldenDecisionSet",
    "GoldenExpectation",
    "GoldenInputRecord",
    "LaborEfficiencyDenominator",
    "LaborEfficiencyNumerator",
    "LargeModelValueV0Definition",
    "PriceToSalesDefinition",
    "ProvisionalTierBand",
    "QuarterlyCutoffSchedule",
    "ReportedEvaluationMetric",
    "RiskFreeInstrument",
    "RiskFreeRatePolicy",
    "SelectionRule",
    "StrategyEngineBinding",
    "ThreeTierValuationDefinition",
    "ValuationGapRule",
    "WalkForwardWindow",
]
