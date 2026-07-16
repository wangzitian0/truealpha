"""Pure, versioned GPPE v0 and three-tier computation for Production TOPT."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.research import ValuationTier

_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_ID_PATTERN = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    return value


def _normalize_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal("0")
    with localcontext(Context(prec=max(_DECIMAL_CONTEXT.prec, len(value.as_tuple().digits)))):
        return value.normalize()


def _identify(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    digest = canonical_sha256(payload)
    expected_id = f"{prefix}:{digest}"
    supplied_id = getattr(model, id_field)
    supplied_hash = getattr(model, "content_sha256")
    if supplied_id not in {"", expected_id} or supplied_hash not in {"", digest}:
        raise ValueError(f"{prefix} identity does not match its canonical content")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", digest)


class MetricAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class MetricFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class ToptCoreAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class OperatingBranch(StrEnum):
    NON_FINANCIAL = "non_financial"
    FINANCIAL = "financial"


class OperatingEfficiencyMetric(StrEnum):
    CAPITAL_ADJUSTED_GPPE = "capital_adjusted_gppe"
    PRE_PROVISION_PROFIT_PER_EMPLOYEE = "pre_provision_profit_per_employee"


class ToptCoreReasonCode(StrEnum):
    MISSING_GROSS_PROFIT = "missing_gross_profit"
    MISSING_TOTAL_ASSETS = "missing_total_assets"
    MISSING_HEADCOUNT = "missing_headcount"
    MISSING_REVENUE = "missing_revenue"
    MISSING_SHARES_OUTSTANDING = "missing_shares_outstanding"
    MISSING_MARKET_PRICE = "missing_market_price"
    MISSING_PRE_PROVISION_PROFIT = "missing_pre_provision_profit"
    NONPOSITIVE_HEADCOUNT = "nonpositive_headcount"
    NONPOSITIVE_REVENUE = "nonpositive_revenue"
    NONPOSITIVE_SHARES_OUTSTANDING = "nonpositive_shares_outstanding"
    NONPOSITIVE_MARKET_PRICE = "nonpositive_market_price"
    STALE_INPUT = "stale_input"
    UNKNOWN_FRESHNESS = "unknown_freshness"
    FINANCIAL_VALUATION_NOT_COMPARABLE = "financial_valuation_not_comparable"


class ToptMetricInput(_FrozenModel):
    """One provenance-neutral value selected into an exact snapshot."""

    input_id: str = Field(pattern=r"^normalized-observation:[0-9a-f]{64}$")
    metric: str = Field(min_length=1)
    value: Decimal | None = None
    unit: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
    knowable_at: datetime
    freshness: MetricFreshness
    availability: MetricAvailability

    @field_validator("value", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("knowable_at")
    @classmethod
    def normalize_knowable_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "knowable_at")

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if (self.availability is MetricAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("metric value must exist exactly when the input is available")
        if self.value is not None:
            if not self.value.is_finite():
                raise ValueError("metric value must be finite")
            object.__setattr__(self, "value", _normalize_decimal(self.value))
        return self


class ToptCellQualityInput(_FrozenModel):
    """Quality carried by every selected semantic cell, including identity cells."""

    input_id: str = Field(pattern=r"^normalized-observation:[0-9a-f]{64}$")
    confidence: Decimal = Field(ge=0, le=1)
    knowable_at: datetime
    freshness: MetricFreshness

    @field_validator("confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("knowable_at")
    @classmethod
    def normalize_knowable_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "knowable_at")


class ToptMarketValueComponent(_FrozenModel):
    instrument_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    market_price: ToptMetricInput
    shares_outstanding: ToptMetricInput

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        if self.market_price.metric != "market_price":
            raise ValueError("market-value component carries the wrong price metric")
        if self.shares_outstanding.metric != "shares_outstanding":
            raise ValueError("market-value component carries the wrong shares metric")
        return self


class GppeV0Definition(_FrozenModel):
    """Owner-selected v0 capital-adjusted labor-efficiency definition."""

    definition_id: str = Field(default="", pattern=r"^(?:|gppe-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_id: Literal["gross_profit_per_employee"] = "gross_profit_per_employee"
    factor_version: Literal["production-topt-v0.1.0"] = "production-topt-v0.1.0"
    formula: Literal["(gross_profit-total_assets*risk_free_rate)/headcount"] = (
        "(gross_profit-total_assets*risk_free_rate)/headcount"
    )
    financial_formula: Literal["pre_provision_profit/headcount"] = "pre_provision_profit/headcount"
    risk_free_benchmark: Literal["3m-us-tbill"] = "3m-us-tbill"
    risk_free_rate: Decimal = Field(ge=0, le=1)
    output_unit: Literal["reporting_currency_per_employee"] = "reporting_currency_per_employee"
    confidence_rule: Literal["minimum_consumed_confidence"] = "minimum_consumed_confidence"
    stale_input_behavior: Literal["unavailable"] = "unavailable"

    @field_validator("risk_free_rate", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("risk_free_rate")
    @classmethod
    def normalize_risk_free_rate(cls, value: Decimal) -> Decimal:
        return _normalize_decimal(value)

    @model_validator(mode="after")
    def identify(self) -> Self:
        _identify(self, id_field="definition_id", prefix="gppe-definition")
        return self


class TierBandDefinition(_FrozenModel):
    tier: ValuationTier
    gppe_lower: Decimal | None = None
    gppe_upper: Decimal | None = None
    target_ps_lower: Decimal = Field(gt=0)
    target_ps_upper: Decimal = Field(gt=0)

    @field_validator("gppe_lower", "gppe_upper", "target_ps_lower", "target_ps_upper", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("gppe_lower", "gppe_upper", "target_ps_lower", "target_ps_upper")
    @classmethod
    def normalize_bounds(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else _normalize_decimal(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.gppe_lower is not None and self.gppe_upper is not None and self.gppe_lower >= self.gppe_upper:
            raise ValueError("GPPE tier lower bound must be below its upper bound")
        if self.target_ps_lower >= self.target_ps_upper:
            raise ValueError("target P/S lower bound must be below its upper bound")
        return self


class ThreeTierV0Definition(_FrozenModel):
    definition_id: str = Field(default="", pattern=r"^(?:|three-tier-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_id: Literal["three_tier_valuation"] = "three_tier_valuation"
    factor_version: Literal["production-topt-v0.1.0"] = "production-topt-v0.1.0"
    boundary_rule: Literal["lower_inclusive_upper_exclusive"] = "lower_inclusive_upper_exclusive"
    valuation_gap_formula: Literal["target_ps_midpoint/current_ps-1"] = "target_ps_midpoint/current_ps-1"
    bands: tuple[TierBandDefinition, TierBandDefinition, TierBandDefinition] = (
        TierBandDefinition(
            tier=ValuationTier.TRADITIONAL,
            gppe_upper=Decimal("1000000"),
            target_ps_lower=Decimal("3"),
            target_ps_upper=Decimal("4"),
        ),
        TierBandDefinition(
            tier=ValuationTier.TECH,
            gppe_lower=Decimal("1000000"),
            gppe_upper=Decimal("3000000"),
            target_ps_lower=Decimal("8"),
            target_ps_upper=Decimal("10"),
        ),
        TierBandDefinition(
            tier=ValuationTier.LARGE_MODEL_NATIVE,
            gppe_lower=Decimal("3000000"),
            target_ps_lower=Decimal("20"),
            target_ps_upper=Decimal("30"),
        ),
    )
    confidence_rule: Literal["minimum_consumed_confidence"] = "minimum_consumed_confidence"

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        expected_tiers = (
            ValuationTier.TRADITIONAL,
            ValuationTier.TECH,
            ValuationTier.LARGE_MODEL_NATIVE,
        )
        if tuple(band.tier for band in self.bands) != expected_tiers:
            raise ValueError("three-tier definition must use canonical tier order")
        if self.bands[0].gppe_lower is not None or self.bands[-1].gppe_upper is not None:
            raise ValueError("three-tier definition must cover the complete GPPE domain")
        for left, right in zip(self.bands, self.bands[1:]):
            if left.gppe_upper != right.gppe_lower:
                raise ValueError("three-tier definition must be contiguous")
        _identify(self, id_field="definition_id", prefix="three-tier-definition")
        return self


class ToptCoreSnapshotInput(_FrozenModel):
    """Exact one-issuer projection with complete share-class market value."""

    snapshot_id: str = Field(pattern=r"^topt-core-snapshot:[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^capture-run:[0-9a-f]{64}$")
    release_manifest_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    universe_id: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    universe_sha256: str = Field(pattern=_SHA256_PATTERN)
    cutoff: datetime
    issuer_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    operating_branch: OperatingBranch
    observation_ids: tuple[str, ...]
    cell_inputs: tuple[ToptCellQualityInput, ...]
    gross_profit: ToptMetricInput | None
    total_assets: ToptMetricInput | None
    headcount: ToptMetricInput | None
    revenue: ToptMetricInput | None
    pre_provision_profit: ToptMetricInput | None
    market_value_components: tuple[ToptMarketValueComponent, ...]

    @field_validator("cutoff")
    @classmethod
    def normalize_cutoff(cls, value: datetime) -> datetime:
        return _aware_utc(value, "cutoff")

    @field_validator("observation_ids")
    @classmethod
    def validate_observation_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            len(values) < 4
            or len(values) % 4 != 0
            or len(set(values)) != len(values)
            or tuple(sorted(values)) != values
        ):
            raise ValueError("issuer snapshot observation IDs must contain sorted unique four-cell listing groups")
        if any(not value.startswith("normalized-observation:") for value in values):
            raise ValueError("snapshot members must use normalized observation identities")
        return values

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        expected_metrics = {
            "gross_profit": self.gross_profit,
            "total_assets": self.total_assets,
            "headcount": self.headcount,
            "revenue": self.revenue,
            "pre_provision_profit": self.pre_provision_profit,
        }
        for expected_metric, metric in expected_metrics.items():
            if metric is None:
                continue
            if metric.metric != expected_metric:
                raise ValueError(f"{expected_metric} input carries the wrong metric identity")
            if metric.input_id not in self.observation_ids:
                raise ValueError(f"{expected_metric} input is not selected into the snapshot")
            if metric.knowable_at > self.cutoff:
                raise ValueError(f"{expected_metric} input is future-dated")
        if tuple(sorted(item.input_id for item in self.cell_inputs)) != self.observation_ids:
            raise ValueError("cell quality inputs must cover every selected observation exactly once")
        if any(item.knowable_at > self.cutoff for item in self.cell_inputs):
            raise ValueError("cell quality input is future-dated")
        component_listings = tuple(item.listing_id for item in self.market_value_components)
        if not component_listings or len(component_listings) != len(set(component_listings)):
            raise ValueError("issuer market-value components must contain unique listings")
        if self.listing_id not in component_listings:
            raise ValueError("execution listing must belong to the issuer market-value components")
        for component in self.market_value_components:
            for metric in (component.market_price, component.shares_outstanding):
                if metric.input_id not in self.observation_ids:
                    raise ValueError("market-value component is not selected into the snapshot")
                if metric.knowable_at > self.cutoff:
                    raise ValueError("market-value component is future-dated")
        return self


class ToptCoreResult(_FrozenModel):
    result_id: str = Field(default="", pattern=r"^(?:|topt-core-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    invocation_id: str = Field(pattern=r"^topt-core-invocation:[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^topt-core-snapshot:[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^capture-run:[0-9a-f]{64}$")
    release_manifest_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    universe_id: str
    universe_version: str
    universe_sha256: str = Field(pattern=_SHA256_PATTERN)
    cutoff: datetime
    issuer_id: str
    instrument_id: str
    listing_id: str
    operating_branch: OperatingBranch
    operating_metric: OperatingEfficiencyMetric
    availability: ToptCoreAvailability
    operating_efficiency: Decimal | None = None
    capital_adjusted_gross_profit: Decimal | None = None
    gppe: Decimal | None = None
    tier: ValuationTier | None = None
    target_ps_lower: Decimal | None = None
    target_ps_upper: Decimal | None = None
    target_ps_midpoint: Decimal | None = None
    current_ps: Decimal | None = None
    valuation_gap: Decimal | None = None
    confidence: Decimal = Field(ge=0, le=1)
    freshness: MetricFreshness
    reason_codes: tuple[ToptCoreReasonCode, ...] = ()
    input_observation_ids: tuple[str, ...]
    gppe_definition_id: str = Field(pattern=r"^gppe-definition:[0-9a-f]{64}$")
    gppe_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    tier_definition_id: str = Field(pattern=r"^three-tier-definition:[0-9a-f]{64}$")
    tier_definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("cutoff")
    @classmethod
    def normalize_cutoff(cls, value: datetime) -> datetime:
        return _aware_utc(value, "cutoff")

    @field_validator(
        "capital_adjusted_gross_profit",
        "operating_efficiency",
        "gppe",
        "target_ps_lower",
        "target_ps_upper",
        "target_ps_midpoint",
        "current_ps",
        "valuation_gap",
        "confidence",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        numeric_values = (
            self.capital_adjusted_gross_profit,
            self.gppe,
            self.target_ps_lower,
            self.target_ps_upper,
            self.target_ps_midpoint,
            self.current_ps,
            self.valuation_gap,
        )
        if self.availability is ToptCoreAvailability.AVAILABLE:
            if (
                self.operating_branch is not OperatingBranch.NON_FINANCIAL
                or self.operating_metric is not OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
                or self.operating_efficiency is None
                or any(value is None for value in numeric_values)
                or self.tier is None
                or self.reason_codes
            ):
                raise ValueError("available TOPT core result requires all values and no reason codes")
        else:
            financial_efficiency_only = (
                self.operating_branch is OperatingBranch.FINANCIAL
                and self.operating_metric is OperatingEfficiencyMetric.PRE_PROVISION_PROFIT_PER_EMPLOYEE
                and self.operating_efficiency is not None
                and ToptCoreReasonCode.FINANCIAL_VALUATION_NOT_COMPARABLE in self.reason_codes
            )
            if (
                any(value is not None for value in numeric_values)
                or self.tier is not None
                or not self.reason_codes
                or (self.operating_efficiency is not None and not financial_efficiency_only)
            ):
                raise ValueError("unavailable TOPT core result carries invalid computed values")
        for field_name in (
            "capital_adjusted_gross_profit",
            "operating_efficiency",
            "gppe",
            "target_ps_lower",
            "target_ps_upper",
            "target_ps_midpoint",
            "current_ps",
            "valuation_gap",
        ):
            value = getattr(self, field_name)
            if value is not None:
                if not value.is_finite():
                    raise ValueError("TOPT core result decimals must be finite")
                object.__setattr__(self, field_name, _normalize_decimal(value))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes), key=str)))
        _identify(self, id_field="result_id", prefix="topt-core-result")
        return self


def _tier_band(value: Decimal, definition: ThreeTierV0Definition) -> TierBandDefinition:
    for band in definition.bands:
        if (band.gppe_lower is None or value >= band.gppe_lower) and (
            band.gppe_upper is None or value < band.gppe_upper
        ):
            return band
    raise AssertionError("validated tier definition does not cover GPPE")


def _unavailable(
    snapshot: ToptCoreSnapshotInput,
    *,
    invocation_id: str,
    gppe_definition: GppeV0Definition,
    tier_definition: ThreeTierV0Definition,
    reasons: tuple[ToptCoreReasonCode, ...],
    freshness: MetricFreshness,
    confidence: Decimal = Decimal("0"),
    operating_efficiency: Decimal | None = None,
) -> ToptCoreResult:
    operating_metric = (
        OperatingEfficiencyMetric.PRE_PROVISION_PROFIT_PER_EMPLOYEE
        if snapshot.operating_branch is OperatingBranch.FINANCIAL
        else OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
    )
    return ToptCoreResult(
        invocation_id=invocation_id,
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
        release_manifest_id=snapshot.release_manifest_id,
        universe_id=snapshot.universe_id,
        universe_version=snapshot.universe_version,
        universe_sha256=snapshot.universe_sha256,
        cutoff=snapshot.cutoff,
        issuer_id=snapshot.issuer_id,
        instrument_id=snapshot.instrument_id,
        listing_id=snapshot.listing_id,
        operating_branch=snapshot.operating_branch,
        operating_metric=operating_metric,
        availability=ToptCoreAvailability.UNAVAILABLE,
        operating_efficiency=operating_efficiency,
        confidence=confidence,
        freshness=freshness,
        reason_codes=reasons,
        input_observation_ids=snapshot.observation_ids,
        gppe_definition_id=gppe_definition.definition_id,
        gppe_definition_sha256=gppe_definition.content_sha256,
        tier_definition_id=tier_definition.definition_id,
        tier_definition_sha256=tier_definition.content_sha256,
    )


def compute_topt_core(
    snapshot: ToptCoreSnapshotInput,
    *,
    invocation_id: str,
    gppe_definition: GppeV0Definition,
    tier_definition: ThreeTierV0Definition,
) -> ToptCoreResult:
    """Compute one issuer result without source, storage, or provenance branching."""

    if not invocation_id.startswith("topt-core-invocation:"):
        raise ValueError("invocation_id must be a content-addressed TOPT core invocation")
    inputs = {ToptCoreReasonCode.MISSING_HEADCOUNT: snapshot.headcount}
    if snapshot.operating_branch is OperatingBranch.FINANCIAL:
        inputs[ToptCoreReasonCode.MISSING_PRE_PROVISION_PROFIT] = snapshot.pre_provision_profit
    else:
        inputs.update(
            {
                ToptCoreReasonCode.MISSING_GROSS_PROFIT: snapshot.gross_profit,
                ToptCoreReasonCode.MISSING_TOTAL_ASSETS: snapshot.total_assets,
                ToptCoreReasonCode.MISSING_REVENUE: snapshot.revenue,
            }
        )
    reasons = tuple(
        reason
        for reason, metric in inputs.items()
        if metric is None or metric.availability is MetricAvailability.UNAVAILABLE or metric.value is None
    )
    if snapshot.operating_branch is OperatingBranch.NON_FINANCIAL:
        if any(
            component.shares_outstanding.availability is MetricAvailability.UNAVAILABLE
            or component.shares_outstanding.value is None
            for component in snapshot.market_value_components
        ):
            reasons = (*reasons, ToptCoreReasonCode.MISSING_SHARES_OUTSTANDING)
        if any(
            component.market_price.availability is MetricAvailability.UNAVAILABLE
            or component.market_price.value is None
            for component in snapshot.market_value_components
        ):
            reasons = (*reasons, ToptCoreReasonCode.MISSING_MARKET_PRICE)
    freshness = (
        MetricFreshness.STALE
        if any(cell.freshness is MetricFreshness.STALE for cell in snapshot.cell_inputs)
        else MetricFreshness.UNKNOWN
        if any(cell.freshness is MetricFreshness.UNKNOWN for cell in snapshot.cell_inputs)
        else MetricFreshness.FRESH
    )
    if freshness is MetricFreshness.STALE:
        reasons = (*reasons, ToptCoreReasonCode.STALE_INPUT)
    elif freshness is MetricFreshness.UNKNOWN:
        reasons = (*reasons, ToptCoreReasonCode.UNKNOWN_FRESHNESS)
    if reasons:
        return _unavailable(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            tier_definition=tier_definition,
            reasons=tuple(reasons),
            freshness=freshness,
        )

    assert snapshot.headcount is not None and snapshot.headcount.value is not None

    invalid_reasons: list[ToptCoreReasonCode] = []
    if snapshot.headcount.value <= 0:
        invalid_reasons.append(ToptCoreReasonCode.NONPOSITIVE_HEADCOUNT)
    if snapshot.operating_branch is OperatingBranch.NON_FINANCIAL:
        assert snapshot.revenue is not None and snapshot.revenue.value is not None
        if snapshot.revenue.value <= 0:
            invalid_reasons.append(ToptCoreReasonCode.NONPOSITIVE_REVENUE)
        if any(
            component.shares_outstanding.value is not None and component.shares_outstanding.value <= 0
            for component in snapshot.market_value_components
        ):
            invalid_reasons.append(ToptCoreReasonCode.NONPOSITIVE_SHARES_OUTSTANDING)
        if any(
            component.market_price.value is not None and component.market_price.value <= 0
            for component in snapshot.market_value_components
        ):
            invalid_reasons.append(ToptCoreReasonCode.NONPOSITIVE_MARKET_PRICE)
    if invalid_reasons:
        return _unavailable(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            tier_definition=tier_definition,
            reasons=tuple(invalid_reasons),
            freshness=freshness,
        )

    confidence = min(cell.confidence for cell in snapshot.cell_inputs)
    if snapshot.operating_branch is OperatingBranch.FINANCIAL:
        assert snapshot.pre_provision_profit is not None and snapshot.pre_provision_profit.value is not None
        with localcontext(_DECIMAL_CONTEXT):
            financial_efficiency = snapshot.pre_provision_profit.value / snapshot.headcount.value
        return _unavailable(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            tier_definition=tier_definition,
            reasons=(ToptCoreReasonCode.FINANCIAL_VALUATION_NOT_COMPARABLE,),
            freshness=freshness,
            confidence=confidence,
            operating_efficiency=financial_efficiency,
        )

    assert snapshot.gross_profit is not None and snapshot.gross_profit.value is not None
    assert snapshot.total_assets is not None and snapshot.total_assets.value is not None
    assert snapshot.revenue is not None and snapshot.revenue.value is not None
    for component in snapshot.market_value_components:
        assert component.market_price.value is not None
        assert component.shares_outstanding.value is not None
    with localcontext(_DECIMAL_CONTEXT):
        capital_adjusted = snapshot.gross_profit.value - (snapshot.total_assets.value * gppe_definition.risk_free_rate)
        gppe = capital_adjusted / snapshot.headcount.value
        market_cap = Decimal("0")
        for component in snapshot.market_value_components:
            assert component.market_price.value is not None
            assert component.shares_outstanding.value is not None
            market_cap += component.market_price.value * component.shares_outstanding.value
        current_ps = market_cap / snapshot.revenue.value
        band = _tier_band(gppe, tier_definition)
        midpoint = (band.target_ps_lower + band.target_ps_upper) / Decimal("2")
        valuation_gap = midpoint / current_ps - Decimal("1")
    return ToptCoreResult(
        invocation_id=invocation_id,
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
        release_manifest_id=snapshot.release_manifest_id,
        universe_id=snapshot.universe_id,
        universe_version=snapshot.universe_version,
        universe_sha256=snapshot.universe_sha256,
        cutoff=snapshot.cutoff,
        issuer_id=snapshot.issuer_id,
        instrument_id=snapshot.instrument_id,
        listing_id=snapshot.listing_id,
        operating_branch=snapshot.operating_branch,
        operating_metric=OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE,
        availability=ToptCoreAvailability.AVAILABLE,
        operating_efficiency=gppe,
        capital_adjusted_gross_profit=capital_adjusted,
        gppe=gppe,
        tier=band.tier,
        target_ps_lower=band.target_ps_lower,
        target_ps_upper=band.target_ps_upper,
        target_ps_midpoint=midpoint,
        current_ps=current_ps,
        valuation_gap=valuation_gap,
        confidence=confidence,
        freshness=freshness,
        input_observation_ids=snapshot.observation_ids,
        gppe_definition_id=gppe_definition.definition_id,
        gppe_definition_sha256=gppe_definition.content_sha256,
        tier_definition_id=tier_definition.definition_id,
        tier_definition_sha256=tier_definition.content_sha256,
    )
