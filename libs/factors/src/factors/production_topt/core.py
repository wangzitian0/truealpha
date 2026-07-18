"""Pure, versioned GPPE v0 and three-tier computation for Production TOPT.

**Converged onto the base `large_model_value_v0` semantics (#394, `production-topt-v0.2.0`).**
GPPE is one uniform capital-adjusted formula for every issuer —
`(numerator - total_assets*risk_free_rate)/headcount`, where the numerator is the
parser's industry-branch profit (pre-provision profit for financial issuers, gross
profit otherwise) — identical to `factors.base.gross_profit_per_employee`. The
v0.1.0 financial short-circuit (`pre_provision_profit/headcount` with no capital
charge, then `FINANCIAL_VALUATION_NOT_COMPARABLE`) is retired; financial issuers now
flow through the same tier / P-S valuation path as every other issuer.

The tier-band parameters here remain TOPT's own calibration and are versioned with
the `production-topt-*` coordinates; aligning them to the base bands, if desired, is
a separate parameter change. This module still writes the `mart.topt_*` namespace
(disjoint from the base replay's `mart.strategy_*`); the v0.1.0 rows stay as an
append-only prior vintage.
"""

from __future__ import annotations

import re
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
_OBSERVATION_ID_PATTERN = re.compile(r"^normalized-observation:[0-9a-f]{64}$")


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
    """v0.2.0 uniform capital-adjusted labor-efficiency definition.

    Converged onto the base `large_model_value_v0` (#394): one formula for every
    issuer, `(numerator - total_assets*risk_free_rate)/headcount`, where the
    numerator is the parser's industry-branch profit (pre-provision profit for
    financial issuers, gross profit otherwise). v0.1.0's financial short-circuit
    (`pre_provision_profit/headcount`, no capital charge) is retired.
    """

    definition_id: str = Field(default="", pattern=r"^(?:|gppe-definition:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    factor_id: Literal["gross_profit_per_employee"] = "gross_profit_per_employee"
    factor_version: Literal["production-topt-v0.2.0"] = "production-topt-v0.2.0"
    formula: Literal["(numerator-total_assets*risk_free_rate)/headcount"] = (
        "(numerator-total_assets*risk_free_rate)/headcount"
    )
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
    factor_version: Literal["production-topt-v0.2.0"] = "production-topt-v0.2.0"
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
        if any(_OBSERVATION_ID_PATTERN.fullmatch(value) is None for value in values):
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


class ToptGppeResult(_FrozenModel):
    """Materialized module-2 output consumed by the tier composite."""

    result_id: str = Field(default="", pattern=r"^(?:|topt-gppe-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    invocation_id: str = Field(pattern=r"^topt-gppe-invocation:[0-9a-f]{64}$")
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
    confidence: Decimal = Field(ge=0, le=1)
    freshness: MetricFreshness
    reason_codes: tuple[ToptCoreReasonCode, ...] = ()
    input_observation_ids: tuple[str, ...]
    gppe_definition_id: str = Field(pattern=r"^gppe-definition:[0-9a-f]{64}$")
    gppe_definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("cutoff")
    @classmethod
    def normalize_cutoff(cls, value: datetime) -> datetime:
        return _aware_utc(value, "cutoff")

    @field_validator(
        "operating_efficiency",
        "capital_adjusted_gross_profit",
        "gppe",
        "confidence",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.availability is ToptCoreAvailability.AVAILABLE:
            valid = (
                self.operating_metric is OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
                and self.operating_efficiency is not None
                and self.capital_adjusted_gross_profit is not None
                and self.gppe is not None
            )
            if not valid or self.reason_codes:
                raise ValueError("available GPPE result carries invalid values")
        elif (
            self.operating_efficiency is not None
            or self.capital_adjusted_gross_profit is not None
            or self.gppe is not None
            or not self.reason_codes
        ):
            raise ValueError("unavailable GPPE result carries computed values")
        for field_name in ("operating_efficiency", "capital_adjusted_gross_profit", "gppe"):
            value = getattr(self, field_name)
            if value is not None:
                if not value.is_finite():
                    raise ValueError("GPPE result decimals must be finite")
                object.__setattr__(self, field_name, _normalize_decimal(value))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes), key=str)))
        _identify(self, id_field="result_id", prefix="topt-gppe-result")
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
    gppe_invocation_id: str = Field(pattern=r"^topt-gppe-invocation:[0-9a-f]{64}$")
    gppe_result_id: str = Field(pattern=r"^topt-gppe-result:[0-9a-f]{64}$")
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
                self.operating_metric is not OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
                or self.operating_efficiency is None
                or any(value is None for value in numeric_values)
                or self.tier is None
                or self.reason_codes
            ):
                raise ValueError("available TOPT core result requires all values and no reason codes")
        else:
            if (
                any(value is not None for value in numeric_values)
                or self.tier is not None
                or not self.reason_codes
                or self.operating_efficiency is not None
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


def _snapshot_freshness(snapshot: ToptCoreSnapshotInput) -> MetricFreshness:
    if any(cell.freshness is MetricFreshness.STALE for cell in snapshot.cell_inputs):
        return MetricFreshness.STALE
    if any(cell.freshness is MetricFreshness.UNKNOWN for cell in snapshot.cell_inputs):
        return MetricFreshness.UNKNOWN
    return MetricFreshness.FRESH


def _unavailable_gppe(
    snapshot: ToptCoreSnapshotInput,
    *,
    invocation_id: str,
    gppe_definition: GppeV0Definition,
    reasons: tuple[ToptCoreReasonCode, ...],
    freshness: MetricFreshness,
) -> ToptGppeResult:
    operating_metric = OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
    return ToptGppeResult(
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
        confidence=Decimal("0"),
        freshness=freshness,
        reason_codes=reasons,
        input_observation_ids=snapshot.observation_ids,
        gppe_definition_id=gppe_definition.definition_id,
        gppe_definition_sha256=gppe_definition.content_sha256,
    )


def compute_topt_gppe(
    snapshot: ToptCoreSnapshotInput,
    *,
    invocation_id: str,
    gppe_definition: GppeV0Definition,
) -> ToptGppeResult:
    """Compute the module-2 base output without invoking the tier composite."""

    if not invocation_id.startswith("topt-gppe-invocation:"):
        raise ValueError("invocation_id must be a content-addressed TOPT GPPE invocation")
    inputs = {
        ToptCoreReasonCode.MISSING_HEADCOUNT: snapshot.headcount,
        ToptCoreReasonCode.MISSING_TOTAL_ASSETS: snapshot.total_assets,
    }
    if snapshot.operating_branch is OperatingBranch.FINANCIAL:
        inputs[ToptCoreReasonCode.MISSING_PRE_PROVISION_PROFIT] = snapshot.pre_provision_profit
    else:
        inputs[ToptCoreReasonCode.MISSING_GROSS_PROFIT] = snapshot.gross_profit
    reasons = tuple(
        reason
        for reason, metric in inputs.items()
        if metric is None or metric.availability is MetricAvailability.UNAVAILABLE or metric.value is None
    )
    freshness = _snapshot_freshness(snapshot)
    if freshness is MetricFreshness.STALE:
        reasons = (*reasons, ToptCoreReasonCode.STALE_INPUT)
    elif freshness is MetricFreshness.UNKNOWN:
        reasons = (*reasons, ToptCoreReasonCode.UNKNOWN_FRESHNESS)
    if reasons:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=tuple(reasons),
            freshness=freshness,
        )

    assert snapshot.headcount is not None and snapshot.headcount.value is not None

    invalid_reasons: list[ToptCoreReasonCode] = []
    if snapshot.headcount.value <= 0:
        invalid_reasons.append(ToptCoreReasonCode.NONPOSITIVE_HEADCOUNT)
    if invalid_reasons:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=tuple(invalid_reasons),
            freshness=freshness,
        )

    confidence = min(cell.confidence for cell in snapshot.cell_inputs)
    assert snapshot.total_assets is not None and snapshot.total_assets.value is not None
    # Uniform capital-adjusted formula for every issuer (#394 convergence onto the
    # base large_model_value_v0 definition): the numerator is the parser's
    # industry-branch profit -- pre-provision profit for financials, gross profit
    # otherwise -- and the same total_assets * risk_free_rate capital charge is
    # subtracted for both. No financial short-circuit; financials flow through the
    # same tier/P-S path.
    if snapshot.operating_branch is OperatingBranch.FINANCIAL:
        assert snapshot.pre_provision_profit is not None and snapshot.pre_provision_profit.value is not None
        numerator = snapshot.pre_provision_profit.value
    else:
        assert snapshot.gross_profit is not None and snapshot.gross_profit.value is not None
        numerator = snapshot.gross_profit.value
    with localcontext(_DECIMAL_CONTEXT):
        capital_adjusted = numerator - (snapshot.total_assets.value * gppe_definition.risk_free_rate)
        gppe = capital_adjusted / snapshot.headcount.value
    return ToptGppeResult(
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
        confidence=confidence,
        freshness=freshness,
        input_observation_ids=snapshot.observation_ids,
        gppe_definition_id=gppe_definition.definition_id,
        gppe_definition_sha256=gppe_definition.content_sha256,
    )


