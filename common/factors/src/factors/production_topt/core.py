"""Pure, versioned GPPE v0 and three-tier computation for Production TOPT.

**One band set (#1235).** The tier bands are not written here. `compute_topt_core` reads
them from the strategy definition that the deployed strategy tick also evaluates
(`truealpha_contracts.strategy.load_live_strategy_definition`, definition 1.1.0). The
engine tiers by capital-adjusted GPPE and values by P/GP = market value / the operating
numerator that GPPE consumes. Each row stamps that definition's id and content hash as
`tier_definition_id` / `tier_definition_sha256`, the same hash that
`mart.strategy_runs.definition_content_sha256` carries.

**One tree per class, pinned by the definition (#394, #1518).** GPPE is the tree of the metric forest
that the strategy definition pins for the issuer class (`factors.forest.bound_tree`). A non-financial
issuer or an insurer takes the uniform tree `gppe` (`production-topt-v0.2.0`):
`(numerator - total_assets*risk_free_rate)/headcount`. A bank takes the tree `gppe_banking_tce`: its
capital charge is measured tangible common equity (owner decision D2, 2026-10-26). The numerator is the
parser's industry-branch profit (pre-provision profit for financial issuers, gross profit otherwise). The
strategy composite evaluates the same pin, so one issuer has one value. The v0.1.0 financial
short-circuit (`pre_provision_profit/headcount` with no capital charge, then
`FINANCIAL_VALUATION_NOT_COMPARABLE`) is retired; financial issuers flow through the same tier / P-S
valuation path as every other issuer.

Rows written under the former TOPT-own bands (P/S 3-4, 8-10, 20-30, stamped with a
`three-tier-definition:` id) stay as an append-only prior vintage. This module still
writes the `mart.topt_*` namespace (disjoint from the base replay's `mart.strategy_*`).
"""

from __future__ import annotations

import re
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
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy import LaborEfficiencyTreePin, LargeModelValueV0Definition, ValuationBasis

from factors.forest import (
    FOREST,
    AliasKind,
    IssuerClass,
    UnpinnedClassError,
    bound_tree,
    capital_adjusted_node,
    definition_pins,
    evaluate,
    operating_numerator_node,
    required_inputs,
)

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
    # #496: insurers (SIC 63xx) — the operating numerator is revenue minus
    # policyholder benefits/claims (the insurance analog of the bank PPNR
    # proxy), owner-approved 2026-07-28.
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
    #: #1518 review: the strategy definition pins no labor-efficiency tree for the class of the issuer. The row is
    #: unavailable, as the composite excludes the issuer with `missing_issuer_class`. It is not an abort.
    LABOR_EFFICIENCY_TREE_UNPINNED = "labor_efficiency_tree_unpinned"
    #: #1518 review: a negative tangible common equity gives no value, because the capital charge would be a credit.
    NEGATIVE_TANGIBLE_COMMON_EQUITY = "negative_tangible_common_equity"
    #: #1518: the four captured leaves of the banking tree (`gppe_banking_tce`). The reason of a leaf
    #: is `missing_<snapshot field>`, the field being the `topt_snapshot_field` alias of the leaf.
    MISSING_STOCKHOLDERS_EQUITY = "missing_stockholders_equity"
    MISSING_PREFERRED_STOCK_VALUE = "missing_preferred_stock_value"
    MISSING_GOODWILL = "missing_goodwill"
    MISSING_INTANGIBLE_ASSETS_NET_EXCLUDING_GOODWILL = "missing_intangible_assets_net_excluding_goodwill"
    NONPOSITIVE_HEADCOUNT = "nonpositive_headcount"
    NONPOSITIVE_REVENUE = "nonpositive_revenue"
    NONPOSITIVE_SHARES_OUTSTANDING = "nonpositive_shares_outstanding"
    NONPOSITIVE_MARKET_PRICE = "nonpositive_market_price"
    STALE_INPUT = "stale_input"
    UNKNOWN_FRESHNESS = "unknown_freshness"
    #: #1235: P/GP divides by the operating numerator; zero or negative gives no multiple.
    NONPOSITIVE_GROSS_PROFIT = "nonpositive_gross_profit"
    #: #1277: a filed share count that cannot be put on the share basis of its close. A split
    #: between the count's basis date and the close makes the market value wrong by the split
    #: ratio. The snapshot names the cause, and the filed count is never used. Each value below
    #: equals one reason of `data_engine.datahub.share_basis`. A test holds the two registered.
    NO_SPLIT_ADJUSTED_SERIES = "no_split_adjusted_series"
    NO_UNADJUSTED_SERIES = "no_unadjusted_series"
    SPLIT_DERIVATION_FAILED = "split_derivation_failed"
    SPLIT_SERIES_DO_NOT_COVER_THE_WINDOW = "split_series_do_not_cover_the_window"
    LISTINGS_DISAGREE_ON_SPLITS = "listings_disagree_on_splits"
    SHARE_COUNT_UNDATED = "share_count_undated"
    SHARE_BASIS_AMBIGUOUS = "share_basis_ambiguous"
    CLOSE_JUMP_BEYOND_SERIES = "close_jump_beyond_series"
    MISSING_LISTING_UNIT_RATIO = "missing_listing_unit_ratio"
    #: #1518: the issuer has several listings and no `primary_listing` row names one. The snapshot
    #: builder cannot say which listing values the issuer, so the issuer is unavailable.
    PRIMARY_LISTING_UNDECLARED = "primary_listing_undeclared"


