"""Versioned ``large_model_value_v0`` strategy definitions and golden decisions."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib import resources
from importlib.resources.abc import Traversable
from itertools import pairwise
from typing import Any, Literal, NamedTuple, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from truealpha_contracts.common import identify as _identify
from truealpha_contracts.models import _require_aware
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


def _reject_binary_float(value: object) -> object:
    if isinstance(value, (bool, float)):
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
    DIMENSIONLESS = "dimensionless"
    REPORTING_CURRENCY_PER_EMPLOYEE = "reporting_currency_per_employee"
    YEARS = "years"


class FactorUnitSpec(_StrictFrozenModel):
    dimension: FactorDimension
    unit_code: str = Field(pattern=_STABLE_KEY_PATTERN)


class DecimalQuantization(_StrictFrozenModel):
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


class RiskFreeRatePolicy(_StrictFrozenModel):
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


_FORMULA_FIELDS = (
    "numerator",
    "capital_charge_base",
    "denominator",
    "formula",
    "missing_numerator_reason",
    "missing_capital_base_reason",
    "missing_denominator_reason",
    "nonpositive_denominator_reason",
)


class CapitalAdjustedLaborEfficiencyDefinition(_StrictFrozenModel):
    factor_definition_id: str = Field(default="", pattern=r"^(?:|factor-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_key: Literal["capital_adjusted_labor_efficiency"]
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    numerator: LaborEfficiencyNumerator | None = None
    capital_charge_base: CapitalChargeBase | None = None
    denominator: LaborEfficiencyDenominator | None = None
    risk_free: RiskFreeRatePolicy
    formula: Literal["(numerator-capital_charge_base*risk_free_rate)/denominator"] | None = None
    unit: FactorUnitSpec
    quantization: DecimalQuantization
    missing_numerator_reason: ExclusionReason | None = None
    missing_capital_base_reason: ExclusionReason | None = None
    missing_denominator_reason: ExclusionReason | None = None
    nonpositive_denominator_reason: ExclusionReason | None = None

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    @model_serializer(mode="wrap")
    def omit_absent_formula(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in _FORMULA_FIELDS:
            if getattr(self, name) is None:
                data.pop(name, None)
        return data

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        stated = {name: getattr(self, name) is not None for name in _FORMULA_FIELDS}
        if len(set(stated.values())) != 1:
            raise ValueError(
                f"the formula fields {sorted(_FORMULA_FIELDS)} must be all present or all absent, got {stated}"
            )
        if self.formula is not None:
            self._validate_formula_semantics()
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self

    def _validate_formula_semantics(self) -> None:
        assert self.capital_charge_base is not None
        if (
            self.numerator is LaborEfficiencyNumerator.GROSS_PROFIT
            and self.missing_numerator_reason is not ExclusionReason.MISSING_GROSS_PROFIT_FACT
        ):
            raise ValueError("gross-profit numerator requires the missing_gross_profit_fact reason")
        if self.missing_capital_base_reason is not ExclusionReason.MISSING_TOTAL_ASSETS_FACT:
            raise ValueError("capital-charge base and its missing-input reason code are inconsistent")
        expected = (
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
        if self.unit.dimension is not expected[0]:
            raise ValueError("labor-efficiency unit dimension does not match the configured denominator")
        if self.missing_denominator_reason is not expected[1]:
            raise ValueError("denominator and its missing-input reason code are inconsistent")
        if self.nonpositive_denominator_reason is not expected[2]:
            raise ValueError("denominator and its nonpositive reason code are inconsistent")


class LaborEfficiencyTreePin(_StrictFrozenModel):
    tree_key: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    tree_version: str = Field(min_length=1)
    tree_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("tree_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "tree_version")


class _MarketMultipleDefinitionBase(_StrictFrozenModel):
    factor_definition_id: str = Field(default="", pattern=r"^(?:|factor-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    market_value_rule: Literal["shares_outstanding_times_last_unadjusted_close"]
    price_rule: Literal["last_unadjusted_close_at_or_before_cutoff"]
    shares_rule: Literal["latest_knowable_shares_outstanding_at_or_before_cutoff"]
    unit: FactorUnitSpec
    quantization: DecimalQuantization
    missing_market_value_reason: ExclusionReason

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    def _validate_multiple_base(self, label: str) -> None:
        if self.unit.dimension is not FactorDimension.DIMENSIONLESS:
            raise ValueError(f"{label} is a dimensionless ratio")
        if self.missing_market_value_reason is not ExclusionReason.MISSING_MARKET_VALUE_INPUT:
            raise ValueError("missing market-value inputs must use their exact reason code")


class PriceToSalesDefinition(_MarketMultipleDefinitionBase):
    factor_key: Literal["price_to_sales"]
    revenue_rule: Literal["latest_complete_fiscal_year_total_revenue"]
    missing_revenue_reason: ExclusionReason
    nonpositive_revenue_reason: ExclusionReason

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        self._validate_multiple_base("price-to-sales")
        if self.missing_revenue_reason is not ExclusionReason.MISSING_REVENUE_FACT:
            raise ValueError("missing revenue must use its exact reason code")
        if self.nonpositive_revenue_reason is not ExclusionReason.NONPOSITIVE_REVENUE:
            raise ValueError("nonpositive revenue must use its exact reason code")
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self


class PriceToGrossProfitDefinition(_MarketMultipleDefinitionBase):
    factor_key: Literal["price_to_gross_profit"]
    gross_profit_rule: Literal["latest_complete_fiscal_year_gross_profit"]
    missing_gross_profit_reason: ExclusionReason
    nonpositive_gross_profit_reason: ExclusionReason

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        self._validate_multiple_base("price-to-gross-profit")
        if self.missing_gross_profit_reason is not ExclusionReason.MISSING_GROSS_PROFIT_FACT:
            raise ValueError("missing gross profit must use its exact reason code")
        if self.nonpositive_gross_profit_reason is not ExclusionReason.NONPOSITIVE_GROSS_PROFIT:
            raise ValueError("nonpositive gross profit must use its exact reason code")
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self


class ValuationBasis(StrEnum):
    PRICE_TO_SALES = "price_to_sales"
    PRICE_TO_GROSS_PROFIT = "price_to_gross_profit"


_TIER_ORDER = {
    ValuationTier.TRADITIONAL: 0,
    ValuationTier.TECH: 1,
    ValuationTier.LARGE_MODEL_NATIVE: 2,
}


class _TierBand(Protocol):
    @property
    def tier(self) -> ValuationTier: ...
    @property
    def labor_efficiency_lower_bound(self) -> Decimal | None: ...
    @property
    def labor_efficiency_upper_bound(self) -> Decimal | None: ...


def _ordered_tier_bands[BandT: _TierBand](bands: tuple[BandT, ...]) -> tuple[BandT, ...]:
    ordered = tuple(sorted(bands, key=lambda item: _TIER_ORDER[item.tier]))
    if {band.tier for band in ordered} != set(ValuationTier):
        raise ValueError("tier bands require exactly traditional, tech, and large-model-native")
    if ordered[0].labor_efficiency_lower_bound is not None or ordered[-1].labor_efficiency_upper_bound is not None:
        raise ValueError("tier bands must cover the complete labor-efficiency domain")
    for left, right in pairwise(ordered):
        if left.labor_efficiency_upper_bound != right.labor_efficiency_lower_bound:
            raise ValueError("tier bands must be contiguous and unambiguous")
    return ordered


def _band_for[BandT: _TierBand](bands: tuple[BandT, ...], labor_efficiency: Decimal) -> BandT:
    for band in bands:
        lower_ok = band.labor_efficiency_lower_bound is None or labor_efficiency >= band.labor_efficiency_lower_bound
        upper_ok = band.labor_efficiency_upper_bound is None or labor_efficiency < band.labor_efficiency_upper_bound
        if lower_ok and upper_ok:
            return band
    raise ValueError("tier bands do not cover the supplied labor efficiency")  # pragma: no cover


class _TierBandBase(_StrictFrozenModel):
    tier: ValuationTier
    labor_efficiency_lower_bound: Decimal | None
    labor_efficiency_upper_bound: Decimal | None

    @field_validator("labor_efficiency_lower_bound", "labor_efficiency_upper_bound", mode="before")
    @classmethod
    def reject_float_labor_bounds(cls, value: object) -> object:
        return None if value is None else _reject_binary_float(value)

    def _validate_range(self, target_low: Decimal, target_high: Decimal, label: str) -> None:
        if (
            self.labor_efficiency_lower_bound is not None
            and self.labor_efficiency_upper_bound is not None
            and self.labor_efficiency_lower_bound >= self.labor_efficiency_upper_bound
        ):
            raise ValueError("tier labor-efficiency lower bound must be below its upper bound")
        if target_low >= target_high:
            raise ValueError(f"tier target {label} lower bound must be below its upper bound")


class ProvisionalTierBand(_TierBandBase):
    target_ps_lower_bound: Decimal = Field(gt=0)
    target_ps_upper_bound: Decimal = Field(gt=0)

    @field_validator("target_ps_lower_bound", "target_ps_upper_bound", mode="before")
    @classmethod
    def reject_float_target_bounds(cls, value: object) -> object:
        return _reject_binary_float(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        self._validate_range(self.target_ps_lower_bound, self.target_ps_upper_bound, "P/S")
        return self

    @property
    def target_lower_bound(self) -> Decimal:
        return self.target_ps_lower_bound

    @property
    def target_upper_bound(self) -> Decimal:
        return self.target_ps_upper_bound


class ThreeTierValuationDefinition(_StrictFrozenModel):
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
        object.__setattr__(self, "bands", _ordered_tier_bands(self.bands))
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self

    @property
    def valuation_basis(self) -> ValuationBasis:
        return ValuationBasis.PRICE_TO_SALES

    def band_for(self, labor_efficiency: Decimal) -> ProvisionalTierBand:
        return _band_for(self.bands, labor_efficiency)


class PriceToGrossProfitTierBand(_TierBandBase):
    target_pgp_lower_bound: Decimal = Field(gt=0)
    target_pgp_upper_bound: Decimal = Field(gt=0)

    @field_validator("target_pgp_lower_bound", "target_pgp_upper_bound", mode="before")
    @classmethod
    def reject_float_target_bounds(cls, value: object) -> object:
        return _reject_binary_float(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        self._validate_range(self.target_pgp_lower_bound, self.target_pgp_upper_bound, "P/GP")
        return self

    @property
    def target_lower_bound(self) -> Decimal:
        return self.target_pgp_lower_bound

    @property
    def target_upper_bound(self) -> Decimal:
        return self.target_pgp_upper_bound


class ThreeTierPgpValuationDefinition(_StrictFrozenModel):
    factor_definition_id: str = Field(default="", pattern=r"^(?:|factor-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_key: Literal["three_tier_valuation"]
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    valuation_basis: Literal[ValuationBasis.PRICE_TO_GROSS_PROFIT]
    bands: tuple[PriceToGrossProfitTierBand, ...] = Field(min_length=3, max_length=3)
    boundary_rule: Literal["lower_inclusive_upper_exclusive"]
    target_pgp_selection: Literal["band_midpoint"]
    provisional_thresholds: bool
    unit: FactorUnitSpec
    quantization: DecimalQuantization

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    @field_validator("valuation_basis", mode="before")
    @classmethod
    def parse_basis(cls, value: object) -> object:
        return ValuationBasis(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.unit.dimension is not FactorDimension.DIMENSIONLESS:
            raise ValueError("target P/GP is a dimensionless ratio")
        object.__setattr__(self, "bands", _ordered_tier_bands(self.bands))
        _identify(self, id_field="factor_definition_id", prefix="factor-definition")
        return self

    def band_for(self, labor_efficiency: Decimal) -> PriceToGrossProfitTierBand:
        return _band_for(self.bands, labor_efficiency)


_GAP_RULES: dict[ValuationBasis, tuple[str, str]] = {
    ValuationBasis.PRICE_TO_SALES: ("target_ps_over_current_ps_minus_one", "target_ps/current_ps-1"),
    ValuationBasis.PRICE_TO_GROSS_PROFIT: ("target_pgp_over_current_pgp_minus_one", "target_pgp/current_pgp-1"),
}


class ValuationGapRule(_StrictFrozenModel):
    rule_id: Literal["target_ps_over_current_ps_minus_one", "target_pgp_over_current_pgp_minus_one"]
    formula: Literal["target_ps/current_ps-1", "target_pgp/current_pgp-1"]
    quantization: DecimalQuantization
    above_band_outcome: Literal["rejected_valuation_above_tier_band"]
    ranking_direction: Literal["descending_valuation_gap"]
    tie_break: Literal["ascending_issuer_id"]


_CONFIDENCE_FLOOR_FIELDS = ("rule_id", "minimum_confidence", "confidence_semantics", "below_floor_reason")


class ConfidenceEligibilityRule(_StrictFrozenModel):
    rule_id: Literal["minimum_consumed_confidence_floor"] | None = None
    minimum_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    confidence_semantics: Literal["minimum_consumed_input_confidence"] | None = None
    maximum_input_age_days: int = Field(ge=1, le=730)
    below_floor_reason: ExclusionReason | None = None
    stale_input_reason: ExclusionReason

    @field_validator("minimum_confidence", mode="before")
    @classmethod
    def reject_float_confidence(cls, value: object) -> object:
        return None if value is None else _reject_binary_float(value)

    @model_serializer(mode="wrap")
    def omit_absent_confidence_floor(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in _CONFIDENCE_FLOOR_FIELDS:
            if getattr(self, name) is None:
                data.pop(name, None)
        return data

    @model_validator(mode="after")
    def validate_reasons(self) -> Self:
        stated = {name: getattr(self, name) is not None for name in _CONFIDENCE_FLOOR_FIELDS}
        if len(set(stated.values())) != 1:
            raise ValueError(
                f"the confidence-floor fields {sorted(_CONFIDENCE_FLOOR_FIELDS)} must be all present or all absent, got {stated}"
            )
        if (
            self.below_floor_reason is not None
            and self.below_floor_reason is not ExclusionReason.BELOW_CONFIDENCE_FLOOR
        ):
            raise ValueError("the confidence floor must use its exact reason code")
        if self.stale_input_reason is not ExclusionReason.STALE_REQUIRED_INPUT:
            raise ValueError("stale inputs must use their exact reason code")
        return self


class SelectionRule(_StrictFrozenModel):
    rule_id: Literal["top_n_ranked_eligible"]
    selection_count: int = Field(ge=1, le=1000)


class EqualWeightSizingRule(_StrictFrozenModel):
    rule_id: Literal["equal_weight_selected"]
    weight_formula: Literal["1/selected_count"]
    quantization: DecimalQuantization
    empty_selection_behavior: Literal["retain_all_cash"]


class QuarterlyCutoffSchedule(_StrictFrozenModel):
    rule_id: Literal["calendar_quarter_end_utc"]
    first_cutoff_at: datetime
    cutoff_inclusive: Literal[True]

    @field_validator("first_cutoff_at")
    @classmethod
    def validate_first_cutoff_at(cls, value: datetime) -> datetime:
        canonical = _require_aware(value, "first_cutoff_at").astimezone(UTC)
        if canonical.month not in _QUARTER_END_MONTHS:
            raise ValueError("quarterly cutoffs must land on calendar quarter ends")
        if (canonical + timedelta(days=1)).month == canonical.month:
            raise ValueError("quarterly cutoffs must use the last calendar day of the quarter")
        if (canonical.hour, canonical.minute, canonical.second, canonical.microsecond) != (23, 59, 59, 0):
            raise ValueError("quarterly cutoffs must close the day at 23:59:59 UTC")
        return canonical


class LargeModelValueV0Definition(_StrictFrozenModel):
    strategy_definition_id: str = Field(default="", pattern=r"^(?:|strategy-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_id: Literal["large_model_value_v0"]
    definition_version: str = Field(pattern=_VERSION_PATTERN)
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

    @field_validator("definition_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "definition_version")

    @model_serializer(mode="wrap")
    def omit_absent_gross_profit_multiple(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in ("price_to_gross_profit", "universe", "labor_efficiency_tree_keys"):
            if getattr(self, name) is None:
                data.pop(name, None)
        return data

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        basis = self.tier_valuation.valuation_basis
        if (basis is ValuationBasis.PRICE_TO_GROSS_PROFIT) != (self.price_to_gross_profit is not None):
            raise ValueError("a P/GP tier valuation requires exactly the price_to_gross_profit factor definition")
        if (self.valuation_gap.rule_id, self.valuation_gap.formula) != _GAP_RULES[basis]:
            raise ValueError("the valuation-gap rule must use the multiple of the tier bands")
        if self.provisional_semantics != self.tier_valuation.provisional_thresholds:
            raise ValueError("provisional_semantics must equal the tier valuation's provisional_thresholds")
        if (self.labor_efficiency.formula is None) != (self.labor_efficiency_tree_keys is not None):
            raise ValueError(
                "labor efficiency has one owner: the formula of labor_efficiency, or the trees of "
                "labor_efficiency_tree_keys. Exactly one must be present"
            )
        if self.labor_efficiency_tree_keys is not None and not self.labor_efficiency_tree_keys:
            raise ValueError("labor_efficiency_tree_keys must pin at least one issuer class")
        factor_keys = [self.labor_efficiency.factor_key, self.price_to_sales.factor_key, self.tier_valuation.factor_key]
        if self.price_to_gross_profit is not None:
            factor_keys.append(self.price_to_gross_profit.factor_key)
        if len(set(factor_keys)) != len(factor_keys):
            raise ValueError("the strategy requires distinct factor definitions")  # pragma: no cover
        _identify(self, id_field="strategy_definition_id", prefix="strategy-definition")
        return self

    @property
    def valuation_basis(self) -> ValuationBasis:
        return self.tier_valuation.valuation_basis

    @property
    def valuation_quantization(self) -> DecimalQuantization:
        if self.price_to_gross_profit is not None:
            return self.price_to_gross_profit.quantization
        return self.price_to_sales.quantization


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


def _identity_of(definition: LargeModelValueV0Definition) -> StrategyDefinitionIdentity:
    return StrategyDefinitionIdentity(
        version=definition.definition_version,
        content_sha256=definition.content_sha256,
        provisional_semantics=definition.provisional_semantics,
    )


def strategy_definitions_in(directory: Traversable) -> tuple[StrategyDefinitionIdentity, ...]:
    entries: list[StrategyDefinitionIdentity] = []
    for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not item.name.endswith(".json"):
            continue
        corpus = json.loads(item.read_bytes())
        if not isinstance(corpus, dict) or "strategy_definition" not in corpus:
            continue
        definition = _definition_of_corpus(item.name, corpus)
        if any(entry.version == definition.definition_version for entry in entries):
            raise ValueError(f"{item.name}: a second definition file has version {definition.definition_version}")
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
        entry.version == identity.version or entry.content_sha256 == identity.content_sha256
        for entry in strategy_definition_registry()
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


class ExpressionEngineExecutionBinding(_StrictFrozenModel):
    execution_binding_id: str = Field(default="", pattern=r"^(?:|expression-engine-execution-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    distribution: Literal["polars"] = "polars"
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    release_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")
    adapter_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def identify(self) -> Self:
        _identify(self, id_field="execution_binding_id", prefix="expression-engine-execution-binding")
        return self


class StrategyEngineBinding(_StrictFrozenModel):
    engine_binding_id: str = Field(default="", pattern=r"^(?:|strategy-engine-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_definition_id: str = Field(pattern=r"^strategy-definition:[0-9a-f]{64}$")
    strategy_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_binding: ExpressionEngineExecutionBinding

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.strategy_definition_id, self.strategy_definition_sha256, "strategy definition")
        _identify(self, id_field="engine_binding_id", prefix="strategy-engine-binding")
        return self


class GoldenDecisionOutcome(StrEnum):
    SELECTED = "selected"
    RANKED_BEYOND_SELECTION_COUNT = "ranked_beyond_selection_count"
    REJECTED_VALUATION_ABOVE_TIER_BAND = "rejected_valuation_above_tier_band"
    EXCLUDED = "excluded"


class GoldenInputRecord(_StrictFrozenModel):
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
    current_price_to_gross_profit: Decimal | None = None
    target_price_to_gross_profit: Decimal | None = None

    @field_validator(
        "capital_adjusted_labor_efficiency",
        "current_price_to_sales",
        "target_price_to_sales",
        "valuation_gap",
        "target_weight",
        "current_price_to_gross_profit",
        "target_price_to_gross_profit",
        mode="before",
    )
    @classmethod
    def reject_float_values(cls, value: object) -> object:
        return None if value is None else _reject_binary_float(value)

    @model_serializer(mode="wrap")
    def omit_absent_gross_profit_multiple(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in ("current_price_to_gross_profit", "target_price_to_gross_profit"):
            if getattr(self, name) is None:
                data.pop(name, None)
        return data

    def _validate_excluded(self, pgp_fields: tuple[Decimal | None, Decimal | None]) -> None:
        if self.exclusion_reason is None:
            raise ValueError("an excluded decision requires a machine-readable reason code")
        if self.eligible or self.rank is not None or self.target_weight is not None:
            raise ValueError("an excluded decision cannot be eligible, ranked, or weighted")
        if any(value is not None for value in pgp_fields):
            raise ValueError("an excluded decision carries no P/GP value")

    def _validate_outcome_rank(self) -> None:
        if self.outcome is GoldenDecisionOutcome.SELECTED:
            if self.rank is None or self.target_weight is None:
                raise ValueError("a selected decision requires a rank and a target weight")
        elif self.outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT:
            if self.rank is None or self.target_weight is not None:
                raise ValueError("a ranked-beyond decision carries a rank and no weight")
        elif self.rank is not None or self.target_weight is not None:
            raise ValueError("a valuation-rejected decision carries no rank or weight")

    @model_validator(mode="after")
    def validate_outcome_consistency(self) -> Self:
        pgp_fields = (self.current_price_to_gross_profit, self.target_price_to_gross_profit)
        if self.outcome is GoldenDecisionOutcome.EXCLUDED:
            self._validate_excluded(pgp_fields)
            return self
        if self.exclusion_reason is not None:
            raise ValueError("only excluded decisions carry an exclusion reason")
        if not self.eligible:
            raise ValueError("non-excluded decisions passed eligibility")
        common = (self.capital_adjusted_labor_efficiency, self.tier, self.current_price_to_sales, self.valuation_gap)
        if any(value is None for value in common):
            raise ValueError("eligible decisions require every expected factor value")
        valued_by_sales = self.target_price_to_sales is not None and all(value is None for value in pgp_fields)
        valued_by_gross_profit = self.target_price_to_sales is None and all(value is not None for value in pgp_fields)
        if not (valued_by_sales or valued_by_gross_profit):
            raise ValueError("eligible decisions require the target multiple of exactly one valuation basis")
        self._validate_outcome_rank()
        return self


class GoldenDecision(_StrictFrozenModel):
    decision_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    issuer: SubjectRef
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
        if any(record.knowable_at > self.cutoff_at for record in inputs):
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


def _validate_cutoff_ranks(at_cutoff: list[GoldenDecision]) -> None:
    ranked = sorted(
        (decision for decision in at_cutoff if decision.expected.rank is not None),
        key=lambda decision: decision.expected.rank or 0,
    )
    if [decision.expected.rank for decision in ranked] != list(range(1, len(ranked) + 1)):
        raise ValueError("golden ranks must be contiguous from one within each cutoff")
    selected = [decision for decision in ranked if decision.expected.outcome is GoldenDecisionOutcome.SELECTED]
    if selected != ranked[: len(selected)]:
        raise ValueError("selected golden ranks must precede ranked-beyond decisions")
    weights = [d.expected.target_weight for d in selected if d.expected.target_weight is not None]
    if weights and sum(weights) > 1:
        raise ValueError("selected golden weights cannot exceed a fully invested portfolio")
    if len(set(weights)) > 1:
        raise ValueError("equal-weight golden selections must share one target weight")


class GoldenDecisionSet(_StrictFrozenModel):
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
        if len({(decision.issuer.id, decision.cutoff_at) for decision in decisions}) != len(decisions):
            raise ValueError("golden decisions must be unique per issuer and cutoff")
        decision_cutoffs = {decision.cutoff_at for decision in decisions}
        if decision_cutoffs != set(rate_cutoffs):
            raise ValueError("every golden cutoff requires exactly one declared risk-free rate")
        if len({decision.issuer.id for decision in decisions}) < 3:
            raise ValueError("golden decisions require at least three materially different issuers")
        if len(decision_cutoffs) < 2:
            raise ValueError("golden decisions require at least two historical cutoffs")
        if not _REQUIRED_GOLDEN_OUTCOMES <= {decision.expected.outcome for decision in decisions}:
            raise ValueError("golden decisions must cover selection, valuation rejection, and data exclusion")
        for cutoff in decision_cutoffs:
            _validate_cutoff_ranks([decision for decision in decisions if decision.cutoff_at == cutoff])
        object.__setattr__(self, "risk_free_rates", rates)
        object.__setattr__(self, "decisions", decisions)
        _identify(self, id_field="golden_decision_set_id", prefix="golden-decision-set")
        return self


__all__ = [
    "CapitalAdjustedLaborEfficiencyDefinition",
    "CapitalChargeBase",
    "ConfidenceEligibilityRule",
    "CutoffRiskFreeRate",
    "DecimalQuantization",
    "DefinitionReviewState",
    "EqualWeightSizingRule",
    "ExclusionReason",
    "ExpressionEngineExecutionBinding",
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
    "StrategyEngineBinding",
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
