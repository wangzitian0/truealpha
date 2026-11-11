"""Canonical metric registry: unit family, period semantics, and panel metric specs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

NAMESPACE_UUID: UUID = uuid5(NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/metric-forest")

_MINTED_NODE_HEX = (
    ("labor_efficiency", "8f78ed40-9625-58eb-a866-4fd42015b43d"),
    ("gross_profit", "c682a9c8-c41d-5262-899b-f2d62000e10f"),
    ("pre_provision_profit", "75bf2b7a-1600-5bcf-9d86-648fb16a18ae"),
    ("total_assets", "c1d19aea-eb4a-525e-b62c-2fc9efe3bf77"),
    ("employees_total", "c7db1ab2-d0fa-5437-800e-34f3be376420"),
    ("risk_free_rate", "c6ae2f22-4b96-5610-8482-71c6ccd132ed"),
    ("operating_gross_profit", "34a38955-e69e-559b-a483-91f81b9a6ab0"),
    ("capital_charge", "6b4ce481-f045-5503-9675-921160e2bcd2"),
    ("capital_adjusted_gross_profit", "c3428286-eb85-5107-a32a-b8757ad5b340"),
    ("gppe_uniform_charge_v0", "6143cf17-ceba-5f78-a2d3-a7cc423c49a7"),
    ("stockholders_equity", "0aa53e41-0d12-5f11-92c4-fe6225166328"),
    ("preferred_stock_value", "ddf51b59-2c0e-52f0-8798-a555673c9bf6"),
    ("goodwill", "738c81a6-fd15-53ca-ad2f-679a2b700056"),
    ("intangible_assets_net_excluding_goodwill", "14313d73-f5f7-5a47-a087-ed9ce71ebd88"),
    ("tangible_deductions", "805d8f77-10aa-5dc4-b2bd-f47647e00593"),
    ("tangible_common_equity", "8c346339-0ca5-588a-90d4-e67c96db8ecb"),
    ("charged_tangible_common_equity", "2cc2205b-4649-51a4-9f21-9e64b238d162"),
    ("capital_charge_tce", "588a8f4d-b301-5a2e-b0f3-de6743d60af4"),
    ("capital_adjusted_tce", "57204502-7b9c-5671-b392-424b4368d9f1"),
    ("gppe_banking_tce_v1", "582c62d6-4873-5ff4-ad91-eba1d571fc4c"),
    ("financial_assets", "31e866ea-79cd-5bc5-8203-04fc40e3240f"),
    ("financial_returns", "c7f19762-975d-52b1-86db-360f8f7958fc"),
    ("bank_net_revenue", "4e99982c-03e2-510d-84fa-b7d8e988783e"),
    ("total_gross_return", "1519c717-370f-5b3f-a8fd-d37fc895d160"),
    ("capital_adjusted_operating_financial", "5e4a472f-71cd-57b3-90e6-21a5b94cc4cb"),
    ("gppe_operating_financial_v1", "25ff84c7-8f4d-515c-a6bb-e33cf52a2ef1"),
)
MINTED_NODE_IDS: Mapping[str, UUID] = {k: UUID(v) for k, v in _MINTED_NODE_HEX}
MINTED_TREE_IDS: Mapping[tuple[str, str], UUID] = {
    ("gppe", "production-topt-v0.2.0"): UUID("ef13b940-b695-5f5f-846a-a36face5cc3e"),
    ("gppe_banking_tce", "v2"): UUID("96fd6d4a-8983-5caf-b647-360122569d00"),
    ("gppe_operating_financial", "1.0.0"): UUID("7183c828-7252-5591-81a8-99fd05737995"),
}


def node_id_for(key: str) -> UUID:
    return MINTED_NODE_IDS.get(key) or uuid5(NAMESPACE_UUID, f"node:{key}")


def tree_id_for(key: str, version: str) -> UUID:
    return MINTED_TREE_IDS.get((key, version)) or uuid5(NAMESPACE_UUID, f"tree:{key}:{version}")


class UnitFamily(StrEnum):
    CURRENCY = "currency"
    COUNT = "count"
    RATIO = "ratio"
    PER_SHARE = "per_share"
    PER_EMPLOYEE = "per_employee"
    PERCENTAGE = "percentage"
    TIME_YEARS = "time_years"


class MetricSpec(BaseModel):
    model_config = {"frozen": True}

    name: str = Field(min_length=1)
    unit_family: UnitFamily
    periodic: bool = False
    description: str = Field(min_length=1)


_SPEC_ROWS: tuple[tuple[str, UnitFamily, str, bool], ...] = (
    ("revenue", UnitFamily.CURRENCY, "Total revenue for the fiscal period.", False),
    ("segment_revenue", UnitFamily.CURRENCY, "Revenue attributed to one reportable segment (#772).", False),
    ("gross_profit", UnitFamily.CURRENCY, "Gross profit for non-financial issuers.", False),
    ("pre_provision_profit", UnitFamily.CURRENCY, "Bank pre-provision net revenue (#59, #1519).", False),
    ("insurance_pre_claims_profit", UnitFamily.CURRENCY, "Insurance operating numerator (#496, #1519).", False),
    ("shares_weighted_average_basic", UnitFamily.COUNT, "Weighted-average basic common shares (#496, #1519).", False),
    ("cost_of_revenue", UnitFamily.CURRENCY, "Reported cost of revenue.", False),
    ("operating_income", UnitFamily.CURRENCY, "Reported operating income.", False),
    ("net_income", UnitFamily.CURRENCY, "Net income attributable to the company.", True),
    ("eps_diluted", UnitFamily.PER_SHARE, "Diluted earnings per share.", False),
    ("shares_outstanding", UnitFamily.COUNT, "Point-in-time common shares outstanding.", False),
    ("stockholders_equity", UnitFamily.CURRENCY, "Total stockholders' equity (#1176).", False),
    ("preferred_stock_value", UnitFamily.CURRENCY, "Preferred stock carrying value (#1176).", False),
    ("goodwill", UnitFamily.CURRENCY, "Goodwill carrying amount (#1176).", False),
    (
        "intangible_assets_net_excluding_goodwill",
        UnitFamily.CURRENCY,
        "Intangible assets excluding goodwill (#1176).",
        False,
    ),
    ("employees_total", UnitFamily.COUNT, "Total headcount.", False),
    ("total_assets", UnitFamily.CURRENCY, "Total assets on the balance sheet.", False),
    ("price", UnitFamily.PER_SHARE, "Unadjusted close price.", False),
    ("consensus_profit_growth", UnitFamily.PERCENTAGE, "Annual profit growth rate (#1354).", False),
    ("gross_profit_per_employee", UnitFamily.PER_EMPLOYEE, "Gross profit per employee (#1523).", False),
    ("financial_assets", UnitFamily.CURRENCY, "Cash and marketable investments (#528).", False),
    ("financial_returns", UnitFamily.CURRENCY, "Investment income, dividends, and gains (#528).", False),
    ("bank_net_revenue", UnitFamily.CURRENCY, "Bank net revenue.", False),
)


def _build_registry(specs: tuple[MetricSpec, ...]) -> dict[str, MetricSpec]:
    registry: dict[str, MetricSpec] = {}
    for spec in specs:
        if spec.name in registry:
            raise ValueError(f"duplicate metric registration: {spec.name}")
        registry[spec.name] = spec
    return registry


METRICS: dict[str, MetricSpec] = _build_registry(
    tuple(MetricSpec(name=n, unit_family=u, description=d, periodic=p) for n, u, d, p in _SPEC_ROWS)
)

INPUT_KEY_ALIASES: dict[str, str] = {
    "headcount": "employees_total",
    "last_close": "price",
    "gppe": "gross_profit_per_employee",
}


def is_registered_input_key(input_key: str) -> bool:
    return INPUT_KEY_ALIASES.get(input_key, input_key) in METRICS


def input_key_for_metric(metric: str) -> str:
    return next((k for k, name in INPUT_KEY_ALIASES.items() if name == metric), metric)


class IssuerClass(StrEnum):
    NON_FINANCIAL = "non_financial"
    FINANCIAL = "financial"
    INSURANCE = "insurance"


ALL_ISSUER_CLASSES: frozenset[IssuerClass] = frozenset(IssuerClass)


class PeriodSemantics(StrEnum):
    FISCAL_YEAR_FLOW = "fiscal-year-flow"
    FISCAL_YEAR_END_STOCK = "fiscal-year-end-stock"
    CUTOFF_INSTANT = "cutoff-instant"
    DEFINITIONAL = "definitional"
    NONE = "none"


class SignPolicy(StrEnum):
    MAY_BE_NEGATIVE = "may-be-negative"
    MUST_BE_NON_NEGATIVE = "must-be-non-negative"
    SIGN_IS_SIGNAL = "sign-is-signal"


PHYSICALLY_NON_NEGATIVE: Mapping[str, str] = MappingProxyType(
    {
        "total_assets": "balance of assets",
        "employees_total": "count of people",
        "risk_free_rate": "definition parameter",
        "capital_charge": "total_assets * risk_free_rate",
        "goodwill": "carrying amount of goodwill",
        "intangible_assets_net_excluding_goodwill": "carrying amount of intangible assets",
        "preferred_stock_value": "carrying amount of preferred stock",
        "tangible_deductions": "sum of non-negative balances",
        "charged_tangible_common_equity": "non-negative tangible common equity (#1518)",
        "financial_assets": "cash and marketable investments",
    }
)


class AliasKind(StrEnum):
    KEY = "key"
    METRIC = "metric"
    INPUT_KEY = "input_key"
    TOPT_SNAPSHOT_FIELD = "topt_snapshot_field"
    DEFINITION_PARAMETER = "definition_parameter"


@dataclass(frozen=True)
class SignFinding:
    node_key: str
    node_id: UUID
    issuer_class: IssuerClass | None
    policy: SignPolicy | None
    value: Decimal
    subject: str

    @property
    def violation(self) -> bool:
        return self.policy is None or self.policy is SignPolicy.MUST_BE_NON_NEGATIVE

    @property
    def signal(self) -> bool:
        return self.policy is SignPolicy.SIGN_IS_SIGNAL


PUBLISHED_COLUMNS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        table: MappingProxyType(
            {
                "gppe": "gppe_uniform_charge_v0",
                "operating_efficiency": "gppe_uniform_charge_v0",
                "capital_adjusted_gross_profit": "capital_adjusted_gross_profit",
            }
        )
        for table in ("mart.topt_gppe_results", "mart.topt_core_results")
    }
)


@dataclass(frozen=True)
class PanelMetricSpec:
    key: str
    unit: UnitFamily | None
    period: PeriodSemantics
    applicability: frozenset[IssuerClass]
    sign_policy: Mapping[IssuerClass, SignPolicy]
    node_id: UUID = field(default_factory=lambda: UUID(int=0))
    expr: Any = None
    expr_by_class: Mapping[IssuerClass, Any] = field(default_factory=dict)
    metric: str | None = None
    input_key: str | None = None
    snapshot_field: str | None = None
    definition_parameter: str | None = None
    default_when_missing: Decimal | None = None

    def __post_init__(self) -> None:
        if self.node_id == UUID(int=0):
            object.__setattr__(self, "node_id", node_id_for(self.key))
        if self.metric is not None and self.metric not in METRICS:
            raise ValueError(f"panel metric {self.key} references unregistered metric {self.metric!r}")

    @property
    def is_derived(self) -> bool:
        return self.expr is not None or bool(self.expr_by_class)

    @property
    def is_input(self) -> bool:
        return not self.is_derived and self.definition_parameter is None and self.unit is not None

    def expr_for(self, issuer_class: IssuerClass | None = None) -> Any:
        return self.expr_by_class[issuer_class] if issuer_class and issuer_class in self.expr_by_class else self.expr

    def alias(self, kind: AliasKind) -> str | None:
        return {
            AliasKind.KEY: self.key,
            AliasKind.METRIC: self.metric,
            AliasKind.INPUT_KEY: self.input_key,
            AliasKind.TOPT_SNAPSHOT_FIELD: self.snapshot_field,
            AliasKind.DEFINITION_PARAMETER: self.definition_parameter,
        }[kind]

    def with_updates(self, **changes: Any) -> PanelMetricSpec:
        return replace(self, **changes)

    def model_copy(self, *, update: Mapping[str, Any] | None = None) -> PanelMetricSpec:
        return replace(self, **(update or {}))


@dataclass(frozen=True)
class FactorVariantSpec:
    key: str
    version: str
    root: str
    applicability: frozenset[IssuerClass]
    tree_id: UUID = field(default_factory=lambda: UUID(int=0))
    pinned_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.tree_id == UUID(int=0):
            object.__setattr__(self, "tree_id", tree_id_for(self.key, self.version))


@dataclass(frozen=True)
class PanelRowEvaluation:
    variant_key: str
    variant_version: str
    issuer_class: IssuerClass
    values: Mapping[str, Decimal | None]
    undefined: Mapping[str, str] = field(default_factory=dict)


class UnpinnedClassError(ValueError):
    """The definition pins no factor variant for the issuer class."""


_ISSUER_CLASS_VALUES = frozenset(member.value for member in IssuerClass)


def judge_sign(spec: PanelMetricSpec, *, issuer_class: str, value: Decimal | None, subject: str) -> SignFinding | None:
    if value is None:
        return None
    declared = IssuerClass(issuer_class) if issuer_class in _ISSUER_CLASS_VALUES else None
    policy = spec.sign_policy.get(declared) if declared is not None else None
    if policy is not None and (value >= 0 or policy is SignPolicy.MAY_BE_NEGATIVE):
        return None
    return SignFinding(spec.key, spec.node_id, declared, policy, value, subject)


def metric_of(spec: PanelMetricSpec) -> str:
    return spec.metric or spec.key


def input_key_of(spec: PanelMetricSpec) -> str:
    return spec.input_key or input_key_for_metric(metric_of(spec))
