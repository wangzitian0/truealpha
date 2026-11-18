"""Snapshot input contracts and factor definition pins (#1235, #1518, #1730)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)
from truealpha_contracts.common import (
    ContentHash,
    FrozenModel,
    IdentifiedFrozenModel,
    OmitNoneFields,
    Sha256,
    content_id,
    content_ref,
    require_aware,
)
from truealpha_contracts.strategy import LaborEfficiencyTreePin, LargeModelValueV0Definition

from factors.panel import definition_pins, snapshot_field_specs

_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_CONTENT_ID_PATTERN = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_OBSERVATION_ID_PATTERN = re.compile(r"^normalized-observation:[0-9a-f]{64}$")


def _aware_utc(value: datetime, info: ValidationInfo) -> datetime:
    return require_aware(value, info.field_name or "datetime").astimezone(UTC)


def _decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    return value


def _normalize_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal("0")
    with localcontext(Context(prec=max(_DECIMAL_CONTEXT.prec, len(value.as_tuple().digits)))):
        return value.normalize()


#: A timezone-aware datetime, normalized to UTC.
_UtcDatetime = Annotated[datetime, AfterValidator(_aware_utc)]
#: A Decimal that refuses binary float input; finiteness is checked where the value is normalized.
_FloatFreeDecimal = Annotated[Decimal, BeforeValidator(_decimal_input)]


def _normalize_decimal_fields(model: Any, field_names: Sequence[str], error_label: str) -> None:
    for name in field_names:
        value = getattr(model, name)
        if value is not None:
            if not value.is_finite():
                raise ValueError(f"{error_label} decimals must be finite")
            object.__setattr__(model, name, _normalize_decimal(value))


class MetricAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class MetricFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class OperatingBranch(StrEnum):
    NON_FINANCIAL = "non_financial"
    FINANCIAL = "financial"
    INSURANCE = "insurance"


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


class ToptMetricInput(OmitNoneFields, FrozenModel):
    omit_none_fields = ("unavailable_reason",)
    input_id: str = content_ref("normalized-observation")
    metric: str = Field(min_length=1)
    value: _FloatFreeDecimal | None = None
    confidence: _FloatFreeDecimal = Field(ge=0, le=1)
    knowable_at: _UtcDatetime
    freshness: MetricFreshness
    availability: MetricAvailability
    unavailable_reason: ToptCoreReasonCode | None = None

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if (self.availability is MetricAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("metric value must exist exactly when the input is available")
        if self.unavailable_reason is not None and self.availability is MetricAvailability.AVAILABLE:
            raise ValueError("an available metric cannot carry an unavailable reason")
        _normalize_decimal_fields(self, ("value",), "metric value")
        return self


class ToptCellQualityInput(FrozenModel):
    input_id: str = content_ref("normalized-observation")
    confidence: _FloatFreeDecimal = Field(ge=0, le=1)
    knowable_at: _UtcDatetime
    freshness: MetricFreshness


class ToptMarketValueComponent(FrozenModel):
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


class GppeV0Definition(OmitNoneFields, IdentifiedFrozenModel):
    id_field = "definition_id"
    id_prefix = "gppe-definition"
    omit_none_fields = ("labor_efficiency_trees",)
    definition_id: str = content_id("gppe-definition")
    content_sha256: ContentHash = ""
    factor_id: Literal["gross_profit_per_employee"] = "gross_profit_per_employee"
    factor_version: Literal["production-topt-v0.2.0"] = "production-topt-v0.2.0"
    formula: Literal["(numerator-total_assets*risk_free_rate)/headcount"] = (
        "(numerator-total_assets*risk_free_rate)/headcount"
    )
    risk_free_benchmark: Literal["3m-us-tbill"] = "3m-us-tbill"
    risk_free_rate: Annotated[_FloatFreeDecimal, AfterValidator(_normalize_decimal)] = Field(ge=0, le=1)
    output_unit: Literal["reporting_currency_per_employee"] = "reporting_currency_per_employee"
    confidence_rule: Literal["minimum_consumed_confidence"] = "minimum_consumed_confidence"
    stale_input_behavior: Literal["unavailable"] = "unavailable"
    labor_efficiency_trees: dict[str, LaborEfficiencyTreePin] | None = None

    def pinned_by(self, definition: LargeModelValueV0Definition) -> Self:
        return type(self)(risk_free_rate=self.risk_free_rate, labor_efficiency_trees=dict(definition_pins(definition)))


class ToptCoreSnapshotInput(FrozenModel):
    snapshot_id: str = content_ref("topt-core-snapshot")
    run_id: str = content_ref("capture-run")
    release_manifest_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    universe_id: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    universe_sha256: Sha256
    cutoff: _UtcDatetime
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


__all__ = [
    "OPTIONAL_SNAPSHOT_FIELDS",
    "SHARE_BASIS_REASON_CODES",
    "TCE_SNAPSHOT_FIELDS",
    "GppeV0Definition",
    "MetricAvailability",
    "MetricFreshness",
    "OperatingBranch",
    "ToptCellQualityInput",
    "ToptCoreReasonCode",
    "ToptCoreSnapshotInput",
    "ToptMarketValueComponent",
    "ToptMetricInput",
    "drop_absent_banking_leaves",
]
