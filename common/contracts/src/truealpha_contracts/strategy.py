"""Versioned large_model_value_v0 strategy definitions."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib import resources
from importlib.resources.abc import Traversable
from itertools import pairwise
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, Self

from pydantic import AfterValidator, BeforeValidator, Field, ValidationInfo, field_validator, model_validator

from truealpha_contracts import common as _c
from truealpha_contracts import research as _r
from truealpha_contracts.universe import UniverseRef

_VERSION_PATTERN, _Q_MONTHS = r"^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$", frozenset({3, 6, 9, 12})
_FORMULA_FIELDS = tuple(
    "numerator capital_charge_base denominator formula "
    "missing_numerator_reason missing_capital_base_reason "
    "missing_denominator_reason nonpositive_denominator_reason".split()
)
_CONF_FIELDS = ("rule_id", "minimum_confidence", "confidence_semantics", "below_floor_reason")
_TIER_ORDER = {_r.ValuationTier.TRADITIONAL: 0, _r.ValuationTier.TECH: 1, _r.ValuationTier.LARGE_MODEL_NATIVE: 2}


class _Box(_c.AttrBox):
    optional_attrs = _FORMULA_FIELDS


def _box(o: Any) -> Any:
    return _c.box_attrs(o, _Box)


def _no_mut(v: str, info: ValidationInfo | str = "coordinate") -> str:
    n = info if isinstance(info, str) else (info.field_name or "coordinate")
    if {t for t in re.split(r"[._:/@-]", v.lower()) if t} & _c.DEFAULT_MUTABLE_TOKENS:
        raise ValueError(f"{n} cannot contain mutable ref")
    return v


def _dec(v: object) -> object:
    if isinstance(v, (bool, float)):
        raise ValueError("binary float disallowed")
    if isinstance(v, (Decimal, int, str)):
        try:
            p = Decimal(v)
        except (InvalidOperation, ValueError):
            return v
        if not p.is_finite():
            raise ValueError("must be finite")
        return p
    return v


_DecParam = Annotated[Decimal, BeforeValidator(_dec)]
_ImmKey = Annotated[str, AfterValidator(_no_mut)]


def _all_or_none(m: Any, fields: tuple[str, ...], label: str) -> None:
    if len({(m.get(n) if isinstance(m, Mapping) else getattr(m, n)) is not None for n in fields}) != 1:
        raise ValueError(f"{label} fields must be all present or all absent")


class FactorDimension(StrEnum):
    DIMENSIONLESS = "dimensionless"
    REPORTING_CURRENCY_PER_EMPLOYEE = "reporting_currency_per_employee"
    YEARS = "years"


class DecimalQuantization(_c.StrictFrozenModel):
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


class GoldenDecisionOutcome(StrEnum):
    SELECTED = "selected"
    RANKED_BEYOND_SELECTION_COUNT = "ranked_beyond_selection_count"
    REJECTED_VALUATION_ABOVE_TIER_BAND = "rejected_valuation_above_tier_band"
    EXCLUDED = "excluded"


class RiskFreeInstrument(StrEnum):
    US_TREASURY_BILL_3_MONTH = "us_treasury_bill_3_month"


class ValuationBasis(StrEnum):
    PRICE_TO_SALES = "price_to_sales"
    PRICE_TO_GROSS_PROFIT = "price_to_gross_profit"


_GAP_RULES = {
    ValuationBasis.PRICE_TO_SALES: ("target_ps_over_current_ps_minus_one", "target_ps/current_ps-1"),
    ValuationBasis.PRICE_TO_GROSS_PROFIT: ("target_pgp_over_current_pgp_minus_one", "target_pgp/current_pgp-1"),
}


class LaborEfficiencyTreePin(_c.StrictFrozenModel):
    tree_key: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    tree_version: _ImmKey = Field(min_length=1)
    tree_sha256: _c.Sha256


class _TierBandBase(_c.StrictFrozenModel):
    target_stem: ClassVar[str]
    tier: _r.ValuationTier
    labor_efficiency_lower_bound: _DecParam | None
    labor_efficiency_upper_bound: _DecParam | None

    @property
    def target_lower_bound(self) -> Decimal:
        return Decimal(getattr(self, f"{self.target_stem}_lower_bound"))

    @property
    def target_upper_bound(self) -> Decimal:
        return Decimal(getattr(self, f"{self.target_stem}_upper_bound"))

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        lo, hi = self.labor_efficiency_lower_bound, self.labor_efficiency_upper_bound
        if (lo is not None and hi is not None and lo >= hi) or self.target_lower_bound >= self.target_upper_bound:
            raise ValueError("lower bound >= upper bound")
        return self


class ProvisionalTierBand(_TierBandBase):
    target_stem = "target_ps"
    target_ps_lower_bound: _DecParam = Field(gt=0)
    target_ps_upper_bound: _DecParam = Field(gt=0)


class PriceToGrossProfitTierBand(_TierBandBase):
    target_stem = "target_pgp"
    target_pgp_lower_bound: _DecParam = Field(gt=0)
    target_pgp_upper_bound: _DecParam = Field(gt=0)


class _FactorDefBase(_c.StrictIdentifiedFrozenModel):
    id_field, id_prefix = "factor_definition_id", "factor-definition"
    factor_definition_id: str = _c.content_id("factor-definition")
    content_sha256: _c.ContentHash = ""
    factor_version: _ImmKey = Field(pattern=_VERSION_PATTERN)
    unit: Any
    quantization: DecimalQuantization
    factor_key: Literal["three_tier_valuation"]
    boundary_rule: Literal["lower_inclusive_upper_exclusive"]
    provisional_thresholds: bool

    def _canonicalize(self) -> None:
        if self.unit["dimension"] != FactorDimension.DIMENSIONLESS:
            raise ValueError("must be dimensionless")
        object.__setattr__(self, "unit", _box({**self.unit, "dimension": FactorDimension(self.unit["dimension"])}))
        ordered = tuple(sorted(self.bands, key=lambda x: _TIER_ORDER[x.tier]))  # type: ignore[attr-defined]
        if (
            {b.tier for b in ordered} != set(_r.ValuationTier)
            or ordered[0].labor_efficiency_lower_bound is not None
            or ordered[-1].labor_efficiency_upper_bound is not None
            or any(a.labor_efficiency_upper_bound != b.labor_efficiency_lower_bound for a, b in pairwise(ordered))
        ):
            raise ValueError("tier bands must be complete and contiguous")
        object.__setattr__(self, "bands", ordered)

    def band_for(self, le: Decimal) -> Any:
        return _band_for(self.bands, le)  # type: ignore[attr-defined]


def _band_for[BandT: _TierBandBase](bands: tuple[BandT, ...], le: Decimal) -> BandT:
    for b in bands:
        lo, hi = b.labor_efficiency_lower_bound, b.labor_efficiency_upper_bound
        if (lo is None or le >= lo) and (hi is None or le < hi):
            return b
    raise ValueError("labor efficiency outside tier bands")


class ThreeTierValuationDefinition(_FactorDefBase):
    bands: tuple[ProvisionalTierBand, ...] = Field(min_length=3, max_length=3)
    target_ps_selection: _r.TargetPsSelection
    provisional_thresholds: Literal[True]

    @property
    def valuation_basis(self) -> ValuationBasis:
        return ValuationBasis.PRICE_TO_SALES


class ThreeTierPgpValuationDefinition(_FactorDefBase):
    valuation_basis: Literal[ValuationBasis.PRICE_TO_GROSS_PROFIT]
    bands: tuple[PriceToGrossProfitTierBand, ...] = Field(min_length=3, max_length=3)
    target_pgp_selection: Literal["band_midpoint"]

    @field_validator("valuation_basis", mode="before")
    @classmethod
    def parse_basis(cls, value: object) -> object:
        return ValuationBasis(value) if isinstance(value, str) else value


class ConfidenceEligibilityRule(_c.OmitNoneFields, _c.StrictFrozenModel):
    omit_none_fields = _CONF_FIELDS
    rule_id: Literal["minimum_consumed_confidence_floor"] | None = None
    minimum_confidence: _DecParam | None = Field(default=None, ge=0, le=1)
    confidence_semantics: Literal["minimum_consumed_input_confidence"] | None = None
    maximum_input_age_days: int = Field(ge=1, le=730)
    below_floor_reason: ExclusionReason | None = None
    stale_input_reason: Literal[ExclusionReason.STALE_REQUIRED_INPUT]

    @model_validator(mode="after")
    def validate_reasons(self) -> Self:
        _all_or_none(self, _CONF_FIELDS, "confidence-floor")
        if self.below_floor_reason not in (None, ExclusionReason.BELOW_CONFIDENCE_FLOOR):
            raise ValueError("reason code mismatch")
        return self


class QuarterlyCutoffSchedule(_c.StrictFrozenModel):
    rule_id: Literal["calendar_quarter_end_utc"]
    first_cutoff_at: datetime
    cutoff_inclusive: Literal[True]

    @field_validator("first_cutoff_at")
    @classmethod
    def validate_first_cutoff_at(cls, value: datetime) -> datetime:
        c = _c.require_aware(value, "first_cutoff_at").astimezone(UTC)
        q_ok = c.month in _Q_MONTHS and (c + timedelta(days=1)).month != c.month
        if not q_ok or (c.hour, c.minute, c.second, c.microsecond) != (23, 59, 59, 0):
            raise ValueError("invalid quarter-end cutoff")
        return c


_REASON_KEYS = (
    "missing_numerator_reason",
    "missing_capital_base_reason",
    "missing_denominator_reason",
    "nonpositive_denominator_reason",
    "missing_market_value_reason",
    "missing_revenue_reason",
    "nonpositive_revenue_reason",
    "missing_gross_profit_reason",
    "nonpositive_gross_profit_reason",
)


def _stamp_factor(raw: dict[str, Any]) -> _Box:
    q = DecimalQuantization.model_validate(raw["quantization"])
    if not isinstance(fv := raw.get("factor_version"), str) or not re.fullmatch(_VERSION_PATTERN, fv):
        raise ValueError("invalid factor_version")
    _no_mut(fv, "factor_version")
    pay = {k: v for k, v in raw.items() if k not in {"factor_definition_id", "content_sha256"}}
    eh = _c.canonical_sha256(pay)
    eid = f"factor-definition:{eh}"
    if raw.get("content_sha256", "") not in ("", eh) or raw.get("factor_definition_id", "") not in ("", eid):
        raise ValueError("factor id/hash mismatch")
    out = _box({**pay, "factor_definition_id": eid, "content_sha256": eh})
    out["unit"]["dimension"], out["quantization"] = FactorDimension(out["unit"]["dimension"]), q
    for rk in _REASON_KEYS:
        if out.get(rk) is not None:
            out[rk] = ExclusionReason(out[rk])
    if (rf := out.get("risk_free")) is not None:
        rf["instrument"] = RiskFreeInstrument(rf["instrument"])
        rf["missing_rate_reason"] = ExclusionReason(rf["missing_rate_reason"])
    return out


class LargeModelValueV0Definition(_c.OmitNoneFields, _c.StrictIdentifiedFrozenModel):
    id_field, id_prefix = "strategy_definition_id", "strategy-definition"
    omit_none_fields = ("price_to_gross_profit", "universe", "labor_efficiency_tree_keys")
    strategy_definition_id: str = _c.content_id("strategy-definition")
    content_sha256: _c.ContentHash = ""
    strategy_id: Literal["large_model_value_v0"]
    definition_version: _ImmKey = Field(pattern=_VERSION_PATTERN)
    provisional_semantics: bool
    universe: UniverseRef | None = None
    schedule: QuarterlyCutoffSchedule
    labor_efficiency: Any
    labor_efficiency_tree_keys: dict[str, LaborEfficiencyTreePin] | None = None
    price_to_sales: Any
    price_to_gross_profit: Any | None = None
    tier_valuation: ThreeTierValuationDefinition | ThreeTierPgpValuationDefinition
    valuation_gap: Any
    eligibility: ConfidenceEligibilityRule
    selection: Any
    sizing: Any
    execution: _r.StrategyExecutionPolicy
    transaction_cost: _r.StrategyTransactionCostPolicy
    total_return: _r.StrategyTotalReturnPolicy

    def _canonicalize(self) -> None:
        le = dict(self.labor_efficiency)
        _all_or_none(le, _FORMULA_FIELDS, "formula")
        le = {k: v for k, v in le.items() if k not in _FORMULA_FIELDS or v is not None}
        if le.get("formula") is not None:
            exp = (
                ("reporting_currency_per_employee", "missing_headcount_disclosure", "nonpositive_headcount")
                if le.get("denominator") == "headcount"
                else ("dimensionless", "missing_labor_cost_disclosure", "nonpositive_labor_cost")
            )
            if (
                le["unit"]["dimension"],
                le.get("missing_denominator_reason"),
                le.get("nonpositive_denominator_reason"),
            ) != exp:
                raise ValueError("labor efficiency contract invalid")
        for attr, val in (
            ("labor_efficiency", _stamp_factor(le)),
            ("price_to_sales", _stamp_factor(dict(self.price_to_sales))),
        ):
            object.__setattr__(self, attr, val)
        if self.price_to_gross_profit is not None:
            object.__setattr__(self, "price_to_gross_profit", _stamp_factor(dict(self.price_to_gross_profit)))
        vg, sel, sz = _box(dict(self.valuation_gap)), _box(dict(self.selection)), _box(dict(self.sizing))
        vg["quantization"] = DecimalQuantization.model_validate(vg["quantization"])
        sz["quantization"] = DecimalQuantization.model_validate(sz["quantization"])
        if not (1 <= int(sel["selection_count"]) <= 1000) or isinstance(sel["selection_count"], bool):
            raise ValueError("invalid selection_count")
        for attr, val in (("valuation_gap", vg), ("selection", sel), ("sizing", sz)):
            object.__setattr__(self, attr, val)
        basis = self.tier_valuation.valuation_basis
        if (basis is ValuationBasis.PRICE_TO_GROSS_PROFIT) != (self.price_to_gross_profit is not None):
            raise ValueError("a P/GP tier valuation requires exactly the price_to_gross_profit factor definition")
        if (vg["rule_id"], vg["formula"]) != _GAP_RULES[basis]:
            raise ValueError("valuation-gap rule must use multiple of the tier bands")
        if self.provisional_semantics != self.tier_valuation.provisional_thresholds:
            raise ValueError("provisional_semantics must equal the tier valuation's provisional_thresholds")
        if (self.labor_efficiency.get("formula") is None) != (
            self.labor_efficiency_tree_keys is not None
        ) or self.labor_efficiency_tree_keys == {}:
            raise ValueError("labor efficiency has one owner")

    @property
    def valuation_basis(self) -> ValuationBasis:
        return self.tier_valuation.valuation_basis

    @property
    def valuation_quantization(self) -> DecimalQuantization:
        return (self.price_to_gross_profit or self.price_to_sales).quantization


LIVE_STRATEGY_DEFINITION_RESOURCE = "large_model_value_v0_definition_1_1_0.json"


@functools.lru_cache(maxsize=16)
def load_strategy_definition_corpus(resource: str) -> dict[str, Any]:
    p = json.loads(resources.files("truealpha_contracts.data").joinpath(resource).read_bytes())
    if not isinstance(p, dict):
        raise ValueError(f"{resource}: not a JSON object")
    return p


def _def_of(resource: str, corpus: dict[str, Any]) -> LargeModelValueV0Definition:
    d = LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
    if d.strategy_definition_id != corpus["expected"]["strategy_definition_id"]:
        raise ValueError(f"{resource}: id mismatch")
    return d


@functools.lru_cache(maxsize=16)
def load_strategy_definition(resource: str) -> LargeModelValueV0Definition:
    return _def_of(resource, load_strategy_definition_corpus(resource))


def load_live_strategy_definition() -> LargeModelValueV0Definition:
    return load_strategy_definition(LIVE_STRATEGY_DEFINITION_RESOURCE)


def load_live_strategy_corpus() -> dict[str, Any]:
    return load_strategy_definition_corpus(LIVE_STRATEGY_DEFINITION_RESOURCE)


class StrategyDefinitionIdentity(NamedTuple):
    version: str
    content_sha256: str
    provisional_semantics: bool


DefinitionReviewState = Literal["reviewed", "provisional", "unknown"]
_REG_CTX: ContextVar[tuple[StrategyDefinitionIdentity, ...]] = ContextVar(
    "truealpha_registered_strategy_definitions", default=()
)


def strategy_definitions_in(directory: Traversable) -> tuple[StrategyDefinitionIdentity, ...]:
    entries: list[StrategyDefinitionIdentity] = []
    for item in sorted(directory.iterdir(), key=lambda e: e.name):
        if (
            item.name.endswith(".json")
            and isinstance(c := json.loads(item.read_bytes()), dict)
            and "strategy_definition" in c
        ):
            d = _def_of(item.name, c)
            if any(e.version == d.definition_version for e in entries):
                raise ValueError(f"a second definition file has version {d.definition_version}: {item.name}")
            entries.append(StrategyDefinitionIdentity(d.definition_version, d.content_sha256, d.provisional_semantics))
    return tuple(sorted(entries))


@functools.cache
def _packaged_defs() -> tuple[StrategyDefinitionIdentity, ...]:
    return strategy_definitions_in(resources.files("truealpha_contracts.data"))


def strategy_definition_registry() -> tuple[StrategyDefinitionIdentity, ...]:
    return tuple(sorted(_packaged_defs() + _REG_CTX.get()))


@contextmanager
def registered_strategy_definition(definition: LargeModelValueV0Definition) -> Iterator[StrategyDefinitionIdentity]:
    d = LargeModelValueV0Definition.model_validate_json(definition.model_dump_json())
    ident = StrategyDefinitionIdentity(d.definition_version, d.content_sha256, d.provisional_semantics)
    if any(
        e.version == ident.version or e.content_sha256 == ident.content_sha256 for e in strategy_definition_registry()
    ):
        raise ValueError(f"the registry already holds version {ident.version} or hash {ident.content_sha256}")
    tok = _REG_CTX.set(_REG_CTX.get() + (ident,))
    try:
        yield ident
    finally:
        _REG_CTX.reset(tok)


def definition_review_state(version: str, content_sha256: str) -> DefinitionReviewState:
    hit = next(
        (e for e in strategy_definition_registry() if e.content_sha256 == content_sha256 and e.version == version), None
    )
    return "unknown" if hit is None else ("provisional" if hit.provisional_semantics else "reviewed")
