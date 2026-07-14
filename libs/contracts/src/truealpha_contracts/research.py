"""Frozen research semantics and blind independent-oracle contracts.

The models in this module close research choices before factor implementation or
evaluation.  They deliberately bind the existing Research Catalog and
UniverseRef contracts instead of introducing parallel scope models.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.catalog import (
    CatalogTargetKind,
    ProductOwnerApproval,
    ResearchCatalogManifest,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseClaimKind,
    UniverseDefinitionKind,
    UniverseRef,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_ID_PATTERN = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_STABLE_KEY_PATTERN = r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_MUTABLE_TOKENS = frozenset({"current", "default", "head", "latest", "main", "master", "stable"})


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


def _sorted_unique_strings(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


def _model_key(model: BaseModel) -> str:
    return canonical_sha256(model.model_dump(mode="json"))


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ArtifactVisibility(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PROTECTED = "protected"


class ImmutableArtifactRef(_StrictFrozenModel):
    """A digest-qualified artifact; branch names and mutable tags are invalid."""

    artifact_id: str = Field(pattern=r"^artifact:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    immutable_locator: str = Field(min_length=1)
    visibility: ArtifactVisibility

    @field_validator("immutable_locator")
    @classmethod
    def reject_mutable_locator(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "immutable_locator")

    @model_validator(mode="after")
    def validate_digest_binding(self) -> Self:
        _validate_ref_hash(self.artifact_id, self.content_sha256, "artifact")
        if f"sha256:{self.content_sha256}" not in self.immutable_locator:
            raise ValueError("immutable_locator must contain the exact artifact sha256 digest")
        return self


class ProtectedLabelArtifactRef(_StrictFrozenModel):
    artifact: ImmutableArtifactRef
    visibility: Literal[ArtifactVisibility.PROTECTED] = ArtifactVisibility.PROTECTED
    publicly_accessible: Literal[False] = False
    contains_protected_labels: Literal[True] = True

    @model_validator(mode="after")
    def validate_protected_artifact(self) -> Self:
        if self.artifact.visibility is not ArtifactVisibility.PROTECTED:
            raise ValueError("protected labels require a protected artifact")
        return self


class IndependentApproval(_StrictFrozenModel):
    reviewer_id: str = Field(pattern=_STABLE_KEY_PATTERN)
    reviewer_organization: str = Field(pattern=_STABLE_KEY_PATTERN)
    approval_record_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    approval_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    approved_at: datetime
    independence_attested: Literal[True] = True

    @field_validator("reviewer_id", "reviewer_organization")
    @classmethod
    def reject_mutable_identity(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @model_validator(mode="after")
    def validate_record_binding(self) -> Self:
        _validate_ref_hash(self.approval_record_id, self.approval_record_sha256, "approval record")
        return self


class NumericBand(_StrictFrozenModel):
    band_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.lower_bound is not None and self.upper_bound is not None:
            if self.lower_bound >= self.upper_bound:
                raise ValueError("numeric band lower_bound must be below upper_bound")
        if self.lower_bound is None and not self.lower_inclusive:
            raise ValueError("an unbounded lower edge must use the canonical inclusive marker")
        if self.upper_bound is None and self.upper_inclusive:
            raise ValueError("an unbounded upper edge must use the canonical exclusive marker")
        return self


def _validate_complete_bands(bands: tuple[NumericBand, ...], field_name: str) -> tuple[NumericBand, ...]:
    ordered = tuple(
        sorted(
            bands,
            key=lambda band: (band.lower_bound is not None, band.lower_bound or Decimal(0)),
        )
    )
    if len({band.band_key for band in ordered}) != len(ordered):
        raise ValueError(f"{field_name} band keys must be unique")
    if ordered[0].lower_bound is not None or ordered[-1].upper_bound is not None:
        raise ValueError(f"{field_name} bands must cover the complete numeric domain")
    for left, right in pairwise(ordered):
        if left.upper_bound != right.lower_bound:
            raise ValueError(f"{field_name} bands must be contiguous")
        if left.upper_inclusive == right.lower_inclusive:
            raise ValueError(f"{field_name} boundaries must belong to exactly one band")
    return ordered


class ResearchTarget(StrEnum):
    PEG = "peg"
    GPPE = "gross_profit_per_employee"
    SUPPLY_CHAIN = "supply_chain"
    ANALYST_BACKTEST = "analyst_backtest"
    ETF_VIRTUAL_COMPANY = "etf_virtual_company"
    THEME_PURITY = "theme_purity"
    THREE_TIER_VALUATION = "three_tier_valuation"
    LARGE_MODEL_VALUE_V0 = "large_model_value_v0"


class GppeLeverageChoice(StrEnum):
    LEVEL = "level"
    TIME_SERIES_ELASTICITY = "time_series_elasticity"
    COMBINED_DISCRIMINATED = "combined_discriminated"


class ElasticityEstimator(StrEnum):
    ORDINARY_LEAST_SQUARES = "ordinary_least_squares"
    THEIL_SEN = "theil_sen"


class HeadcountAlignment(StrEnum):
    PERIOD_END = "period_end"
    PERIOD_AVERAGE = "period_average"


class FinancialEfficiencyProxy(StrEnum):
    NET_REVENUE_PER_EMPLOYEE = "net_revenue_per_employee"
    PRE_PROVISION_PROFIT_PER_EMPLOYEE = "pre_provision_profit_per_employee"
    GROSS_PROFIT_EQUIVALENT_PER_EMPLOYEE = "gross_profit_equivalent_per_employee"


class FinancialComparisonPolicy(_StrictFrozenModel):
    financial_policy_id: str = Field(default="", pattern=r"^(?:|financial-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    proxy: FinancialEfficiencyProxy
    numerator_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    denominator: Literal["aligned_employee_count"] = "aligned_employee_count"
    comparison_universe: UniverseRef
    comparison_bands: tuple[NumericBand, ...] = Field(min_length=2)
    separate_from_non_financial_comparisons: Literal[True] = True
    blanket_exclusion_permitted: Literal[False] = False
    independent_approval: IndependentApproval

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        object.__setattr__(
            self,
            "comparison_bands",
            _validate_complete_bands(self.comparison_bands, "financial comparison"),
        )
        _identify(self, id_field="financial_policy_id", prefix="financial-policy")
        return self


class GppeResearchPolicy(_StrictFrozenModel):
    gppe_policy_id: str = Field(default="", pattern=r"^(?:|gppe-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    leverage_choice: GppeLeverageChoice
    level_formula: Literal["annual_gross_profit/aligned_employee_count"]
    output_unit: Literal["reporting_currency_per_employee"]
    headcount_alignment: HeadcountAlignment
    level_window_periods: int = Field(ge=1)
    elasticity_formula: Literal["delta_ln_gross_profit/delta_ln_employee_count"] | None
    elasticity_estimator: ElasticityEstimator | None
    elasticity_window_periods: int | None = Field(default=None, ge=3)
    decision_bands: tuple[NumericBand, ...] = Field(min_length=2)
    zero_headcount_behavior: Literal["unavailable"] = "unavailable"
    missing_headcount_behavior: Literal["unavailable"] = "unavailable"
    nonpositive_log_input_behavior: Literal["unavailable"] = "unavailable"
    reviewed_examples: ImmutableArtifactRef
    financial_policy: FinancialComparisonPolicy

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        needs_elasticity = self.leverage_choice in {
            GppeLeverageChoice.TIME_SERIES_ELASTICITY,
            GppeLeverageChoice.COMBINED_DISCRIMINATED,
        }
        elasticity_fields = (
            self.elasticity_formula,
            self.elasticity_estimator,
            self.elasticity_window_periods,
        )
        if needs_elasticity and any(value is None for value in elasticity_fields):
            raise ValueError("the selected leverage choice requires a complete elasticity specification")
        if not needs_elasticity and any(value is not None for value in elasticity_fields):
            raise ValueError("level leverage cannot carry unresolved or inactive elasticity semantics")
        object.__setattr__(self, "decision_bands", _validate_complete_bands(self.decision_bands, "GPPE decision"))
        _identify(self, id_field="gppe_policy_id", prefix="gppe-policy")
        return self


class GrowthConventionKind(StrEnum):
    ANALYST_CONSENSUS = "analyst_consensus"
    HISTORICAL_CAGR = "historical_cagr"
    COMPANY_GUIDANCE = "company_guidance"


class PeBasis(StrEnum):
    TRAILING_DILUTED = "trailing_diluted"
    FORWARD_DILUTED = "forward_diluted"


class GrowthUnit(StrEnum):
    PERCENTAGE_POINTS = "percentage_points"
    DECIMAL_FRACTION = "decimal_fraction"


class PegPeriodAlignment(StrEnum):
    SAME_FISCAL_HORIZON = "same_fiscal_horizon"
    NEXT_TWELVE_MONTHS = "next_twelve_months"


class NegativeGrowthBehavior(StrEnum):
    UNAVAILABLE = "unavailable"
    SIGNED_PEG = "signed_peg"


class ZeroGrowthBehavior(StrEnum):
    UNAVAILABLE = "unavailable"
    POSITIVE_INFINITY = "positive_infinity"


class ConsensusStatistic(StrEnum):
    MEDIAN = "median"
    MEAN = "mean"


class GuidanceRangePoint(StrEnum):
    MIDPOINT = "midpoint"
    LOWER_BOUND = "lower_bound"
    UPPER_BOUND = "upper_bound"


class AnalystConsensusGrowthConvention(_StrictFrozenModel):
    convention: Literal[GrowthConventionKind.ANALYST_CONSENSUS] = GrowthConventionKind.ANALYST_CONSENSUS
    growth_metric_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    statistic: ConsensusStatistic
    horizon_months: int = Field(ge=1)
    estimate_cutoff: Literal["latest_knowable_at_as_of"] = "latest_knowable_at_as_of"
    fallback_allowed: Literal[False] = False


class HistoricalCagrGrowthConvention(_StrictFrozenModel):
    convention: Literal[GrowthConventionKind.HISTORICAL_CAGR] = GrowthConventionKind.HISTORICAL_CAGR
    growth_metric_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    lookback_years: int = Field(ge=2)
    formula: Literal["(ending_value/starting_value)^(1/years)-1"]
    require_complete_fiscal_periods: Literal[True] = True
    nonpositive_endpoint_behavior: Literal["unavailable"] = "unavailable"
    fallback_allowed: Literal[False] = False


class CompanyGuidanceGrowthConvention(_StrictFrozenModel):
    convention: Literal[GrowthConventionKind.COMPANY_GUIDANCE] = GrowthConventionKind.COMPANY_GUIDANCE
    growth_metric_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    range_point: GuidanceRangePoint
    horizon_months: int = Field(ge=1)
    availability_rule: Literal["issuer_publication_knowable_at"] = "issuer_publication_knowable_at"
    stale_guidance_behavior: Literal["unavailable"] = "unavailable"
    fallback_allowed: Literal[False] = False


PegGrowthConvention = Annotated[
    AnalystConsensusGrowthConvention | HistoricalCagrGrowthConvention | CompanyGuidanceGrowthConvention,
    Field(discriminator="convention"),
]


class PegResearchPolicy(_StrictFrozenModel):
    peg_policy_id: str = Field(default="", pattern=r"^(?:|peg-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    pe_basis: PeBasis
    price_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    eps_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    pe_horizon_months: int = Field(ge=1)
    growth_unit: GrowthUnit
    formula: Literal["pe/growth_percentage_points", "pe/(growth_decimal_fraction*100)"]
    period_alignment: PegPeriodAlignment
    negative_growth_behavior: NegativeGrowthBehavior
    zero_growth_behavior: ZeroGrowthBehavior
    conventions: tuple[PegGrowthConvention, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        expected_formula = (
            "pe/growth_percentage_points"
            if self.growth_unit is GrowthUnit.PERCENTAGE_POINTS
            else "pe/(growth_decimal_fraction*100)"
        )
        if self.formula != expected_formula:
            raise ValueError("PEG formula does not match the frozen growth unit")
        conventions = tuple(sorted(self.conventions, key=lambda item: item.convention.value))
        if {item.convention for item in conventions} != set(GrowthConventionKind):
            raise ValueError("PEG requires exactly the three non-fallback growth conventions")
        forward_horizons = {
            item.horizon_months
            for item in conventions
            if isinstance(item, (AnalystConsensusGrowthConvention, CompanyGuidanceGrowthConvention))
        }
        if forward_horizons != {self.pe_horizon_months}:
            raise ValueError("forward PEG growth and P/E horizons must use the exact same period")
        object.__setattr__(self, "conventions", conventions)
        _identify(self, id_field="peg_policy_id", prefix="peg-policy")
        return self


class CanonicalRating(StrEnum):
    STRONG_SELL = "strong_sell"
    SELL = "sell"
    HOLD = "hold"
    BUY = "buy"
    STRONG_BUY = "strong_buy"


class RatingNormalizationEntry(_StrictFrozenModel):
    source_label: str = Field(min_length=1)
    canonical_rating: CanonicalRating
    ordinal_score: int = Field(ge=-2, le=2)

    @field_validator("source_label")
    @classmethod
    def normalize_source_label(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("source_label cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_score(self) -> Self:
        expected = {
            CanonicalRating.STRONG_SELL: -2,
            CanonicalRating.SELL: -1,
            CanonicalRating.HOLD: 0,
            CanonicalRating.BUY: 1,
            CanonicalRating.STRONG_BUY: 2,
        }[self.canonical_rating]
        if self.ordinal_score != expected:
            raise ValueError("ordinal_score must use the canonical five-point scale")
        return self


class AnalystAction(StrEnum):
    INITIATE = "initiate"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    REITERATE = "reiterate"
    MAINTAIN = "maintain"
    RESUME = "resume"
    SUSPEND = "suspend"
    DISCONTINUE = "discontinue"


class OverlapPolicy(StrEnum):
    LATEST_ACTIVE_SIGNAL = "latest_active_signal"
    INDEPENDENT_EVENT_WINDOWS = "independent_event_windows"


class CensoringPolicy(StrEnum):
    REQUIRE_COMPLETE_HORIZON = "require_complete_horizon"
    RIGHT_CENSOR_WITH_SURVIVAL_WEIGHT = "right_censor_with_survival_weight"


class BenchmarkReturnPolicy(StrEnum):
    SUBJECT_CURRENCY_THEN_EXCESS = "subject_currency_then_excess"
    BASE_CURRENCY_TOTAL_RETURN = "base_currency_total_return"


class CurrencyConversionPolicy(StrEnum):
    SPOT_AT_EACH_RETURN_ENDPOINT = "spot_at_each_return_endpoint"
    NO_CONVERSION_SAME_CURRENCY_ONLY = "no_conversion_same_currency_only"


class UncertaintyMethod(StrEnum):
    BLOCK_BOOTSTRAP = "block_bootstrap"
    ANALYTIC_STANDARD_ERROR = "analytic_standard_error"


class TieBehavior(StrEnum):
    REPORT_TIE = "report_tie"
    LOWER_UNCERTAINTY_WINS = "lower_uncertainty_wins"


class MultipleComparisonMethod(StrEnum):
    BENJAMINI_HOCHBERG = "benjamini_hochberg"
    HOLM_BONFERRONI = "holm_bonferroni"


class AnalystBacktestPolicy(_StrictFrozenModel):
    analyst_policy_id: str = Field(default="", pattern=r"^(?:|analyst-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    rating_normalization: tuple[RatingNormalizationEntry, ...] = Field(min_length=5)
    included_actions: tuple[AnalystAction, ...] = Field(min_length=1)
    excluded_actions: tuple[AnalystAction, ...]
    horizon_days: int = Field(ge=1)
    overlap_policy: OverlapPolicy
    censoring_policy: CensoringPolicy
    benchmark: SubjectRef
    benchmark_return_policy: BenchmarkReturnPolicy
    currency_conversion_policy: CurrencyConversionPolicy
    minimum_history_events: int = Field(ge=2)
    minimum_history_days: int = Field(ge=1)
    uncertainty_method: UncertaintyMethod
    confidence_level: Decimal = Field(gt=Decimal("0.5"), lt=Decimal("1"))
    tie_tolerance: Decimal = Field(ge=0)
    tie_behavior: TieBehavior
    multiple_comparison_method: MultipleComparisonMethod
    multiple_comparison_alpha: Decimal = Field(gt=0, lt=1)
    comparison_family: Literal["all_analysts_in_bound_universe"] = "all_analysts_in_bound_universe"
    public_availability_field: Literal["knowable_at"] = "knowable_at"
    vendor_backfill_time_is_public_availability: Literal[False] = False
    revised_events_are_new_vintages: Literal[True] = True

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        normalization = tuple(sorted(self.rating_normalization, key=lambda item: item.source_label))
        labels = [item.source_label for item in normalization]
        if len(labels) != len(set(labels)):
            raise ValueError("rating normalization source labels must be unique")
        if {item.canonical_rating for item in normalization} != set(CanonicalRating):
            raise ValueError("rating normalization must cover the complete canonical scale")
        included = tuple(sorted(self.included_actions, key=lambda item: item.value))
        excluded = tuple(sorted(self.excluded_actions, key=lambda item: item.value))
        if set(included) & set(excluded) or set(included) | set(excluded) != set(AnalystAction):
            raise ValueError("analyst actions must form an exact included/excluded partition")
        if self.benchmark.kind not in {SubjectKind.SECURITY, SubjectKind.FUND}:
            raise ValueError("analyst benchmark must be an immutable security or fund subject")
        object.__setattr__(self, "rating_normalization", normalization)
        object.__setattr__(self, "included_actions", included)
        object.__setattr__(self, "excluded_actions", excluded)
        _identify(self, id_field="analyst_policy_id", prefix="analyst-policy")
        return self


class EtfAggregationMethod(StrEnum):
    RATIO_OF_SUMS = "ratio_of_sums"
    WEIGHTED_CONSTITUENT_RATIO = "weighted_constituent_ratio"


class CashTreatment(StrEnum):
    INCLUDE_AS_ZERO_NUMERATOR_AND_DENOMINATOR = "include_as_zero_numerator_and_denominator"
    EXCLUDE_AND_REPORT_WEIGHT = "exclude_and_report_weight"


class DerivativeTreatment(StrEnum):
    DELTA_ADJUSTED_LOOK_THROUGH = "delta_adjusted_look_through"
    EXCLUDE_AND_REPORT_WEIGHT = "exclude_and_report_weight"
    UNAVAILABLE_IF_PRESENT = "unavailable_if_present"


class ShortTreatment(StrEnum):
    NET_WITH_MATCHED_LONG = "net_with_matched_long"
    GROSS_SEPARATE = "gross_separate"
    UNAVAILABLE_IF_PRESENT = "unavailable_if_present"


class UnresolvedWeightTreatment(StrEnum):
    FAIL_ABOVE_LIMIT_THEN_RENORMALIZE = "fail_above_limit_then_renormalize"
    ALWAYS_UNAVAILABLE = "always_unavailable"


class EtfCurrencyPolicy(StrEnum):
    CONVERT_TO_FUND_BASE_AT_REPORT_DATE = "convert_to_fund_base_at_report_date"
    REQUIRE_SINGLE_CURRENCY = "require_single_currency"


class InstrumentAggregationLevel(StrEnum):
    SECURITY = "security"
    ISSUER_AFTER_SECURITY_RESOLUTION = "issuer_after_security_resolution"


class EtfPeriodAlignment(StrEnum):
    COMMON_FISCAL_PERIOD = "common_fiscal_period"
    LATEST_KNOWABLE_NOT_AFTER_HOLDINGS_REPORT = "latest_knowable_not_after_holdings_report"


class EtfVirtualCompanyPolicy(_StrictFrozenModel):
    etf_policy_id: str = Field(default="", pattern=r"^(?:|etf-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    aggregation_method: EtfAggregationMethod
    ratio_numerator_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    ratio_denominator_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    holdings_weight_basis: Literal["reported_percent_value"] = "reported_percent_value"
    cash_treatment: CashTreatment
    derivative_treatment: DerivativeTreatment
    short_treatment: ShortTreatment
    unresolved_weight_treatment: UnresolvedWeightTreatment
    unresolved_weight_limit: Decimal = Field(ge=0, lt=1)
    currency_policy: EtfCurrencyPolicy
    fx_semantic_type_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    instrument_aggregation_level: InstrumentAggregationLevel
    period_alignment: EtfPeriodAlignment
    fund_series_identity_required: Literal[True] = True
    report_and_filing_lag_preserved: Literal[True] = True

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if (
            self.unresolved_weight_treatment is UnresolvedWeightTreatment.FAIL_ABOVE_LIMIT_THEN_RENORMALIZE
            and self.unresolved_weight_limit <= 0
        ):
            raise ValueError("renormalization requires a positive, non-trivial unresolved-weight limit")
        if (
            self.unresolved_weight_treatment is UnresolvedWeightTreatment.ALWAYS_UNAVAILABLE
            and self.unresolved_weight_limit != 0
        ):
            raise ValueError("always-unavailable unresolved weight must use the canonical zero limit")
        _identify(self, id_field="etf_policy_id", prefix="etf-policy")
        return self


class ThemeDenominator(StrEnum):
    TOTAL_REVENUE = "total_revenue"
    CLASSIFIED_REVENUE = "classified_revenue"


class UnclassifiedRevenueTreatment(StrEnum):
    INCLUDE_AS_UNCLASSIFIED = "include_as_unclassified"
    EXCLUDE_AND_REPORT_SHARE = "exclude_and_report_share"


class MissingSegmentTreatment(StrEnum):
    UNAVAILABLE = "unavailable"
    PARTIAL_WITH_COVERAGE = "partial_with_coverage"


class ThemePurityPolicy(_StrictFrozenModel):
    theme_policy_id: str = Field(default="", pattern=r"^(?:|theme-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    ontology_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    ontology_sha256: str = Field(pattern=_SHA256_PATTERN)
    ontology_version: str = Field(pattern=_VERSION_PATTERN)
    ontology_owner_id: str = Field(pattern=_STABLE_KEY_PATTERN)
    denominator: ThemeDenominator
    unclassified_revenue_treatment: UnclassifiedRevenueTreatment
    missing_segment_treatment: MissingSegmentTreatment
    minimum_classified_share: Decimal = Field(gt=0, le=1)
    negative_inference_from_missing_evidence: Literal[False] = False
    no_exposure_requires_affirmative_evidence: Literal[True] = True
    classification_evidence_required: Literal[True] = True

    @field_validator("ontology_version", "ontology_owner_id")
    @classmethod
    def reject_mutable_ontology_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.ontology_id, self.ontology_sha256, "theme ontology")
        expected_unclassified_treatment = (
            UnclassifiedRevenueTreatment.INCLUDE_AS_UNCLASSIFIED
            if self.denominator is ThemeDenominator.TOTAL_REVENUE
            else UnclassifiedRevenueTreatment.EXCLUDE_AND_REPORT_SHARE
        )
        if self.unclassified_revenue_treatment is not expected_unclassified_treatment:
            raise ValueError("theme denominator and unclassified-revenue treatment are inconsistent")
        _identify(self, id_field="theme_policy_id", prefix="theme-policy")
        return self


class GraphDirection(StrEnum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"
    BOTH = "both"


class ScenarioOutputLabel(StrEnum):
    SCENARIO_SENSITIVITY = "scenario_sensitivity"
    CAUSAL_EFFECT = "causal_effect"


class SupplyChainResearchPolicy(_StrictFrozenModel):
    supply_chain_policy_id: str = Field(default="", pattern=r"^(?:|supply-chain-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    disclosure_coverage_denominator: Literal["eligible_disclosure_opportunities"]
    missing_disclosure_meaning: Literal["unknown_not_no_dependency"] = "unknown_not_no_dependency"
    graph_edge_semantic: Literal["disclosed_or_independently_evidenced_relationship"]
    graph_minimum_confidence: Decimal = Field(gt=0, le=1)
    relationship_confidence_proves_causality: Literal[False] = False
    scenario_definition_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    scenario_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    direction: GraphDirection
    shock_unit: str = Field(pattern=_STABLE_KEY_PATTERN)
    materiality_threshold: Decimal = Field(gt=0)
    sensitivity_rule_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    sensitivity_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    horizon_days: int = Field(ge=1)
    confidence_kill_threshold: Decimal = Field(gt=0, le=1)
    default_output_label: Literal[ScenarioOutputLabel.SCENARIO_SENSITIVITY] = ScenarioOutputLabel.SCENARIO_SENSITIVITY
    causal_evidence_schema_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    causal_evidence_schema_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.scenario_definition_id, self.scenario_definition_sha256, "scenario definition")
        _validate_ref_hash(self.sensitivity_rule_id, self.sensitivity_rule_sha256, "sensitivity rule")
        _validate_ref_hash(self.causal_evidence_schema_id, self.causal_evidence_schema_sha256, "causal schema")
        if self.confidence_kill_threshold < self.graph_minimum_confidence:
            raise ValueError("confidence kill threshold cannot be below the admitted graph-edge threshold")
        _identify(self, id_field="supply_chain_policy_id", prefix="supply-chain-policy")
        return self


class SupplyChainOutputClaim(_StrictFrozenModel):
    policy_id: str = Field(pattern=r"^supply-chain-policy:[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_label: ScenarioOutputLabel
    scenario_output_artifact: ImmutableArtifactRef
    causal_evidence_artifact: ImmutableArtifactRef | None = None

    @model_validator(mode="after")
    def enforce_causal_boundary(self) -> Self:
        _validate_ref_hash(self.policy_id, self.policy_sha256, "supply-chain policy")
        if self.output_label is ScenarioOutputLabel.CAUSAL_EFFECT and self.causal_evidence_artifact is None:
            raise ValueError("causal output labels require separate causal evidence")
        if self.output_label is ScenarioOutputLabel.SCENARIO_SENSITIVITY and self.causal_evidence_artifact is not None:
            raise ValueError("scenario sensitivity must not smuggle a causal-evidence label")
        return self


class ValuationTier(StrEnum):
    TRADITIONAL = "traditional"
    TECH = "tech"
    LARGE_MODEL_NATIVE = "large_model_native"


class TierBand(_StrictFrozenModel):
    tier: ValuationTier
    gppe_lower_bound: Decimal | None
    gppe_upper_bound: Decimal | None
    ps_lower_bound: Decimal = Field(gt=0)
    ps_upper_bound: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.gppe_lower_bound is not None and self.gppe_upper_bound is not None:
            if self.gppe_lower_bound >= self.gppe_upper_bound:
                raise ValueError("tier GPPE lower bound must be below its upper bound")
        if self.ps_lower_bound >= self.ps_upper_bound:
            raise ValueError("tier P/S lower bound must be below its upper bound")
        return self


class TierValuationPolicy(_StrictFrozenModel):
    tier_policy_id: str = Field(default="", pattern=r"^(?:|tier-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    gppe_policy_id: str = Field(pattern=r"^gppe-policy:[0-9a-f]{64}$")
    gppe_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    bands: tuple[TierBand, ...] = Field(min_length=3, max_length=3)
    boundary_rule: Literal["lower_inclusive_upper_exclusive"] = "lower_inclusive_upper_exclusive"
    exact_boundary_enters_upper_tier: Literal[True] = True
    missing_gppe_behavior: Literal["unavailable"] = "unavailable"
    below_zero_gppe_behavior: Literal["traditional"] = "traditional"

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.gppe_policy_id, self.gppe_policy_sha256, "GPPE policy")
        order = {
            ValuationTier.TRADITIONAL: 0,
            ValuationTier.TECH: 1,
            ValuationTier.LARGE_MODEL_NATIVE: 2,
        }
        bands = tuple(sorted(self.bands, key=lambda item: order[item.tier]))
        if {band.tier for band in bands} != set(ValuationTier):
            raise ValueError("tier policy requires exactly traditional, tech, and large-model-native bands")
        if bands[0].gppe_lower_bound is not None or bands[-1].gppe_upper_bound is not None:
            raise ValueError("tier GPPE bands must cover the complete numeric domain")
        for left, right in pairwise(bands):
            if left.gppe_upper_bound != right.gppe_lower_bound:
                raise ValueError("tier GPPE bands must be contiguous and unambiguous")
        object.__setattr__(self, "bands", bands)
        _identify(self, id_field="tier_policy_id", prefix="tier-policy")
        return self


class TargetPsSelection(StrEnum):
    BAND_MIDPOINT = "band_midpoint"
    BAND_LOWER_BOUND = "band_lower_bound"
    BAND_UPPER_BOUND = "band_upper_bound"


class StrategyFactorId(StrEnum):
    GROSS_PROFIT_PER_EMPLOYEE = "gross_profit_per_employee"
    PRICE_TO_SALES = "price_to_sales"
    THREE_TIER_VALUATION = "three_tier_valuation"


class StrategyFactorRequirement(_StrictFrozenModel):
    factor_requirement_id: str = Field(default="", pattern=r"^(?:|factor-requirement:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_id: StrategyFactorId
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    implementation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("factor_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "factor_version")

    @model_validator(mode="after")
    def identify(self) -> Self:
        _identify(self, id_field="factor_requirement_id", prefix="factor-requirement")
        return self


class StrategyPerformanceClaimPolicy(StrEnum):
    PROHIBITED_FIXED_COHORT = "prohibited_fixed_cohort"
    RESEARCH_ONLY_SURVIVORSHIP_SAFE = "research_only_survivorship_safe"


class FinancialStrategyBehavior(StrEnum):
    REQUIRE_VERSIONED_FINANCIAL_TARGET_BAND = "require_versioned_financial_target_band"
    INELIGIBLE_FINANCIAL_VALUATION_NOT_COMPARABLE = "ineligible_financial_valuation_not_comparable"


class StrategyCutoffSchedule(_StrictFrozenModel):
    rule_id: Literal["fixed_interval_utc"]
    anchor_at: datetime
    interval_days: int = Field(ge=1, le=366)
    cutoff_inclusive: Literal[True]

    @field_validator("anchor_at")
    @classmethod
    def validate_anchor_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "anchor_at")


class StrategyEligibilityPolicy(_StrictFrozenModel):
    rule_id: Literal["all_required_factors_available"]
    minimum_confidence: Decimal = Field(ge=0, le=1)
    confidence_rule: Literal["minimum_consumed_confidence"]
    missing_input_behavior: Literal["ineligible_missing_required_input"]
    stale_input_behavior: Literal["ineligible_stale_required_input"]
    low_confidence_behavior: Literal["ineligible_low_confidence"]
    financial_target_behavior: FinancialStrategyBehavior

    @property
    def financial_ineligibility_reason(self) -> Literal["financial_valuation_not_comparable"] | None:
        if self.financial_target_behavior is FinancialStrategyBehavior.INELIGIBLE_FINANCIAL_VALUATION_NOT_COMPARABLE:
            return "financial_valuation_not_comparable"
        return None


class StrategySizingPolicy(_StrictFrozenModel):
    rule_id: Literal["equal_weight_selected"]
    empty_selection_behavior: Literal["retain_all_cash"]


class StrategyExecutionPolicy(_StrictFrozenModel):
    rule_id: Literal["next_eligible_market_session_open"]
    lag_sessions: Literal[1]
    price_field: Literal["unadjusted_open"]
    unfilled_order_behavior: Literal["remain_cash"]


class StrategyTransactionCostPolicy(_StrictFrozenModel):
    rule_id: Literal["per_side_notional_basis_points"]
    basis_points: Decimal = Field(ge=0, le=1000)


class StrategyTotalReturnPolicy(_StrictFrozenModel):
    rule_id: Literal["unadjusted_bars_explicit_corporate_actions"]
    price_basis: Literal["unadjusted"]
    corporate_action_processing: Literal["explicit_event_lifecycle_once"]
    adjusted_prices_affect_returns: Literal[False]


class LargeModelValueV0Policy(_StrictFrozenModel):
    strategy_policy_id: str = Field(default="", pattern=r"^(?:|strategy-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_id: Literal["large_model_value_v0"]
    strategy_version: str = Field(pattern=_VERSION_PATTERN)
    universe: UniverseRef
    universe_definition_kind: UniverseDefinitionKind
    universe_claim_kind: UniverseClaimKind
    performance_claim_policy: StrategyPerformanceClaimPolicy
    gppe_policy_id: str = Field(pattern=r"^gppe-policy:[0-9a-f]{64}$")
    gppe_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    tier_policy_id: str = Field(pattern=r"^tier-policy:[0-9a-f]{64}$")
    tier_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    required_factors: tuple[StrategyFactorRequirement, ...] = Field(min_length=3, max_length=3)
    schedule: StrategyCutoffSchedule
    valuation_metric: Literal["price_to_sales"]
    target_ps_selection: TargetPsSelection
    valuation_gap_formula: Literal["target_ps/current_ps-1"]
    ranking_direction: Literal["descending_valuation_gap"]
    eligibility: StrategyEligibilityPolicy
    selection_rule_id: Literal["top_n_ranked_eligible"]
    selection_count: int = Field(ge=1)
    sizing: StrategySizingPolicy
    execution: StrategyExecutionPolicy
    transaction_cost: StrategyTransactionCostPolicy
    total_return: StrategyTotalReturnPolicy
    financial_issuers_use_financial_proxy: Literal[True]

    @field_validator("strategy_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "strategy_version")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.gppe_policy_id, self.gppe_policy_sha256, "GPPE policy")
        _validate_ref_hash(self.tier_policy_id, self.tier_policy_sha256, "tier policy")
        requirements = tuple(sorted(self.required_factors, key=lambda item: item.factor_id.value))
        factor_ids = tuple(requirement.factor_id for requirement in requirements)
        if len(factor_ids) != len(set(factor_ids)) or set(factor_ids) != set(StrategyFactorId):
            raise ValueError("large_model_value_v0 requires exactly one version of every core strategy factor")
        object.__setattr__(self, "required_factors", requirements)

        if self.universe_definition_kind is UniverseDefinitionKind.FIXED_COHORT:
            if (
                self.universe_claim_kind is not UniverseClaimKind.FIXED_COHORT_DESCRIPTION
                or self.performance_claim_policy is not StrategyPerformanceClaimPolicy.PROHIBITED_FIXED_COHORT
            ):
                raise ValueError("a fixed research cohort must prohibit strategy performance claims")
        elif (
            self.universe_claim_kind is not UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY
            or self.performance_claim_policy is not StrategyPerformanceClaimPolicy.RESEARCH_ONLY_SURVIVORSHIP_SAFE
        ):
            raise ValueError("a PIT strategy universe must use the survivorship-safe replay claim policy")
        _identify(self, id_field="strategy_policy_id", prefix="strategy-policy")
        return self


class SemanticCatalogBinding(_StrictFrozenModel):
    target: ResearchTarget
    semantic_policy_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    catalog_entry_id: str = Field(pattern=r"^catalog-entry:[0-9a-f]{64}$")
    catalog_alias: str = Field(pattern=_STABLE_KEY_PATTERN)

    @field_validator("catalog_alias")
    @classmethod
    def reject_mutable_alias(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "catalog_alias")


class ResearchSemanticsManifest(_StrictFrozenModel):
    research_semantics_id: str = Field(default="", pattern=r"^(?:|research-semantics:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    semantics_version: str = Field(pattern=_VERSION_PATTERN)
    catalog: ResearchCatalogManifest
    universe: UniverseRef
    gppe: GppeResearchPolicy
    peg: PegResearchPolicy
    analyst: AnalystBacktestPolicy
    etf: EtfVirtualCompanyPolicy
    theme: ThemePurityPolicy
    supply_chain: SupplyChainResearchPolicy
    tier: TierValuationPolicy
    large_model_value_v0: LargeModelValueV0Policy
    catalog_bindings: tuple[SemanticCatalogBinding, ...] = Field(min_length=8, max_length=8)
    semantic_author_ids: tuple[str, ...] = Field(min_length=1)
    product_owner_approval: ProductOwnerApproval
    independent_approval: IndependentApproval
    frozen_at: datetime

    @field_validator("semantics_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "semantics_version")

    @field_validator("semantic_author_ids")
    @classmethod
    def validate_authors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if re.fullmatch(_STABLE_KEY_PATTERN, value) is None:
                raise ValueError("semantic authors must use stable identities")
            _reject_mutable_coordinate(value, "semantic_author_id")
        return _sorted_unique_strings(values, "semantic_author_ids")

    @field_validator("frozen_at")
    @classmethod
    def validate_frozen_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "frozen_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.universe != self.catalog.scope_floor.universe:
            raise ValueError("research semantics must bind the exact Research Catalog UniverseRef")
        if self.independent_approval.reviewer_id in set(self.semantic_author_ids):
            raise ValueError("independent semantic reviewer cannot be a semantic author")
        if self.gppe.financial_policy.independent_approval.reviewer_id in set(self.semantic_author_ids):
            raise ValueError("financial proxy reviewer cannot be a semantic author")
        if self.independent_approval.reviewer_id == self.product_owner_approval.approved_by:
            raise ValueError("independent semantic reviewer cannot also be the product owner")
        if self.gppe.financial_policy.independent_approval.reviewer_id == self.product_owner_approval.approved_by:
            raise ValueError("financial proxy reviewer cannot also be the product owner")
        if self.product_owner_approval.approved_at > self.frozen_at:
            raise ValueError("product-owner approval must predate semantic freeze")
        if self.independent_approval.approved_at > self.frozen_at:
            raise ValueError("independent approval must predate semantic freeze")
        if self.gppe.financial_policy.independent_approval.approved_at > self.frozen_at:
            raise ValueError("financial-proxy approval must predate semantic freeze")
        if self.tier.gppe_policy_id != self.gppe.gppe_policy_id:
            raise ValueError("tier policy must bind the exact GPPE policy")
        if (
            self.large_model_value_v0.gppe_policy_id != self.gppe.gppe_policy_id
            or self.large_model_value_v0.tier_policy_id != self.tier.tier_policy_id
        ):
            raise ValueError("large_model_value_v0 must bind the exact GPPE and tier policies")
        if self.large_model_value_v0.universe != self.universe:
            raise ValueError("large_model_value_v0 must bind the exact Research Catalog UniverseRef")

        expected_policy_ids = {
            ResearchTarget.GPPE: self.gppe.gppe_policy_id,
            ResearchTarget.PEG: self.peg.peg_policy_id,
            ResearchTarget.ANALYST_BACKTEST: self.analyst.analyst_policy_id,
            ResearchTarget.ETF_VIRTUAL_COMPANY: self.etf.etf_policy_id,
            ResearchTarget.THEME_PURITY: self.theme.theme_policy_id,
            ResearchTarget.SUPPLY_CHAIN: self.supply_chain.supply_chain_policy_id,
            ResearchTarget.THREE_TIER_VALUATION: self.tier.tier_policy_id,
            ResearchTarget.LARGE_MODEL_VALUE_V0: self.large_model_value_v0.strategy_policy_id,
        }
        bindings = tuple(sorted(self.catalog_bindings, key=lambda item: item.target.value))
        if {binding.target for binding in bindings} != set(ResearchTarget):
            raise ValueError("catalog bindings must cover all seven modules and large_model_value_v0")
        entries_by_id = {entry.catalog_entry_id: entry for entry in self.catalog.entries}
        for binding in bindings:
            if binding.semantic_policy_id != expected_policy_ids[binding.target]:
                raise ValueError("catalog binding references the wrong semantic policy")
            entry = entries_by_id.get(binding.catalog_entry_id)
            if entry is None or entry.catalog_alias != binding.catalog_alias:
                raise ValueError("semantic binding does not resolve to the exact Research Catalog entry")
            if entry.universe != self.universe:
                raise ValueError("semantic catalog entry does not bind the exact UniverseRef")
            if (
                binding.target is ResearchTarget.LARGE_MODEL_VALUE_V0
                and entry.target.target_kind is not CatalogTargetKind.STRATEGY
            ):
                raise ValueError("large_model_value_v0 must bind a strategy catalog target")
            if binding.target is ResearchTarget.LARGE_MODEL_VALUE_V0:
                strategy_id = getattr(entry.target, "strategy_id", None)
                if strategy_id != self.large_model_value_v0.strategy_id:
                    raise ValueError("catalog strategy coordinate must be exactly large_model_value_v0")
        object.__setattr__(self, "catalog_bindings", bindings)
        _identify(self, id_field="research_semantics_id", prefix="research-semantics")
        return self


class DevelopmentGoldenCase(_StrictFrozenModel):
    case_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    subject_scope: tuple[SubjectRef, ...] = Field(min_length=1)
    input_artifact: ImmutableArtifactRef
    expected_output_artifact: ImmutableArtifactRef
    provenance_artifact: ImmutableArtifactRef

    @field_validator("case_key")
    @classmethod
    def reject_mutable_key(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "case_key")

    @model_validator(mode="after")
    def sort_subjects(self) -> Self:
        subjects = tuple(sorted(self.subject_scope, key=_model_key))
        if len(subjects) != len(set(subjects)):
            raise ValueError("golden subject_scope must not contain duplicates")
        object.__setattr__(self, "subject_scope", subjects)
        return self


class DevelopmentGoldenSet(_StrictFrozenModel):
    golden_set_id: str = Field(default="", pattern=r"^(?:|golden-set:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    target: ResearchTarget
    cases: tuple[DevelopmentGoldenCase, ...] = Field(min_length=1)
    authored_by: tuple[str, ...] = Field(min_length=1)
    independent_approval: IndependentApproval
    frozen_at: datetime

    @field_validator("authored_by")
    @classmethod
    def validate_authors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "authored_by")

    @field_validator("frozen_at")
    @classmethod
    def validate_frozen_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "frozen_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.independent_approval.reviewer_id in set(self.authored_by):
            raise ValueError("development golden reviewer cannot be a golden author")
        if self.independent_approval.approved_at > self.frozen_at:
            raise ValueError("golden approval must predate golden freeze")
        cases = tuple(sorted(self.cases, key=lambda item: item.case_key))
        if len({case.case_key for case in cases}) != len(cases):
            raise ValueError("golden case keys must be unique")
        object.__setattr__(self, "cases", cases)
        _identify(self, id_field="golden_set_id", prefix="golden-set")
        return self


class MetricDirection(StrEnum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class EvaluationMetric(_StrictFrozenModel):
    metric_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    direction: MetricDirection
    threshold: Decimal
    known_reference_baseline: Decimal
    minimum_improvement: Decimal = Field(gt=0)
    logical_minimum: Decimal
    logical_maximum: Decimal
    minimum_sample_count: int = Field(ge=10)
    confidence_level: Decimal = Field(gt=Decimal("0.5"), lt=Decimal("1"))

    @model_validator(mode="after")
    def validate_non_trivial_threshold(self) -> Self:
        if self.logical_minimum >= self.logical_maximum:
            raise ValueError("metric logical range must be increasing")
        if not self.logical_minimum <= self.known_reference_baseline <= self.logical_maximum:
            raise ValueError("known-reference baseline is outside the logical metric range")
        if not self.logical_minimum <= self.threshold <= self.logical_maximum:
            raise ValueError("metric threshold is outside the logical metric range")
        if self.direction is MetricDirection.MINIMUM:
            if self.threshold < self.known_reference_baseline + self.minimum_improvement:
                raise ValueError("minimum metric threshold must beat the predeclared baseline")
            if self.threshold == self.logical_minimum:
                raise ValueError("minimum metric threshold cannot be a pass-all boundary")
        else:
            if self.threshold > self.known_reference_baseline - self.minimum_improvement:
                raise ValueError("maximum metric threshold must beat the predeclared baseline")
            if self.threshold == self.logical_maximum:
                raise ValueError("maximum metric threshold cannot be a pass-all boundary")
        return self


class HoldoutStratum(_StrictFrozenModel):
    stratum_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    selection_frame: ImmutableArtifactRef
    selection_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    subject_kinds: tuple[SubjectKind, ...] = Field(min_length=1)
    minimum_sample_count: int = Field(ge=10)

    @model_validator(mode="after")
    def normalize_subject_kinds(self) -> Self:
        kinds = tuple(sorted(self.subject_kinds, key=lambda item: item.value))
        if len(kinds) != len(set(kinds)):
            raise ValueError("holdout stratum subject kinds must be unique")
        object.__setattr__(self, "subject_kinds", kinds)
        return self


class TargetEvaluationPlan(_StrictFrozenModel):
    target_plan_id: str = Field(default="", pattern=r"^(?:|target-evaluation-plan:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    target: ResearchTarget
    canonical_question_ids: tuple[str, ...] = Field(min_length=1)
    subject_scope: tuple[SubjectRef, ...] = Field(min_length=1)
    strata: tuple[HoldoutStratum, ...] = Field(min_length=1)
    metrics: tuple[EvaluationMetric, ...] = Field(min_length=1)

    @field_validator("canonical_question_ids")
    @classmethod
    def validate_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"canonical-question:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("evaluation questions must be content-addressed catalog questions")
        return _sorted_unique_strings(values, "canonical_question_ids")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        subjects = tuple(sorted(self.subject_scope, key=_model_key))
        if len(subjects) != len(set(subjects)):
            raise ValueError("evaluation subject scope must be unique")
        strata = tuple(sorted(self.strata, key=lambda item: item.stratum_key))
        metrics = tuple(sorted(self.metrics, key=lambda item: item.metric_key))
        if len({item.stratum_key for item in strata}) != len(strata):
            raise ValueError("evaluation stratum keys must be unique")
        if len({item.metric_key for item in metrics}) != len(metrics):
            raise ValueError("evaluation metric keys must be unique")
        object.__setattr__(self, "subject_scope", subjects)
        object.__setattr__(self, "strata", strata)
        object.__setattr__(self, "metrics", metrics)
        _identify(self, id_field="target_plan_id", prefix="target-evaluation-plan")
        return self


class EvaluationProtocol(_StrictFrozenModel):
    evaluation_protocol_id: str = Field(default="", pattern=r"^(?:|evaluation-protocol:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    protocol_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    protocol_version: str = Field(pattern=_VERSION_PATTERN)
    research_semantics_id: str = Field(pattern=r"^research-semantics:[0-9a-f]{64}$")
    research_semantics_sha256: str = Field(pattern=_SHA256_PATTERN)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    universe: UniverseRef
    target_plans: tuple[TargetEvaluationPlan, ...] = Field(min_length=8, max_length=8)
    product_owner_approval: ProductOwnerApproval
    independent_approval: IndependentApproval
    predeclared_at: datetime

    @field_validator("protocol_key", "protocol_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)

    @field_validator("predeclared_at")
    @classmethod
    def validate_predeclared_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "predeclared_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.research_semantics_id, self.research_semantics_sha256, "research semantics")
        _validate_ref_hash(self.research_catalog_id, self.research_catalog_sha256, "Research Catalog")
        plans = tuple(sorted(self.target_plans, key=lambda item: item.target.value))
        if {plan.target for plan in plans} != set(ResearchTarget):
            raise ValueError("evaluation protocol must cover all seven modules and large_model_value_v0")
        if self.product_owner_approval.approved_at > self.predeclared_at:
            raise ValueError("protocol product-owner approval must predate predeclaration")
        if self.independent_approval.approved_at > self.predeclared_at:
            raise ValueError("protocol independent approval must predate predeclaration")
        if self.independent_approval.reviewer_id == self.product_owner_approval.approved_by:
            raise ValueError("independent protocol reviewer cannot also be the product owner")
        object.__setattr__(self, "target_plans", plans)
        _identify(self, id_field="evaluation_protocol_id", prefix="evaluation-protocol")
        return self


class OracleCustody(_StrictFrozenModel):
    custody_id: str = Field(default="", pattern=r"^(?:|oracle-custody:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    custodian_id: str = Field(pattern=_STABLE_KEY_PATTERN)
    authorized_label_reader_ids: tuple[str, ...] = Field(min_length=1)
    protected_labels: ProtectedLabelArtifactRef
    custody_record: ImmutableArtifactRef
    blinded_from_candidate_authors: Literal[True] = True
    labels_never_public: Literal[True] = True
    access_before_candidate_freeze_prohibited: Literal[True] = True
    sealed_at: datetime

    @field_validator("sealed_at")
    @classmethod
    def validate_sealed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "sealed_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        readers = _sorted_unique_strings(self.authorized_label_reader_ids, "authorized_label_reader_ids")
        if self.custodian_id not in readers:
            raise ValueError("custodian must be an authorized protected-label reader")
        object.__setattr__(self, "authorized_label_reader_ids", readers)
        _identify(self, id_field="custody_id", prefix="oracle-custody")
        return self


class StratumSampleCount(_StrictFrozenModel):
    target: ResearchTarget
    stratum_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    sample_count: int = Field(ge=1)


class SealedHoldout(_StrictFrozenModel):
    sealed_holdout_id: str = Field(default="", pattern=r"^(?:|sealed-holdout:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    evaluation_protocol_id: str = Field(pattern=r"^evaluation-protocol:[0-9a-f]{64}$")
    evaluation_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    generation: int = Field(ge=1)
    predecessor_holdout_id: str | None = Field(default=None, pattern=r"^sealed-holdout:[0-9a-f]{64}$")
    sample_artifact: ImmutableArtifactRef
    selected_question_ids: tuple[str, ...] = Field(min_length=1)
    stratum_sample_counts: tuple[StratumSampleCount, ...] = Field(min_length=1)
    custody: OracleCustody
    sampled_at: datetime

    @field_validator("selected_question_ids")
    @classmethod
    def validate_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"canonical-question:[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("holdout questions must be content-addressed catalog questions")
        return _sorted_unique_strings(values, "selected_question_ids")

    @field_validator("sampled_at")
    @classmethod
    def validate_sampled_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "sampled_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        _validate_ref_hash(self.evaluation_protocol_id, self.evaluation_protocol_sha256, "evaluation protocol")
        if self.sampled_at > self.custody.sealed_at:
            raise ValueError("holdout custody cannot seal labels before the sample exists")
        if (self.generation == 1) != (self.predecessor_holdout_id is None):
            raise ValueError("only first-generation holdouts omit a predecessor")
        counts = tuple(sorted(self.stratum_sample_counts, key=lambda item: (item.target.value, item.stratum_key)))
        coordinates = [(item.target, item.stratum_key) for item in counts]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("holdout stratum sample counts must be unique")
        object.__setattr__(self, "stratum_sample_counts", counts)
        _identify(self, id_field="sealed_holdout_id", prefix="sealed-holdout")
        return self


class CandidateArtifactRole(StrEnum):
    IMPLEMENTATION = "implementation"
    PARAMETERS = "parameters"
    RESEARCH_CATALOG = "research_catalog"
    RESEARCH_SEMANTICS = "research_semantics"
    EVALUATION_PROTOCOL = "evaluation_protocol"


class CandidateArtifactRef(_StrictFrozenModel):
    role: CandidateArtifactRole
    artifact: ImmutableArtifactRef


class CandidateFreeze(_StrictFrozenModel):
    candidate_freeze_id: str = Field(default="", pattern=r"^(?:|candidate-freeze:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    candidate_version: str = Field(pattern=_VERSION_PATTERN)
    research_semantics_id: str = Field(pattern=r"^research-semantics:[0-9a-f]{64}$")
    research_semantics_sha256: str = Field(pattern=_SHA256_PATTERN)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluation_protocol_id: str = Field(pattern=r"^evaluation-protocol:[0-9a-f]{64}$")
    evaluation_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    universe: UniverseRef
    sealed_holdout_id: str = Field(pattern=r"^sealed-holdout:[0-9a-f]{64}$")
    sealed_holdout_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifacts: tuple[CandidateArtifactRef, ...] = Field(min_length=5, max_length=5)
    candidate_author_ids: tuple[str, ...] = Field(min_length=1)
    labels_unseen_attestation: Literal[True] = True
    frozen_at: datetime

    @field_validator("candidate_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "candidate_version")

    @field_validator("candidate_author_ids")
    @classmethod
    def validate_authors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "candidate_author_ids")

    @field_validator("frozen_at")
    @classmethod
    def validate_frozen_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "frozen_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        for reference_id, digest, name in (
            (self.research_semantics_id, self.research_semantics_sha256, "research semantics"),
            (self.research_catalog_id, self.research_catalog_sha256, "Research Catalog"),
            (self.evaluation_protocol_id, self.evaluation_protocol_sha256, "evaluation protocol"),
            (self.sealed_holdout_id, self.sealed_holdout_sha256, "sealed holdout"),
        ):
            _validate_ref_hash(reference_id, digest, name)
        artifacts = tuple(sorted(self.artifacts, key=lambda item: item.role.value))
        if {item.role for item in artifacts} != set(CandidateArtifactRole):
            raise ValueError("candidate freeze must hash implementation, parameters, catalog, semantics, and protocol")
        artifact_by_role = {item.role: item.artifact for item in artifacts}
        expected_digests = {
            CandidateArtifactRole.RESEARCH_CATALOG: self.research_catalog_sha256,
            CandidateArtifactRole.RESEARCH_SEMANTICS: self.research_semantics_sha256,
            CandidateArtifactRole.EVALUATION_PROTOCOL: self.evaluation_protocol_sha256,
        }
        if any(artifact_by_role[role].content_sha256 != digest for role, digest in expected_digests.items()):
            raise ValueError("candidate freeze metadata artifacts do not match their bound contract hashes")
        object.__setattr__(self, "artifacts", artifacts)
        _identify(self, id_field="candidate_freeze_id", prefix="candidate-freeze")
        return self


class GateOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class KnownReferenceControl(_StrictFrozenModel):
    control_kind: Literal["known_reference"] = "known_reference"
    control_id: str = Field(default="", pattern=r"^(?:|oracle-control:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    target: Literal[ResearchTarget.LARGE_MODEL_VALUE_V0] = ResearchTarget.LARGE_MODEL_VALUE_V0
    reference_engine: ImmutableArtifactRef
    independently_sourced_expected_output: ImmutableArtifactRef
    source_owner_id: str = Field(pattern=_STABLE_KEY_PATTERN)
    metric_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    absolute_tolerance: Decimal = Field(ge=0)
    relative_tolerance: Decimal = Field(ge=0)
    expected_outcome: Literal[GateOutcome.PASS] = GateOutcome.PASS
    independent_approval: IndependentApproval
    declared_at: datetime

    @field_validator("declared_at")
    @classmethod
    def validate_declared_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "declared_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.absolute_tolerance == 0 and self.relative_tolerance == 0:
            raise ValueError("known-reference control must freeze an explicit non-zero tolerance")
        if self.independent_approval.approved_at > self.declared_at:
            raise ValueError("known-reference approval must predate declaration")
        _identify(self, id_field="control_id", prefix="oracle-control")
        return self


class BrokenEngineNegativeControl(_StrictFrozenModel):
    control_kind: Literal["broken_engine"] = "broken_engine"
    control_id: str = Field(default="", pattern=r"^(?:|oracle-control:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    target: ResearchTarget
    broken_engine: ImmutableArtifactRef
    injected_fault: str = Field(min_length=1)
    metric_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    expected_outcome: Literal[GateOutcome.FAIL] = GateOutcome.FAIL
    independent_approval: IndependentApproval
    declared_at: datetime

    @field_validator("declared_at")
    @classmethod
    def validate_declared_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "declared_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.independent_approval.approved_at > self.declared_at:
            raise ValueError("negative-control approval must predate declaration")
        _identify(self, id_field="control_id", prefix="oracle-control")
        return self


OracleControl = Annotated[
    KnownReferenceControl | BrokenEngineNegativeControl,
    Field(discriminator="control_kind"),
]


class OracleProgram(_StrictFrozenModel):
    oracle_program_id: str = Field(default="", pattern=r"^(?:|oracle-program:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    semantics: ResearchSemanticsManifest
    protocol: EvaluationProtocol
    development_goldens: tuple[DevelopmentGoldenSet, ...] = Field(min_length=8, max_length=8)
    controls: tuple[OracleControl, ...] = Field(min_length=1)
    candidate_author_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("candidate_author_ids")
    @classmethod
    def validate_authors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_strings(values, "candidate_author_ids")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if (
            self.protocol.research_semantics_id != self.semantics.research_semantics_id
            or self.protocol.research_catalog_id != self.semantics.catalog.research_catalog_id
            or self.protocol.universe != self.semantics.universe
        ):
            raise ValueError("oracle protocol must bind the exact semantics, catalog, and UniverseRef")
        goldens = tuple(sorted(self.development_goldens, key=lambda item: item.target.value))
        if {golden.target for golden in goldens} != set(ResearchTarget):
            raise ValueError("independent development goldens must cover every evaluation target")
        controls = tuple(sorted(self.controls, key=lambda item: (item.control_kind, item.control_id)))
        if not any(isinstance(control, KnownReferenceControl) for control in controls):
            raise ValueError("oracle program requires an independent known-reference strategy control")
        if not any(isinstance(control, BrokenEngineNegativeControl) for control in controls):
            raise ValueError("oracle program requires a deliberately broken-engine negative control")

        candidate_authors = set(self.candidate_author_ids)
        reviewer_ids = {
            self.semantics.independent_approval.reviewer_id,
            self.semantics.gppe.financial_policy.independent_approval.reviewer_id,
            self.protocol.independent_approval.reviewer_id,
            *(golden.independent_approval.reviewer_id for golden in goldens),
            *(control.independent_approval.reviewer_id for control in controls),
        }
        if candidate_authors & reviewer_ids:
            raise ValueError("independent reviewers cannot be candidate authors")
        known_references = tuple(control for control in controls if isinstance(control, KnownReferenceControl))
        if any(control.source_owner_id in candidate_authors for control in known_references):
            raise ValueError("known-reference evidence must be sourced independently of candidate authors")
        if any(golden.frozen_at > self.protocol.predeclared_at for golden in goldens):
            raise ValueError("development goldens must be frozen before holdout protocol predeclaration")
        if any(control.declared_at > self.created_at for control in controls):
            raise ValueError("oracle controls must be declared before program creation")

        catalog_entries = {entry.catalog_entry_id: entry for entry in self.semantics.catalog.entries}
        binding_by_target = {binding.target: binding for binding in self.semantics.catalog_bindings}
        for plan in self.protocol.target_plans:
            entry = catalog_entries[binding_by_target[plan.target].catalog_entry_id]
            if not set(plan.canonical_question_ids) <= set(entry.canonical_question_ids):
                raise ValueError("evaluation plan questions are outside the exact bound catalog entry")
            if not set(plan.subject_scope) <= set(entry.subject_scope):
                raise ValueError("evaluation plan subject scope is outside the exact bound catalog entry")

        object.__setattr__(self, "development_goldens", goldens)
        object.__setattr__(self, "controls", controls)
        _identify(self, id_field="oracle_program_id", prefix="oracle-program")
        return self


class EvaluationAuthorization(_StrictFrozenModel):
    authorization_id: str = Field(default="", pattern=r"^(?:|evaluation-authorization:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    oracle_program_id: str = Field(pattern=r"^oracle-program:[0-9a-f]{64}$")
    oracle_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluation_protocol_id: str = Field(pattern=r"^evaluation-protocol:[0-9a-f]{64}$")
    evaluation_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    sealed_holdout_id: str = Field(pattern=r"^sealed-holdout:[0-9a-f]{64}$")
    sealed_holdout_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_freeze_id: str = Field(pattern=r"^candidate-freeze:[0-9a-f]{64}$")
    candidate_freeze_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewer_approval: IndependentApproval
    authorized_at: datetime

    @field_validator("authorized_at")
    @classmethod
    def validate_authorized_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "authorized_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        for reference_id, digest, name in (
            (self.oracle_program_id, self.oracle_program_sha256, "oracle program"),
            (self.evaluation_protocol_id, self.evaluation_protocol_sha256, "evaluation protocol"),
            (self.sealed_holdout_id, self.sealed_holdout_sha256, "sealed holdout"),
            (self.candidate_freeze_id, self.candidate_freeze_sha256, "candidate freeze"),
        ):
            _validate_ref_hash(reference_id, digest, name)
        if self.reviewer_approval.approved_at > self.authorized_at:
            raise ValueError("evaluation approval must not postdate authorization")
        _identify(self, id_field="authorization_id", prefix="evaluation-authorization")
        return self


class MetricObservation(_StrictFrozenModel):
    target: ResearchTarget
    metric_key: str = Field(pattern=_STABLE_KEY_PATTERN)
    observed_value: Decimal
    sample_count: int = Field(ge=1)


class EvaluationAttempt(_StrictFrozenModel):
    evaluation_attempt_id: str = Field(default="", pattern=r"^(?:|evaluation-attempt:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    program: OracleProgram
    holdout: SealedHoldout
    candidate_freeze: CandidateFreeze
    authorization: EvaluationAuthorization
    observations: tuple[MetricObservation, ...] = Field(min_length=1)
    result_artifact: ImmutableArtifactRef
    evaluated_by: str = Field(pattern=_STABLE_KEY_PATTERN)
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @property
    def outcome(self) -> GateOutcome:
        metrics = {
            (plan.target, metric.metric_key): metric
            for plan in self.program.protocol.target_plans
            for metric in plan.metrics
        }
        for observation in self.observations:
            metric = metrics[(observation.target, observation.metric_key)]
            passed = (
                observation.observed_value >= metric.threshold
                if metric.direction is MetricDirection.MINIMUM
                else observation.observed_value <= metric.threshold
            )
            if not passed or observation.sample_count < metric.minimum_sample_count:
                return GateOutcome.FAIL
        return GateOutcome.PASS

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.started_at < self.authorization.authorized_at or self.completed_at < self.started_at:
            raise ValueError("evaluation attempt timestamps are not monotonic")
        expected_bindings = (
            (self.authorization.oracle_program_id, self.program.oracle_program_id, "oracle program"),
            (
                self.authorization.evaluation_protocol_id,
                self.program.protocol.evaluation_protocol_id,
                "evaluation protocol",
            ),
            (self.authorization.sealed_holdout_id, self.holdout.sealed_holdout_id, "sealed holdout"),
            (
                self.authorization.candidate_freeze_id,
                self.candidate_freeze.candidate_freeze_id,
                "candidate freeze",
            ),
        )
        if any(actual != expected for actual, expected, _ in expected_bindings):
            mismatches = [name for actual, expected, name in expected_bindings if actual != expected]
            raise ValueError(f"evaluation attempt authorization binding mismatch: {mismatches}")
        if self.evaluated_by not in set(self.holdout.custody.authorized_label_reader_ids):
            raise ValueError("evaluator is not authorized to read protected labels")
        if self.evaluated_by in set(self.program.candidate_author_ids):
            raise ValueError("candidate authors cannot evaluate protected holdout labels")
        observations = tuple(sorted(self.observations, key=lambda item: (item.target.value, item.metric_key)))
        coordinates = [(item.target, item.metric_key) for item in observations]
        expected_coordinates = {
            (plan.target, metric.metric_key) for plan in self.program.protocol.target_plans for metric in plan.metrics
        }
        if len(coordinates) != len(set(coordinates)) or set(coordinates) != expected_coordinates:
            raise ValueError("evaluation observations must cover every predeclared metric exactly once")
        available_samples = {
            target: sum(count.sample_count for count in self.holdout.stratum_sample_counts if count.target is target)
            for target in ResearchTarget
        }
        if any(observation.sample_count > available_samples[observation.target] for observation in observations):
            raise ValueError("metric observation sample count exceeds the sealed holdout population")
        object.__setattr__(self, "observations", observations)
        _identify(self, id_field="evaluation_attempt_id", prefix="evaluation-attempt")
        return self


def authorize_evaluation(
    *,
    program: OracleProgram,
    holdout: SealedHoldout,
    candidate_freeze: CandidateFreeze,
    reviewer_approval: IndependentApproval,
    authorized_at: datetime,
    prior_attempts: tuple[EvaluationAttempt, ...] = (),
) -> EvaluationAuthorization:
    """Authorize label access only for an exact frozen candidate and fresh holdout."""

    authorized_at = _require_aware(authorized_at, "authorized_at")
    protocol = program.protocol
    semantics = program.semantics
    if (
        holdout.evaluation_protocol_id != protocol.evaluation_protocol_id
        or holdout.evaluation_protocol_sha256 != protocol.content_sha256
    ):
        raise ValueError("holdout does not bind the exact predeclared evaluation protocol")
    expected_questions = {question_id for plan in protocol.target_plans for question_id in plan.canonical_question_ids}
    if set(holdout.selected_question_ids) != expected_questions:
        raise ValueError("holdout question scope differs from the predeclared protocol")
    expected_strata = {
        (plan.target, stratum.stratum_key): stratum.minimum_sample_count
        for plan in protocol.target_plans
        for stratum in plan.strata
    }
    actual_strata = {(item.target, item.stratum_key): item.sample_count for item in holdout.stratum_sample_counts}
    if set(actual_strata) != set(expected_strata):
        raise ValueError("holdout strata differ from the predeclared protocol")
    if any(actual_strata[key] < minimum for key, minimum in expected_strata.items()):
        raise ValueError("holdout does not meet predeclared minimum sample counts")

    exact_candidate_bindings = (
        candidate_freeze.research_semantics_id == semantics.research_semantics_id,
        candidate_freeze.research_semantics_sha256 == semantics.content_sha256,
        candidate_freeze.research_catalog_id == semantics.catalog.research_catalog_id,
        candidate_freeze.research_catalog_sha256 == semantics.catalog.content_sha256,
        candidate_freeze.evaluation_protocol_id == protocol.evaluation_protocol_id,
        candidate_freeze.evaluation_protocol_sha256 == protocol.content_sha256,
        candidate_freeze.universe == semantics.universe,
        candidate_freeze.sealed_holdout_id == holdout.sealed_holdout_id,
        candidate_freeze.sealed_holdout_sha256 == holdout.content_sha256,
        candidate_freeze.candidate_author_ids == program.candidate_author_ids,
    )
    if not all(exact_candidate_bindings):
        raise ValueError("candidate freeze does not bind the exact program, catalog, universe, and holdout")
    if candidate_freeze.frozen_at < holdout.custody.sealed_at:
        raise ValueError("candidate must freeze after the holdout sample and protected labels are sealed")
    if candidate_freeze.frozen_at > authorized_at:
        raise ValueError("candidate freeze cannot postdate evaluation authorization")

    candidate_authors = set(program.candidate_author_ids)
    custodian = holdout.custody.custodian_id
    if custodian in candidate_authors:
        raise ValueError("holdout custodian cannot be a candidate author")
    if candidate_authors & set(holdout.custody.authorized_label_reader_ids):
        raise ValueError("candidate authors cannot be authorized protected-label readers")
    if reviewer_approval.reviewer_id in candidate_authors:
        raise ValueError("evaluation reviewer cannot be a candidate author")
    if reviewer_approval.reviewer_id == custodian:
        raise ValueError("evaluation reviewer cannot also be the holdout custodian")
    program_reviewers = {
        program.semantics.independent_approval.reviewer_id,
        program.semantics.gppe.financial_policy.independent_approval.reviewer_id,
        program.protocol.independent_approval.reviewer_id,
        *(golden.independent_approval.reviewer_id for golden in program.development_goldens),
        *(control.independent_approval.reviewer_id for control in program.controls),
    }
    if custodian in program_reviewers:
        raise ValueError("holdout custodian cannot be an independent program reviewer")

    relevant_attempts = tuple(sorted(prior_attempts, key=lambda attempt: attempt.completed_at))
    if relevant_attempts:
        previous = relevant_attempts[-1]
        if previous.program.protocol.evaluation_protocol_id != protocol.evaluation_protocol_id:
            raise ValueError("threshold or scope changes after an observed result are forbidden")
        if holdout.sealed_holdout_id == previous.holdout.sealed_holdout_id:
            raise ValueError("a completed evaluation cannot reuse its protected holdout")
        if (
            holdout.generation != previous.holdout.generation + 1
            or holdout.predecessor_holdout_id != previous.holdout.sealed_holdout_id
            or holdout.sampled_at <= previous.completed_at
            or holdout.sample_artifact.artifact_id == previous.holdout.sample_artifact.artifact_id
            or holdout.custody.protected_labels.artifact.artifact_id
            == previous.holdout.custody.protected_labels.artifact.artifact_id
        ):
            raise ValueError("a post-result candidate requires a newly sampled, untouched holdout generation")

    return EvaluationAuthorization(
        oracle_program_id=program.oracle_program_id,
        oracle_program_sha256=program.content_sha256,
        evaluation_protocol_id=protocol.evaluation_protocol_id,
        evaluation_protocol_sha256=protocol.content_sha256,
        sealed_holdout_id=holdout.sealed_holdout_id,
        sealed_holdout_sha256=holdout.content_sha256,
        candidate_freeze_id=candidate_freeze.candidate_freeze_id,
        candidate_freeze_sha256=candidate_freeze.content_sha256,
        reviewer_approval=reviewer_approval,
        authorized_at=authorized_at,
    )