def _validate_gppe_lineage(snapshot: ToptCoreSnapshotInput, gppe_result: ToptGppeResult) -> None:
    expected = (
        snapshot.snapshot_id,
        snapshot.run_id,
        snapshot.release_manifest_id,
        snapshot.universe_id,
        snapshot.universe_version,
        snapshot.universe_sha256,
        snapshot.cutoff,
        snapshot.issuer_id,
        snapshot.instrument_id,
        snapshot.listing_id,
        snapshot.observation_ids,
    )
    actual = (
        gppe_result.snapshot_id,
        gppe_result.run_id,
        gppe_result.release_manifest_id,
        gppe_result.universe_id,
        gppe_result.universe_version,
        gppe_result.universe_sha256,
        gppe_result.cutoff,
        gppe_result.issuer_id,
        gppe_result.instrument_id,
        gppe_result.listing_id,
        gppe_result.input_observation_ids,
    )
    if actual != expected:
        raise ValueError("tier composite GPPE input does not match its exact snapshot member")


def _unavailable_core(
    snapshot: ToptCoreSnapshotInput,
    gppe_result: ToptGppeResult,
    *,
    invocation_id: str,
    tier_definition: ThreeTierV0Definition,
    reasons: tuple[ToptCoreReasonCode, ...],
    operating_efficiency: Decimal | None = None,
) -> ToptCoreResult:
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
        operating_branch=gppe_result.operating_branch,
        operating_metric=gppe_result.operating_metric,
        availability=ToptCoreAvailability.UNAVAILABLE,
        operating_efficiency=operating_efficiency,
        confidence=gppe_result.confidence,
        freshness=gppe_result.freshness,
        reason_codes=reasons,
        input_observation_ids=snapshot.observation_ids,
        gppe_invocation_id=gppe_result.invocation_id,
        gppe_result_id=gppe_result.result_id,
        gppe_definition_id=gppe_result.gppe_definition_id,
        gppe_definition_sha256=gppe_result.gppe_definition_sha256,
        tier_definition_id=tier_definition.definition_id,
        tier_definition_sha256=tier_definition.content_sha256,
    )


