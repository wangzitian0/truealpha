"""Canonical metric registry: unit family, period semantics, and panel specs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

from truealpha_contracts.common import load_data_json

NAMESPACE_UUID: UUID = uuid5(NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/metric-forest")
_REG = load_data_json("registries.v1.json")
MINTED_NODE_IDS: Mapping[str, UUID] = {k: UUID(v) for k, v in _REG["minted_nodes"]}
MINTED_TREE_IDS: Mapping[tuple[str, str], UUID] = {(k, v): UUID(uid) for (k, v), uid in _REG["minted_trees"]}


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


METRICS: dict[str, MetricSpec] = {
    n: MetricSpec(name=n, unit_family=UnitFamily(u), description=d, periodic=p) for n, u, d, p in _REG["metric_specs"]
}
INPUT_KEY_ALIASES: dict[str, str] = dict(_REG["input_key_aliases"])
PHYSICALLY_NON_NEGATIVE: Mapping[str, str] = MappingProxyType(_REG["physically_non_negative"])
_GPPE = MappingProxyType(
    {
        "gppe_uniform_charge_v0": "gppe_uniform_charge_v0",
        "gppe_banking_tce_v1": "gppe_banking_tce_v1",
        "capital_adjusted_gross_profit": "capital_adjusted_gross_profit",
        "capital_adjusted_tce": "capital_adjusted_tce",
    }
)
PUBLISHED_COLUMNS: Mapping[str, Mapping[str, str]] = MappingProxyType({"mart.metric_node_values": _GPPE})


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


class AliasKind(StrEnum):
    KEY = "key"
    METRIC = "metric"
    INPUT_KEY = "input_key"
    CORE_SNAPSHOT_FIELD = "snapshot_field"
    DEFINITION_PARAMETER = "definition_parameter"


@dataclass(frozen=True)
class SignFinding:
    node_key: str
    node_id: UUID
    issuer_class: IssuerClass | None
    policy: SignPolicy | None
    value: Decimal
    subject: str

    violation = property(lambda self: self.policy is None or self.policy is SignPolicy.MUST_BE_NON_NEGATIVE)
    signal = property(lambda self: self.policy is SignPolicy.SIGN_IS_SIGNAL)


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
            raise ValueError(f"{self.key}: unregistered metric {self.metric!r}")

    is_derived = property(lambda self: self.expr is not None or bool(self.expr_by_class))
    is_input = property(
        lambda self: not self.is_derived and self.definition_parameter is None and self.unit is not None
    )

    def expr_for(self, issuer_class: IssuerClass | None = None) -> Any:
        return self.expr_by_class[issuer_class] if issuer_class and issuer_class in self.expr_by_class else self.expr

    def alias(self, kind: AliasKind) -> str | None:
        return getattr(self, kind.value)

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
    pass


_IC_VALS = frozenset(m.value for m in IssuerClass)


def judge_sign(spec: PanelMetricSpec, *, issuer_class: str, value: Decimal | None, subject: str) -> SignFinding | None:
    if value is None:
        return None
    dec = IssuerClass(issuer_class) if issuer_class in _IC_VALS else None
    pol = spec.sign_policy.get(dec) if dec is not None else None
    if pol is not None and (value >= 0 or pol is SignPolicy.MAY_BE_NEGATIVE):
        return None
    return SignFinding(spec.key, spec.node_id, dec, pol, value, subject)


def metric_of(spec: PanelMetricSpec) -> str:
    return spec.metric or spec.key


def input_key_of(spec: PanelMetricSpec) -> str:
    return spec.input_key or input_key_for_metric(metric_of(spec))