#: The reason codes above that name a share count with no proven share basis (#1277). A test holds this set
#: equal to the reasons that `data_engine.datahub.share_basis` builds into `Unavailable(...)`.
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
    }
)


#: The snapshot fields that only the banking tree reads (#1518): the captured leaves of `gppe_banking_tce`.
#: `test_core.py` holds this tuple equal to the `topt_snapshot_field` aliases of those leaves.
TCE_SNAPSHOT_FIELDS = (
    "stockholders_equity",
    "preferred_stock_value",
    "goodwill",
    "intangible_assets_net_excluding_goodwill",
)


def drop_absent_banking_leaves(data: dict[str, Any]) -> dict[str, Any]:
    """The dump ``data`` of a snapshot type without each banking leaf that is None (#1518).

    The core snapshot input and the snapshot member share this one rule, so a snapshot stored before
    the leaves existed, and the snapshot of any issuer that is not a bank, keep their content hash.
    """
    return {key: value for key, value in data.items() if key not in TCE_SNAPSHOT_FIELDS or value is not None}


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
    #: #1277: why an unavailable value is unavailable, when the snapshot builder knows a cause
    #: that is more exact than "missing". None means no cause is named. The key is absent from
    #: the serialized form when None, so a snapshot without a named cause keeps its content hash.
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
    """The rate and the pin of the labor-efficiency definition of the TOPT core (v0.2.0).

    It holds the risk-free rate and the version of the uniform tree. The formula is not here: the
    strategy definition pins the tree of each issuer class (#1518). The uniform tree is
    `(numerator - total_assets*risk_free_rate)/headcount`, where the numerator is the parser's
    industry-branch profit (pre-provision profit for financial issuers, gross profit otherwise). The
    banking tree charges measured tangible common equity instead of total assets. v0.1.0's financial
    short-circuit (`pre_provision_profit/headcount`, no capital charge) is retired.
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
    #: #1518: the tree that the strategy definition pins for each issuer class (`pinned_by`). The pins are part of
    #: the identity: `mart.topt_gppe_invocations` is unique on (snapshot, definition id), so a snapshot that is
    #: materialized again under other pins needs another definition id. None on a definition that predates the
    #: pins. The key is absent from the dump then, so that definition keeps its id and its content hash.
    labor_efficiency_trees: dict[str, LaborEfficiencyTreePin] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_trees(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.labor_efficiency_trees is None:
            data.pop("labor_efficiency_trees", None)
        return data

    def pinned_by(self, definition: LargeModelValueV0Definition) -> Self:
        """This definition with the trees that ``definition`` pins. A new identity: the pins are hashed."""
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
    #: #1518: True when the issuer has several listings and no `primary_listing` row names one. The
    #: identity above then names a listing only so that the row has coordinates. No value rests on it.
    #: The key is absent from the serialized form when False, so a stored input keeps its content hash.
    primary_listing_undeclared: bool = False
    #: #1518: the four captured leaves of the banking tree `gppe_banking_tce`. Each name is the
    #: `topt_snapshot_field` alias of one leaf. A bank carries the values it was captured with. Any other
    #: issuer carries none. Each key is absent from the serialized form when None, so a snapshot stored
    #: before #1518 reloads with the same content hash.
    stockholders_equity: ToptMetricInput | None = None
    preferred_stock_value: ToptMetricInput | None = None
    goodwill: ToptMetricInput | None = None
    intangible_assets_net_excluding_goodwill: ToptMetricInput | None = None

    @model_serializer(mode="wrap")
    def omit_absent_optional_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.primary_listing_undeclared:
            data.pop("primary_listing_undeclared", None)
        return drop_absent_banking_leaves(data)

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
            **{name: getattr(self, name) for name in TCE_SNAPSHOT_FIELDS},
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
    #: #1235: the P/GP valuation. The band is a P/GP band; the gap divides P/GP.
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
    #: The strategy definition whose bands tiered this row (#1235): one id for both engines.
    tier_definition_id: str = Field(pattern=r"^strategy-definition:[0-9a-f]{64}$")
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
        "current_pgp",
        "target_pgp_lower",
        "target_pgp_upper",
        "target_pgp_midpoint",
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
            self.current_pgp,
            self.target_pgp_lower,
            self.target_pgp_upper,
            self.target_pgp_midpoint,
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
            "current_pgp",
            "target_pgp_lower",
            "target_pgp_upper",
            "target_pgp_midpoint",
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


def _operating_numerator(
    snapshot: ToptCoreSnapshotInput, definition: LargeModelValueV0Definition
) -> tuple[str, ToptMetricInput | None]:
    """The snapshot field and input that GPPE's numerator reads for this issuer's class.

    P/GP divides market value by the same figure: gross profit, or a bank's pre-provision profit. The
    tree that the definition pins for the class declares which one (`operating_numerator_node`, #1518), so
    this reads the pinned tree and not a tree of its own.
    """
    issuer_class = IssuerClass(snapshot.operating_branch.value)
    node = operating_numerator_node(bound_tree(definition, issuer_class), issuer_class)
    field = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
    if field is None:
        raise AssertionError(f"GPPE numerator {node.key} names no snapshot field")
    return field, getattr(snapshot, field)


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
    definition: LargeModelValueV0Definition,
) -> ToptGppeResult:
    """Compute the module-2 base output without invoking the tier composite.

    The tree is the one that ``definition`` pins for the issuer's class (`bound_tree`, #1518): the
    uniform total-assets tree for a non-financial issuer or an insurer, the tangible-common-equity
    tree for a bank. The composite evaluates the same pin, so one issuer has one value.
    """

    if not invocation_id.startswith("topt-gppe-invocation:"):
        raise ValueError("invocation_id must be a content-addressed TOPT GPPE invocation")
    if snapshot.primary_listing_undeclared:
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=(ToptCoreReasonCode.PRIMARY_LISTING_UNDECLARED,),
            freshness=_snapshot_freshness(snapshot),
        )
    # The formula is the pinned tree (#528, #1518, docs/metric-forest.md): which inputs a class
    # needs, which numerator it uses and how the capital charge is bound are declared there,
    # not branched on here. A captured input names the snapshot field that carries it (a
    # missing one is `missing_<field>`); a parameter names the definition field.
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
            freshness=_snapshot_freshness(snapshot),
        )
    inputs: dict[str, Decimal | None] = {}
    reasons_list: list[ToptCoreReasonCode] = []
    for node in required_inputs(FOREST, tree, issuer_class):
        parameter = node.alias(AliasKind.DEFINITION_PARAMETER)
        if parameter is not None:
            inputs[node.key] = getattr(gppe_definition, parameter)
            continue
        field = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
        if field is None:
            raise AssertionError(f"GPPE tree input {node.key} names no snapshot field")
        metric: ToptMetricInput | None = getattr(snapshot, field)
        if metric is None or metric.availability is MetricAvailability.UNAVAILABLE or metric.value is None:
            reasons_list.append(ToptCoreReasonCode(f"missing_{field}"))
            continue
        inputs[node.key] = metric.value
    reasons = tuple(reasons_list)
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
    # One pinned tree per class: a bank's numerator is its pre-provision profit, everyone else's is
    # gross profit. The capital charge is the tree's: total assets for the uniform tree, measured
    # tangible common equity for the banking tree. The tree's decimal context is the one this kernel always used.
    evaluation = evaluate(FOREST, tree, issuer_class=issuer_class, inputs=inputs)
    capital_adjusted = evaluation.values.get(capital_adjusted_node(tree, issuer_class))
    gppe = evaluation.values.get(tree.root)
    # Every input was checked present and headcount positive above. A tree can still refuse a value: the banking
    # tree gives none for a negative tangible common equity (#1518).
    if "negative_operand" in evaluation.undefined.values():
        return _unavailable_gppe(
            snapshot,
            invocation_id=invocation_id,
            gppe_definition=gppe_definition,
            reasons=(ToptCoreReasonCode.NEGATIVE_TANGIBLE_COMMON_EQUITY,),
            freshness=freshness,
        )
    assert capital_adjusted is not None and gppe is not None and not evaluation.undefined
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
    definition: LargeModelValueV0Definition,
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
        tier_definition_id=definition.strategy_definition_id,
        tier_definition_sha256=definition.content_sha256,
    )


def _share_count_reasons(snapshot: ToptCoreSnapshotInput) -> list[ToptCoreReasonCode]:
    """The reasons that the share counts of the issuer's listings give no market value.

    #1277: a count that the snapshot builder dropped for a named cause reports that cause.
    A count that no cause explains reports `missing_shares_outstanding`.
    """
    shares = [component.shares_outstanding for component in snapshot.market_value_components]
    dropped = [metric for metric in shares if metric.value is None]
    if dropped:
        causes = {metric.unavailable_reason for metric in dropped}
        reasons = [cause for cause in causes if cause is not None]
        if None in causes:
            reasons.append(ToptCoreReasonCode.MISSING_SHARES_OUTSTANDING)
        return reasons
    if any(metric.value is not None and metric.value <= 0 for metric in shares):
        return [ToptCoreReasonCode.NONPOSITIVE_SHARES_OUTSTANDING]
    return []


def compute_topt_core(
    snapshot: ToptCoreSnapshotInput,
    gppe_result: ToptGppeResult,
    *,
    invocation_id: str,
    definition: LargeModelValueV0Definition,
) -> ToptCoreResult:
    """Compute module 7 from an exact materialized module-2 result.

    The tier bands, the valuation basis and the gap rule come from ``definition``, the
    strategy definition the deployed strategy tick evaluates (#1235). The engine values by
    P/GP. It still writes P/S: ``current_ps`` is market value / revenue, and
    ``target_ps_*`` hold the P/S that the P/GP band implies at this issuer's own gross
    margin (target P/GP x numerator / revenue). So ``target_ps_midpoint / current_ps - 1``
    equals the P/GP gap to the 34-digit context precision, and a P/S column never holds a
    P/GP value.
    """

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
            snapshot,
            gppe_result,
            invocation_id=invocation_id,
            definition=definition,
            reasons=gppe_result.reason_codes,
        )
    reasons: list[ToptCoreReasonCode] = []
    if snapshot.revenue is None or snapshot.revenue.value is None:
        reasons.append(ToptCoreReasonCode.MISSING_REVENUE)
    elif snapshot.revenue.value <= 0:
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_REVENUE)
    numerator_field, numerator = _operating_numerator(snapshot, definition)
    if numerator is None or numerator.value is None:
        reasons.append(ToptCoreReasonCode(f"missing_{numerator_field}"))
    elif numerator.value <= 0:
        reasons.append(ToptCoreReasonCode.NONPOSITIVE_GROSS_PROFIT)
    reasons.extend(_share_count_reasons(snapshot))
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
            definition=definition,
            reasons=tuple(reasons),
        )

    assert gppe_result.gppe is not None and gppe_result.capital_adjusted_gross_profit is not None
    assert snapshot.revenue is not None and snapshot.revenue.value is not None
    assert numerator is not None and numerator.value is not None
    with localcontext(_DECIMAL_CONTEXT):
        # Every listing's financial payload carries dei:EntityCommonStockShares-
        # Outstanding, which is the COMPANY total — identical on every class.
        # Summing price×shares per listing therefore counts a dual-listed issuer
        # once per class: Alphabet materialized at exactly 2× the P/S its own
        # captured inputs compute (#705). Identical share counts across several
        # listings are issuer-total by construction — value the company ONCE at
        # the execution listing's price (class prices differ by basis points;
        # per-class share counts are #63's instrument tranche, and when they
        # arrive they will differ and take the summing branch again).
        share_counts = {component.shares_outstanding.value for component in snapshot.market_value_components}
        if len(snapshot.market_value_components) > 1 and len(share_counts) == 1:
            execution = next(
                component
                for component in snapshot.market_value_components
                if component.listing_id == snapshot.listing_id
            )
            # The reasons gate above already refused any None price/share.
            assert execution.market_price.value is not None and execution.shares_outstanding.value is not None
            market_cap = execution.market_price.value * execution.shares_outstanding.value
        else:
            market_cap = sum(
                (
                    component.market_price.value * component.shares_outstanding.value
                    for component in snapshot.market_value_components
                    if component.market_price.value is not None and component.shares_outstanding.value is not None
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
        # The P/S the band implies at this issuer's gross margin (numerator / revenue).
        margin = numerator.value / snapshot.revenue.value
        target_ps_lower = target_pgp_lower * margin
        target_ps_upper = target_pgp_upper * margin
        target_ps_midpoint = target_pgp_midpoint * margin
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
        target_ps_lower=target_ps_lower,
        target_ps_upper=target_ps_upper,
        target_ps_midpoint=target_ps_midpoint,
        current_ps=current_ps,
        current_pgp=current_pgp,
        target_pgp_lower=target_pgp_lower,
        target_pgp_upper=target_pgp_upper,
        target_pgp_midpoint=target_pgp_midpoint,
        valuation_gap=valuation_gap,
        confidence=min(gppe_result.confidence, *(cell.confidence for cell in snapshot.cell_inputs)),
        freshness=gppe_result.freshness,
        input_observation_ids=snapshot.observation_ids,
        gppe_invocation_id=gppe_result.invocation_id,
        gppe_result_id=gppe_result.result_id,
        gppe_definition_id=gppe_result.gppe_definition_id,
        gppe_definition_sha256=gppe_result.gppe_definition_sha256,
        tier_definition_id=definition.strategy_definition_id,
        tier_definition_sha256=definition.content_sha256,
    )