def compute_topt_core(
    snapshot: ToptCoreSnapshotInput,
    gppe_result: ToptGppeResult,
    *,
    invocation_id: str,
    tier_definition: ThreeTierV0Definition,
) -> ToptCoreResult:
    """Compute module 7 from an exact materialized module-2 result."""

    if not invocation_id.startswith("topt-core-invocation:"):
        raise ValueError("invocation_id must be a content-addressed TOPT core invocation")
    _validate_gppe_lineage(snapshot, gppe_result)
    if gppe_result.availability is ToptCoreAvailability.UNAVAILABLE:
        return _unavailable_core(
            snapshot,
            gppe_result,
            invocation_id=invocation_id,
            tier_definition=tier_definition,
            reasons=gppe_result.reason_codes,
        )
    reasons: list[ToptCoreReasonCode] = []
    if snapshot.revenue is None or snapshot.revenue.value is None:
        reasons.append(ToptCoreReasonCode.MISSING_REVENUE)
    elif snapshot.revenue.value <= 0:
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_REVENUE)
    share_values = tuple(component.shares_outstanding.value for component in snapshot.market_value_components)
    if any(value is None for value in share_values):
        reasons.append(ToptCoreReasonCode.MISSING_SHARES_OUTSTANDING)
    elif any(value <= 0 for value in share_values if value is not None):
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_SHARES_OUTSTANDING)
    price_values = tuple(component.market_price.value for component in snapshot.market_value_components)
    if any(value is None for value in price_values):
        reasons.append(ToptCoreReasonCode.MISSING_MARKET_PRICE)
    elif any(value <= 0 for value in price_values if value is not None):
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_MARKET_PRICE)
    if reasons:
        return _unavailable_core(
            snapshot,
            gppe_result,
            invocation_id=invocation_id,
            tier_definition=tier_definition,
            reasons=tuple(reasons),
        )

    assert gppe_result.gppe is not None and gppe_result.capital_adjusted_gross_profit is not None
    assert snapshot.revenue is not None and snapshot.revenue.value is not None
    with localcontext(_DECIMAL_CONTEXT):
        market_cap = sum(
            (
                component.market_price.value * component.shares_outstanding.value
                for component in snapshot.market_value_components
                if component.market_price.value is not None and component.shares_outstanding.value is not None
            ),
            start=Decimal("0"),
        )
        current_ps = market_cap / snapshot.revenue.value
        band = _tier_band(gppe_result.gppe, tier_definition)
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
        operating_branch=gppe_result.operating_branch,
        operating_metric=gppe_result.operating_metric,
        availability=ToptCoreAvailability.AVAILABLE,
        operating_efficiency=gppe_result.operating_efficiency,
        capital_adjusted_gross_profit=gppe_result.capital_adjusted_gross_profit,
        gppe=gppe_result.gppe,
        tier=band.tier,
        target_ps_lower=band.target_ps_lower,
        target_ps_upper=band.target_ps_upper,
        target_ps_midpoint=midpoint,
        current_ps=current_ps,
        valuation_gap=valuation_gap,
        confidence=min(gppe_result.confidence, *(cell.confidence for cell in snapshot.cell_inputs)),
        freshness=gppe_result.freshness,
        input_observation_ids=snapshot.observation_ids,
        gppe_invocation_id=gppe_result.invocation_id,
        gppe_result_id=gppe_result.result_id,
        gppe_definition_id=gppe_result.gppe_definition_id,
        gppe_definition_sha256=gppe_result.gppe_definition_sha256,
        tier_definition_id=tier_definition.definition_id,
        tier_definition_sha256=tier_definition.content_sha256,
    )
