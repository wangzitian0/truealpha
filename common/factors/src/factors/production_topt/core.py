"""Pure, versioned GPPE v0 and three-tier computation for Production TOPT (#1235, #1518)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Any, Literal, Self

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
from truealpha_contracts.measure import Measure, PerUnit, Units
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy import LaborEfficiencyTreePin, LargeModelValueV0Definition, ValuationBasis

from factors.measure import DEFAULT_REPORTING_CURRENCY, times
from factors.panel import (
    PANEL_METRICS,
    AliasKind,
    IssuerClass,
    UnpinnedClassError,
    bound_tree,
    capital_adjusted_node,
    definition_pins,
    evaluate_panel_variant,
    operating_numerator_node,
    required_inputs,
    snapshot_field_specs,
)

_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_ID_PATTERN = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_OBSERVATION_ID_PATTERN = re.compile(r"^normalized-observation:[0-9a-f]{64}$")
_SNAPSHOT_ENVELOPE_KEYS = (
    "snapshot_id",
    "run_id",
    "release_manifest_id",
    "universe_id",
    "universe_version",
    "universe_sha256",
    "cutoff",
    "issuer_id",
    "instrument_id",
    "listing_id",
)


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


def _normalize_decimal_fields(model: Any, field_names: Sequence[str], error_label: str) -> None:
    for name in field_names:
        value = getattr(model, name)
        if value is not None:
            if not value.is_finite():
                raise ValueError(f"{error_label} decimals must be finite")
            object.__setattr__(model, name, _normalize_decimal(value))


def _snapshot_envelope(snapshot: ToptCoreSnapshotInput) -> dict[str, Any]:
    return {key: getattr(snapshot, key) for key in _SNAPSHOT_ENVELOPE_KEYS}


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
    INSURANCE = "insurance"


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
    LABOR_EFFICIENCY_TREE_UNPINNED = "labor_efficiency_tree_unpinned"
    NEGATIVE_TANGIBLE_COMMON_EQUITY = "negative_tangible_common_equity"
    MISSING_STOCKHOLDERS_EQUITY = "missing_stockholders_equity"
    MISSING_PREFERRED_STOCK_VALUE = "missing_preferred_stock_value"
    MISSING_GOODWILL = "missing_goodwill"
    MISSING_INTANGIBLE_ASSETS_NET_EXCLUDING_GOODWILL = "missing_intangible_assets_net_excluding_goodwill"
    MISSING_BANK_NET_REVENUE = "missing_bank_net_revenue"
    MISSING_FINANCIAL_ASSETS = "missing_financial_assets"
    MISSING_FINANCIAL_RETURNS = "missing_financial_returns"
    NONPOSITIVE_HEADCOUNT = "nonpositive_headcount"
    NONPOSITIVE_REVENUE = "nonpositive_revenue"
    NONPOSITIVE_SHARES_OUTSTANDING = "nonpositive_shares_outstanding"
    NONPOSITIVE_MARKET_PRICE = "nonpositive_market_price"
    STALE_INPUT = "stale_input"
    STALE_PERIOD = "stale_period"
    UNKNOWN_FRESHNESS = "unknown_freshness"
    NONPOSITIVE_GROSS_PROFIT = "nonpositive_gross_profit"
    NO_SPLIT_ADJUSTED_SERIES = "no_split_adjusted_series"
    NO_UNADJUSTED_SERIES = "no_unadjusted_series"
    SPLIT_DERIVATION_FAILED = "split_derivation_failed"
    SPLIT_SERIES_DO_NOT_COVER_THE_WINDOW = "split_series_do_not_cover_the_window"
    LISTINGS_DISAGREE_ON_SPLITS = "listings_disagree_on_splits"
    SHARE_COUNT_UNDATED = "share_count_undated"
    SHARE_BASIS_AMBIGUOUS = "share_basis_ambiguous"
    CLOSE_JUMP_BEYOND_SERIES = "close_jump_beyond_series"
    MISSING_LISTING_UNIT_RATIO = "missing_listing_unit_ratio"
    MISSING_CLASS_RATIO = "missing_class_ratio"
    CURRENCY_MISMATCH = "currency_mismatch"
    PRIMARY_LISTING_UNDECLARED = "primary_listing_undeclared"


SHARE_BASIS_REASON_CODES = frozenset(
    {
        ToptCoreReasonCode.NO_SPLIT_ADJUSTED_SERIES,
        ToptCoreReasonCode.NO_UNADJUSTED_SERIES,
        ToptCoreReasonCode.SPLIT_DERIVATION_FAILED,
        ToptCoreReasonCode.SPLIT_SERIES_DO_NOT_COVER_THE_WINDOW,
        ToptCoreReasonCode.LISTINGS_DISAGREE_ON_SPLITS,
        ToptCoreReasonCode.SHARE_COUNT_UNDATED,
        ToptCoreReasonCode.SHARE_BASIS_AMBIGUOUS,
        ToptCoreReasonCode.CLOSE_JUMP_BEYOND_SERIES,
        ToptCoreReasonCode.MISSING_LISTING_UNIT_RATIO,
        ToptCoreReasonCode.MISSING_CLASS_RATIO,
    }
)

_BASE_SNAPSHOT_FIELDS = frozenset({"gross_profit", "total_assets", "headcount", "revenue", "pre_provision_profit"})
TCE_SNAPSHOT_FIELDS = (
    "stockholders_equity",
    "preferred_stock_value",
    "goodwill",
    "intangible_assets_net_excluding_goodwill",
)
OPTIONAL_SNAPSHOT_FIELDS: tuple[str, ...] = tuple(
    field for field in snapshot_field_specs() if field not in _BASE_SNAPSHOT_FIELDS
)


def drop_absent_banking_leaves(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in OPTIONAL_SNAPSHOT_FIELDS or value is not None}


class ToptMetricInput(_FrozenModel):
    input_id: str = Field(pattern=r"^normalized-observation:[0-9a-f]{64}$")
    metric: str = Field(min_length=1)
    value: Decimal | None = None
    confidence: Decimal = Field(ge=0, le=1)
    knowable_at: datetime
    freshness: MetricFreshness
    availability: MetricAvailability
    unavailable_reason: ToptCoreReasonCode | None = None

    @model_serializer(mode="wrap")
    def omit_absent_reason(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if data.get("unavailable_reason") is None:
            data.pop("unavailable_reason", None)
        return data

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
        if self.unavailable_reason is not None and self.availability is MetricAvailability.AVAILABLE:
            raise ValueError("an available metric cannot carry an unavailable reason")
        _normalize_decimal_fields(self, ("value",), "metric value")
        return self


class ToptCellQualityInput(_FrozenModel):
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
    labor_efficiency_trees: dict[str, LaborEfficiencyTreePin] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_trees(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.labor_efficiency_trees is None:
            data.pop("labor_efficiency_trees", None)
        return data

    def pinned_by(self, definition: LargeModelValueV0Definition) -> Self:
        return type(self)(risk_free_rate=self.risk_free_rate, labor_efficiency_trees=dict(definition_pins(definition)))

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


class ToptCoreSnapshotInput(_FrozenModel):
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
    primary_listing_undeclared: bool = False
    stockholders_equity: ToptMetricInput | None = None
    preferred_stock_value: ToptMetricInput | None = None
    goodwill: ToptMetricInput | None = None
    intangible_assets_net_excluding_goodwill: ToptMetricInput | None = None
    financial_assets: ToptMetricInput | None = None
    financial_returns: ToptMetricInput | None = None
    bank_net_revenue: ToptMetricInput | None = None
    inputs: Mapping[str, ToptMetricInput] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def omit_absent_optional_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.primary_listing_undeclared:
            data.pop("primary_listing_undeclared", None)
        known_fields = {*_BASE_SNAPSHOT_FIELDS, *OPTIONAL_SNAPSHOT_FIELDS}
        extra_inputs = {k: v for k, v in (data.get("inputs") or {}).items() if k not in known_fields}
        if extra_inputs:
            data["inputs"] = extra_inputs
        else:
            data.pop("inputs", None)
        return drop_absent_banking_leaves(data)

    def metric_input(self, key: str) -> ToptMetricInput | None:
        if key == "employees_total" and self.headcount is not None:
            return self.headcount
        attr = getattr(self, key, None)
        return attr if isinstance(attr, ToptMetricInput) else self.inputs.get(key)

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

    def _check_selected_metric(self, metric: ToptMetricInput, missing_msg: str, future_msg: str) -> None:
        if metric.input_id not in self.observation_ids:
            raise ValueError(missing_msg)
        if metric.knowable_at > self.cutoff:
            raise ValueError(future_msg)

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        expected_metrics: dict[str, ToptMetricInput | None] = {
            **{name: getattr(self, name) for name in (*_BASE_SNAPSHOT_FIELDS, *OPTIONAL_SNAPSHOT_FIELDS)},
        }
        merged_inputs = dict(self.inputs)
        for key, metric in expected_metrics.items():
            if metric is not None:
                merged_inputs.setdefault(key, metric)
            elif key in merged_inputs:
                object.__setattr__(self, key, merged_inputs[key])
                expected_metrics[key] = merged_inputs[key]
        for key, metric in merged_inputs.items():
            expected_metrics.setdefault(key, metric)
        object.__setattr__(self, "inputs", merged_inputs)
        for expected_metric, metric in expected_metrics.items():
            if metric is None:
                continue
            if metric.metric != expected_metric:
                raise ValueError(f"{expected_metric} input carries the wrong metric identity")
            self._check_selected_metric(
                metric,
                f"{expected_metric} input is not selected into the snapshot",
                f"{expected_metric} input is future-dated",
            )
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
                self._check_selected_metric(
                    metric,
                    "market-value component is not selected into the snapshot",
                    "market-value component is future-dated",
                )
        return self


_GPPE_NUMERIC_FIELDS = ("operating_efficiency", "capital_adjusted_gross_profit", "gppe")
_CORE_NUMERIC_FIELDS = (
    "capital_adjusted_gross_profit",
    "operating_efficiency",
    "gppe",
    "target_ps_lower",
    "target_ps_upper",
    "target_ps_midpoint",
    "current_ps",
    "current_pgp",
    "target_pgp_lower",
    "target_pgp_upper",
    "target_pgp_midpoint",
    "valuation_gap",
)


class ToptGppeResult(_FrozenModel):
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

    @field_validator(*_GPPE_NUMERIC_FIELDS, "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        vals = tuple(getattr(self, name) for name in _GPPE_NUMERIC_FIELDS)
        if self.availability is ToptCoreAvailability.AVAILABLE:
            if (
                self.operating_metric is not OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
                or any(v is None for v in vals)
                or self.reason_codes
            ):
                raise ValueError("available GPPE result carries invalid values")
        elif any(v is not None for v in vals) or not self.reason_codes:
            raise ValueError("unavailable GPPE result carries computed values")
        _normalize_decimal_fields(self, _GPPE_NUMERIC_FIELDS, "GPPE result")
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
    current_pgp: Decimal | None = None
    target_pgp_lower: Decimal | None = None
    target_pgp_upper: Decimal | None = None
    target_pgp_midpoint: Decimal | None = None
    valuation_gap: Decimal | None = None
    confidence: Decimal = Field(ge=0, le=1)
    freshness: MetricFreshness
    reason_codes: tuple[ToptCoreReasonCode, ...] = ()
    input_observation_ids: tuple[str, ...]
    gppe_invocation_id: str = Field(pattern=r"^topt-gppe-invocation:[0-9a-f]{64}$")
    gppe_result_id: str = Field(pattern=r"^topt-gppe-result:[0-9a-f]{64}$")
    gppe_definition_id: str = Field(pattern=r"^gppe-definition:[0-9a-f]{64}$")
    gppe_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    tier_definition_id: str = Field(pattern=r"^strategy-definition:[0-9a-f]{64}$")
    tier_definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("cutoff")
    @classmethod
    def normalize_cutoff(cls, value: datetime) -> datetime:
        return _aware_utc(value, "cutoff")

    @field_validator(*_CORE_NUMERIC_FIELDS, "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        vals = tuple(getattr(self, name) for name in _CORE_NUMERIC_FIELDS)
        if self.availability is ToptCoreAvailability.AVAILABLE:
            if (
                self.operating_metric is not OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
                or any(v is None for v in vals)
                or self.tier is None
                or self.reason_codes
            ):
                raise ValueError("available TOPT core result requires all values and no reason codes")
        elif any(v is not None for v in vals) or self.tier is not None or not self.reason_codes:
            raise ValueError("unavailable TOPT core result carries invalid computed values")
        _normalize_decimal_fields(self, _CORE_NUMERIC_FIELDS, "TOPT core result")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes), key=str)))
        _identify(self, id_field="result_id", prefix="topt-core-result")
        return self


def _operating_numerator(
    snapshot: ToptCoreSnapshotInput, definition: LargeModelValueV0Definition
) -> tuple[str, ToptMetricInput | None]:
    issuer_class = IssuerClass(snapshot.operating_branch.value)
    node = operating_numerator_node(bound_tree(definition, issuer_class), issuer_class)
    field = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
    if field is None:
        raise AssertionError(f"GPPE numerator {node.key} names no snapshot field")
    return field, snapshot.metric_input(field)


def _snapshot_freshness(snapshot: ToptCoreSnapshotInput) -> MetricFreshness:
    if any(cell.freshness is MetricFreshness.STALE for cell in snapshot.cell_inputs):
        return MetricFreshness.STALE
    if any(cell.freshness is MetricFreshness.UNKNOWN for cell in snapshot.cell_inputs):
        return MetricFreshness.UNKNOWN
    return MetricFreshness.FRESH


def _missing_metric_reason(metric: ToptMetricInput | None, default_field: str) -> ToptCoreReasonCode:
    if metric is not None and metric.unavailable_reason is not None:
        return metric.unavailable_reason
    return ToptCoreReasonCode(f"missing_{default_field}")


def _component_metric_reasons(
    metrics: Sequence[ToptMetricInput],
    *,
    missing_code: ToptCoreReasonCode,
    nonpositive_code: ToptCoreReasonCode,
) -> list[ToptCoreReasonCode]:
    dropped = [metric for metric in metrics if metric.value is None]
    if dropped:
        causes = {metric.unavailable_reason for metric in dropped}
        reasons = [cause for cause in causes if cause is not None]
        if None in causes:
            reasons.append(missing_code)
        return reasons
    if any(metric.value is not None and metric.value <= 0 for metric in metrics):
        return [nonpositive_code]
    return []


def _unavailable_gppe(
    snapshot: ToptCoreSnapshotInput,
    *,
    invocation_id: str,
    gppe_definition: GppeV0Definition,
    reasons: tuple[ToptCoreReasonCode, ...],
    freshness: MetricFreshness,
) -> ToptGppeResult:
    return ToptGppeResult(
        invocation_id=invocation_id,
        **_snapshot_envelope(snapshot),
        operating_branch=snapshot.operating_branch,
        operating_metric=OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE,
        availability=ToptCoreAvailability.UNAVAILABLE,
        confidence=Decimal("0"),
        freshness=freshness,
        reason_codes=reasons,
        input_observation_ids=snapshot.observation_ids,
        gppe_definition_id=gppe_definition.definition_id,
        gppe_definition_sha256=gppe_definition.content_sha256,
    )


def _collect_gppe_tree_inputs(
    snapshot: ToptCoreSnapshotInput,
    gppe_definition: GppeV0Definition,
    tree: Any,
    issuer_class: IssuerClass,
) -> tuple[dict[str, Decimal | None], tuple[ToptCoreReasonCode, ...], list[Decimal]]:
    inputs: dict[str, Decimal | None] = {}
    reasons_list: list[ToptCoreReasonCode] = []
    consumed_confidences: list[Decimal] = []
    for node in required_inputs(tree, issuer_class, metrics=PANEL_METRICS):
        parameter = node.alias(AliasKind.DEFINITION_PARAMETER)
        if parameter is not None:
            inputs[node.key] = getattr(gppe_definition, parameter)
            continue
        field = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
        if field is None:
            raise AssertionError(f"GPPE tree input {node.key} names no snapshot field")
        metric = snapshot.metric_input(field) or snapshot.metric_input(node.key)
        if metric is None or metric.availability is MetricAvailability.UNAVAILABLE or metric.value is None:
            if node.default_when_missing is not None:
                inputs[node.key] = None
            else:
                reasons_list.append(_missing_metric_reason(metric, field))
            continue
        inputs[node.key] = metric.value
        consumed_confidences.append(metric.confidence)
    return inputs, tuple(reasons_list), consumed_confidences


def compute_topt_gppe(
    snapshot: ToptCoreSnapshotInput,
    *,
    invocation_id: str,
    gppe_definition: GppeV0Definition,
    definition: LargeModelValueV0Definition,
) -> ToptGppeResult:
    if not invocation_id.startswith("topt-gppe-invocation:"):
        raise ValueError("invocation_id must be a content-addressed TOPT GPPE invocation")
    freshness = _snapshot_freshness(snapshot)
    if snapshot.primary_listing_undeclared:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=(ToptCoreReasonCode.PRIMARY_LISTING_UNDECLARED,),
            freshness=freshness,
        )
    issuer_class = IssuerClass(snapshot.operating_branch.value)
    if gppe_definition.labor_efficiency_trees not in (None, definition_pins(definition)):
        raise ValueError("the GPPE definition pins other trees than the strategy definition")
    try:
        tree = bound_tree(definition, issuer_class)
    except UnpinnedClassError:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=(ToptCoreReasonCode.LABOR_EFFICIENCY_TREE_UNPINNED,),
            freshness=freshness,
        )
    inputs, reasons, consumed_confidences = _collect_gppe_tree_inputs(snapshot, gppe_definition, tree, issuer_class)
    if freshness is MetricFreshness.STALE:
        reasons = (*reasons, ToptCoreReasonCode.STALE_INPUT)
    elif freshness is MetricFreshness.UNKNOWN:
        reasons = (*reasons, ToptCoreReasonCode.UNKNOWN_FRESHNESS)
    if reasons:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=reasons,
            freshness=freshness,
        )
    assert snapshot.headcount is not None and snapshot.headcount.value is not None
    if snapshot.headcount.value <= 0:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=(ToptCoreReasonCode.NONPOSITIVE_HEADCOUNT,),
            freshness=freshness,
        )

    evaluation = evaluate_panel_variant(tree, issuer_class=issuer_class, inputs=inputs, metrics=PANEL_METRICS)
    if "negative_operand" in evaluation.undefined.values():
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=(ToptCoreReasonCode.NEGATIVE_TANGIBLE_COMMON_EQUITY,),
            freshness=freshness,
        )
    capital_adjusted = evaluation.values.get(capital_adjusted_node(tree, issuer_class, metrics=PANEL_METRICS))
    gppe = evaluation.values.get(tree.root)
    assert capital_adjusted is not None and gppe is not None and not evaluation.undefined
    return ToptGppeResult(
        invocation_id=invocation_id,
        **_snapshot_envelope(snapshot),
        operating_branch=snapshot.operating_branch,
        operating_metric=OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE,
        availability=ToptCoreAvailability.AVAILABLE,
        operating_efficiency=gppe,
        capital_adjusted_gross_profit=capital_adjusted,
        gppe=gppe,
        confidence=min(consumed_confidences),
        freshness=freshness,
        input_observation_ids=snapshot.observation_ids,
        gppe_definition_id=gppe_definition.definition_id,
        gppe_definition_sha256=gppe_definition.content_sha256,
    )


def _validate_gppe_lineage(snapshot: ToptCoreSnapshotInput, gppe_result: ToptGppeResult) -> None:
    expected = (*(getattr(snapshot, key) for key in _SNAPSHOT_ENVELOPE_KEYS), snapshot.observation_ids)
    actual = (*(getattr(gppe_result, key) for key in _SNAPSHOT_ENVELOPE_KEYS), gppe_result.input_observation_ids)
    if actual != expected:
        raise ValueError("tier composite GPPE input does not match its exact snapshot member")


def _unavailable_core(
    snapshot: ToptCoreSnapshotInput,
    gppe_result: ToptGppeResult,
    *,
    invocation_id: str,
    definition: LargeModelValueV0Definition,
    reasons: tuple[ToptCoreReasonCode, ...],
) -> ToptCoreResult:
    return ToptCoreResult(
        invocation_id=invocation_id,
        **_snapshot_envelope(snapshot),
        operating_branch=gppe_result.operating_branch,
        operating_metric=gppe_result.operating_metric,
        availability=ToptCoreAvailability.UNAVAILABLE,
        confidence=gppe_result.confidence,
        freshness=gppe_result.freshness,
        reason_codes=reasons,
        input_observation_ids=snapshot.observation_ids,
        gppe_invocation_id=gppe_result.invocation_id,
        gppe_result_id=gppe_result.result_id,
        gppe_definition_id=gppe_result.gppe_definition_id,
        gppe_definition_sha256=gppe_result.gppe_definition_sha256,
        tier_definition_id=definition.strategy_definition_id,
        tier_definition_sha256=definition.content_sha256,
    )


def _component_market_value(
    component: ToptMarketValueComponent,
    *,
    currency: str = DEFAULT_REPORTING_CURRENCY,
) -> Decimal:
    assert component.market_price.value is not None and component.shares_outstanding.value is not None
    price_measure = Measure(
        value=component.market_price.value,
        unit=PerUnit(currency=currency, instrument_id=component.instrument_id),
        knowable_at=component.market_price.knowable_at,
        subject=component.listing_id,
    )
    shares_measure = Measure(
        value=component.shares_outstanding.value,
        unit=Units(instrument_id=component.instrument_id),
        knowable_at=component.shares_outstanding.knowable_at,
        subject=component.instrument_id,
    )
    return times(price_measure, shares_measure).value


def compute_topt_core(
    snapshot: ToptCoreSnapshotInput,
    gppe_result: ToptGppeResult,
    *,
    invocation_id: str,
    definition: LargeModelValueV0Definition,
) -> ToptCoreResult:
    if not invocation_id.startswith("topt-core-invocation:"):
        raise ValueError("invocation_id must be a content-addressed TOPT core invocation")
    if definition.valuation_basis is not ValuationBasis.PRICE_TO_GROSS_PROFIT:
        raise ValueError(
            f"the TOPT core engine values by P/GP; definition {definition.definition_version} "
            f"values by {definition.valuation_basis.value}"
        )
    _validate_gppe_lineage(snapshot, gppe_result)
    if gppe_result.availability is ToptCoreAvailability.UNAVAILABLE:
        return _unavailable_core(
            snapshot, gppe_result, invocation_id=invocation_id, definition=definition, reasons=gppe_result.reason_codes
        )

    reasons: list[ToptCoreReasonCode] = []
    if snapshot.revenue is None or snapshot.revenue.value is None:
        reasons.append(_missing_metric_reason(snapshot.revenue, "revenue"))
    elif snapshot.revenue.value <= 0:
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_REVENUE)
    numerator_field, numerator = _operating_numerator(snapshot, definition)
    if numerator is None or numerator.value is None:
        reasons.append(_missing_metric_reason(numerator, numerator_field))
    elif numerator.value <= 0:
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_GROSS_PROFIT)
    reasons.extend(
        _component_metric_reasons(
            [c.shares_outstanding for c in snapshot.market_value_components],
            missing_code=ToptCoreReasonCode.MISSING_SHARES_OUTSTANDING,
            nonpositive_code=ToptCoreReasonCode.NONPOSITIVE_SHARES_OUTSTANDING,
        )
    )
    reasons.extend(
        _component_metric_reasons(
            [c.market_price for c in snapshot.market_value_components],
            missing_code=ToptCoreReasonCode.MISSING_MARKET_PRICE,
            nonpositive_code=ToptCoreReasonCode.NONPOSITIVE_MARKET_PRICE,
        )
    )
    if reasons:
        return _unavailable_core(
            snapshot, gppe_result, invocation_id=invocation_id, definition=definition, reasons=tuple(reasons)
        )

    assert gppe_result.gppe is not None and gppe_result.capital_adjusted_gross_profit is not None
    assert snapshot.revenue is not None and snapshot.revenue.value is not None
    assert numerator is not None and numerator.value is not None
    with localcontext(_DECIMAL_CONTEXT):
        share_counts = {c.shares_outstanding.value for c in snapshot.market_value_components}
        if len(snapshot.market_value_components) > 1 and len(share_counts) == 1:
            execution = next(c for c in snapshot.market_value_components if c.listing_id == snapshot.listing_id)
            market_cap = _component_market_value(execution)
        else:
            market_cap = sum(
                (
                    _component_market_value(c)
                    for c in snapshot.market_value_components
                    if c.market_price.value is not None and c.shares_outstanding.value is not None
                ),
                start=Decimal("0"),
            )
        current_ps = market_cap / snapshot.revenue.value
        current_pgp = market_cap / numerator.value
        band = definition.tier_valuation.band_for(gppe_result.gppe)
        target_pgp_lower = band.target_lower_bound
        target_pgp_upper = band.target_upper_bound
        target_pgp_midpoint = (target_pgp_lower + target_pgp_upper) / Decimal("2")
        valuation_gap = target_pgp_midpoint / current_pgp - Decimal("1")
        margin = numerator.value / snapshot.revenue.value
        target_ps_lower = target_pgp_lower * margin
        target_ps_upper = target_pgp_upper * margin
        target_ps_midpoint = target_pgp_midpoint * margin
    return ToptCoreResult(
        invocation_id=invocation_id,
        **_snapshot_envelope(snapshot),
        operating_branch=gppe_result.operating_branch,
        operating_metric=gppe_result.operating_metric,
        availability=ToptCoreAvailability.AVAILABLE,
        operating_efficiency=gppe_result.operating_efficiency,
        capital_adjusted_gross_profit=gppe_result.capital_adjusted_gross_profit,
        gppe=gppe_result.gppe,
        tier=band.tier,
        target_ps_lower=target_ps_lower,
        target_ps_upper=target_ps_upper,
        target_ps_midpoint=target_ps_midpoint,
        current_ps=current_ps,
        current_pgp=current_pgp,
        target_pgp_lower=target_pgp_lower,
        target_pgp_upper=target_pgp_upper,
        target_pgp_midpoint=target_pgp_midpoint,
        valuation_gap=valuation_gap,
        confidence=min(
            gppe_result.confidence,
            numerator.confidence,
            *(c.market_price.confidence for c in snapshot.market_value_components),
            *(c.shares_outstanding.confidence for c in snapshot.market_value_components),
            *(cell.confidence for cell in snapshot.cell_inputs),
        ),
        freshness=gppe_result.freshness,
        input_observation_ids=snapshot.observation_ids,
        gppe_invocation_id=gppe_result.invocation_id,
        gppe_result_id=gppe_result.result_id,
        gppe_definition_id=gppe_result.gppe_definition_id,
        gppe_definition_sha256=gppe_result.gppe_definition_sha256,
        tier_definition_id=definition.strategy_definition_id,
        tier_definition_sha256=definition.content_sha256,
    )
