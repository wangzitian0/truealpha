"""Standardized wide panel and native Polars pl.Expr factor evaluation (#1610).

DataHub normalization prepares a clean wide panel keyed on ``(issuer_id, period)`` where
each metric column carries its value and confidence (``conf__<metric>``), already
period-aligned and unit-normalized. Factors and intermediate metrics are native Polars
``pl.Expr`` definitions over the wide panel:

- Null-filling (``pl.col("goodwill").fill_null(0)``), positivity conditions
  (``pl.when(pl.col("tangible_common_equity") > 0).then(...).otherwise(None)``), and
  multi-period windows (``.shift(3).over("issuer_id")``) live directly in Polars ``pl.Expr``.
- Consumed input columns are discovered automatically via ``expr.meta.root_names()``.
- Consumed confidence is computed via ``pl.min_horizontal(...)`` over the non-null consumed
  input confidence columns.
- Final quantized outputs preserve exact 34-digit Decimal precision with ``ROUND_HALF_EVEN``.
- Deterministic ``node_id`` UUIDs preserve the existing minted UUIDs for existing node keys
  and use ``uuid5`` for any new factor key so ``mart.metric_node_values`` requires zero SQL
  migration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from functools import reduce
from types import MappingProxyType
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

import polars as pl
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
    model_validator,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.metrics import (
    ALL_ISSUER_CLASSES,
    METRICS,
    PHYSICALLY_NON_NEGATIVE,
    IssuerClass,
    PeriodSemantics,
    SignPolicy,
    UnitFamily,
    input_key_for_metric,
)
from truealpha_contracts.strategy import LaborEfficiencyTreePin, LargeModelValueV0Definition

#: The URL namespace of the metric panel. Any new node or tree UUID is minted with ``uuid5``
#: under this namespace so identities remain deterministic without a schema migration.
NAMESPACE_UUID: UUID = uuid5(NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/metric-forest")

#: Exact 34-digit IEEE 754 decimal128 context with bankers' rounding (`ROUND_HALF_EVEN`).
PANEL_DECIMAL_PRECISION = 34
PANEL_DECIMAL_ROUNDING = "ROUND_HALF_EVEN"
PANEL_DECIMAL_CONTEXT = Context(prec=PANEL_DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)

#: Minted UUIDs of the 26 existing nodes. Preserved verbatim so `mart.metric_node_values`
#: and `output_invariants.py` need zero SQL migration.
MINTED_NODE_IDS: Mapping[str, UUID] = {
    "labor_efficiency": UUID("8f78ed40-9625-58eb-a866-4fd42015b43d"),
    "gross_profit": UUID("c682a9c8-c41d-5262-899b-f2d62000e10f"),
    "pre_provision_profit": UUID("75bf2b7a-1600-5bcf-9d86-648fb16a18ae"),
    "total_assets": UUID("c1d19aea-eb4a-525e-b62c-2fc9efe3bf77"),
    "employees_total": UUID("c7db1ab2-d0fa-5437-800e-34f3be376420"),
    "risk_free_rate": UUID("c6ae2f22-4b96-5610-8482-71c6ccd132ed"),
    "operating_gross_profit": UUID("34a38955-e69e-559b-a483-91f81b9a6ab0"),
    "capital_charge": UUID("6b4ce481-f045-5503-9675-921160e2bcd2"),
    "capital_adjusted_gross_profit": UUID("c3428286-eb85-5107-a32a-b8757ad5b340"),
    "gppe_uniform_charge_v0": UUID("6143cf17-ceba-5f78-a2d3-a7cc423c49a7"),
    "stockholders_equity": UUID("0aa53e41-0d12-5f11-92c4-fe6225166328"),
    "preferred_stock_value": UUID("ddf51b59-2c0e-52f0-8798-a555673c9bf6"),
    "goodwill": UUID("738c81a6-fd15-53ca-ad2f-679a2b700056"),
    "intangible_assets_net_excluding_goodwill": UUID("14313d73-f5f7-5a47-a087-ed9ce71ebd88"),
    "tangible_deductions": UUID("805d8f77-10aa-5dc4-b2bd-f47647e00593"),
    "tangible_common_equity": UUID("8c346339-0ca5-588a-90d4-e67c96db8ecb"),
    "charged_tangible_common_equity": UUID("2cc2205b-4649-51a4-9f21-9e64b238d162"),
    "capital_charge_tce": UUID("588a8f4d-b301-5a2e-b0f3-de6743d60af4"),
    "capital_adjusted_tce": UUID("57204502-7b9c-5671-b392-424b4368d9f1"),
    "gppe_banking_tce_v1": UUID("582c62d6-4873-5ff4-ad91-eba1d571fc4c"),
    "financial_assets": UUID("31e866ea-79cd-5bc5-8203-04fc40e3240f"),
    "financial_returns": UUID("c7f19762-975d-52b1-86db-360f8f7958fc"),
    "bank_net_revenue": UUID("4e99982c-03e2-510d-84fa-b7d8e988783e"),
    "total_gross_return": UUID("1519c717-370f-5b3f-a8fd-d37fc895d160"),
    "capital_adjusted_operating_financial": UUID("5e4a472f-71cd-57b3-90e6-21a5b94cc4cb"),
    "gppe_operating_financial_v1": UUID("25ff84c7-8f4d-515c-a6bb-e33cf52a2ef1"),
}

#: Minted UUIDs of the three GPPE tree specifications.
MINTED_TREE_IDS: Mapping[tuple[str, str], UUID] = {
    ("gppe", "production-topt-v0.2.0"): UUID("ef13b940-b695-5f5f-846a-a36face5cc3e"),
    ("gppe_banking_tce", "v2"): UUID("96fd6d4a-8983-5caf-b647-360122569d00"),
    ("gppe_operating_financial", "1.0.0"): UUID("7183c828-7252-5591-81a8-99fd05737995"),
}


def node_id_for(key: str) -> UUID:
    """Return the deterministic UUID for ``key`` (preserving minted UUIDs and falling back to uuid5)."""
    if key in MINTED_NODE_IDS:
        return MINTED_NODE_IDS[key]
    return uuid5(NAMESPACE_UUID, f"node:{key}")


def tree_id_for(key: str, version: str) -> UUID:
    """Return the deterministic UUID for ``(key, version)``."""
    if (key, version) in MINTED_TREE_IDS:
        return MINTED_TREE_IDS[(key, version)]
    return uuid5(NAMESPACE_UUID, f"tree:{key}:{version}")


# ---------------------------------------------------------------------------
# 1. Native Polars pl.Expr Definitions Over the Standardized Wide Panel
# ---------------------------------------------------------------------------

#: Intermediate and root Polars expressions over the standardized wide panel.
#: The wide panel carries `(issuer_id, period, issuer_class, ...metric columns..., ...conf__<metric> columns...)`.

EXPR_OPERATING_GROSS_PROFIT: pl.Expr = (
    pl.when(pl.col("issuer_class") == IssuerClass.FINANCIAL.value)
    .then(pl.col("pre_provision_profit"))
    .otherwise(pl.col("gross_profit"))
)

EXPR_CAPITAL_CHARGE: pl.Expr = pl.col("total_assets") * pl.col("risk_free_rate")

EXPR_CAPITAL_ADJUSTED_GROSS_PROFIT: pl.Expr = pl.col("operating_gross_profit") - pl.col("capital_charge")

EXPR_TANGIBLE_DEDUCTIONS: pl.Expr = (
    pl.col("preferred_stock_value") + pl.col("goodwill") + pl.col("intangible_assets_net_excluding_goodwill")
)

#: Null-safe tangible deductions where goodwill and intangibles default to 0 when unstated on the balance sheet.
EXPR_TANGIBLE_DEDUCTIONS_FILL_NULL: pl.Expr = (
    pl.col("preferred_stock_value").fill_null(0)
    + pl.col("goodwill").fill_null(0)
    + pl.col("intangible_assets_net_excluding_goodwill").fill_null(0)
)

EXPR_TANGIBLE_COMMON_EQUITY: pl.Expr = pl.col("stockholders_equity") - pl.col("tangible_deductions")

#: Positivity condition: non-negative TCE is charged; negative TCE produces None.
EXPR_CHARGED_TANGIBLE_COMMON_EQUITY: pl.Expr = (
    pl.when(pl.col("tangible_common_equity") >= 0).then(pl.col("tangible_common_equity")).otherwise(None)
)

#: Strict positivity condition variant (`tangible_common_equity > 0`) for direct wide-panel expressions.
EXPR_POSITIVE_TANGIBLE_COMMON_EQUITY: pl.Expr = (
    pl.when(pl.col("tangible_common_equity") > 0).then(pl.col("tangible_common_equity")).otherwise(None)
)

EXPR_CAPITAL_CHARGE_TCE: pl.Expr = pl.col("charged_tangible_common_equity") * pl.col("risk_free_rate")

EXPR_CAPITAL_ADJUSTED_TCE: pl.Expr = pl.col("pre_provision_profit") - pl.col("capital_charge_tce")

#: Null-filling: absent `financial_returns` adds 0 to `gross_profit`.
EXPR_TOTAL_GROSS_RETURN: pl.Expr = pl.col("gross_profit") + pl.col("financial_returns").fill_null(0)

EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL: pl.Expr = (
    pl.when(pl.col("issuer_class") == IssuerClass.FINANCIAL.value)
    .then(pl.col("bank_net_revenue") - pl.col("capital_charge_tce"))
    .otherwise(pl.col("total_gross_return") - pl.col("capital_charge"))
)

# GPPE root variants
EXPR_GPPE_UNIFORM_CHARGE_V0: pl.Expr = (
    pl.when(pl.col("employees_total") > 0)
    .then(pl.col("capital_adjusted_gross_profit") / pl.col("employees_total"))
    .otherwise(None)
)

EXPR_GPPE_BANKING_TCE_V1: pl.Expr = (
    pl.when(pl.col("employees_total") > 0)
    .then(pl.col("capital_adjusted_tce") / pl.col("employees_total"))
    .otherwise(None)
)

EXPR_GPPE_OPERATING_FINANCIAL_V1: pl.Expr = (
    pl.when(pl.col("employees_total") > 0)
    .then(pl.col("capital_adjusted_operating_financial") / pl.col("employees_total"))
    .otherwise(None)
)

# Valuation & growth factor expressions over the standardized wide panel
EXPR_PRICE_TO_SALES: pl.Expr = (
    pl.when((pl.col("price") > 0) & (pl.col("shares_outstanding") > 0) & (pl.col("revenue") > 0))
    .then((pl.col("price") * pl.col("shares_outstanding")) / pl.col("revenue"))
    .otherwise(None)
)

EXPR_PRICE_TO_GROSS_PROFIT: pl.Expr = (
    pl.when((pl.col("price") > 0) & (pl.col("shares_outstanding") > 0) & (pl.col("gross_profit") > 0))
    .then((pl.col("price") * pl.col("shares_outstanding")) / pl.col("gross_profit"))
    .otherwise(None)
)

#: Multi-period window over sorted `(issuer_id, period)` for 3-year recency-weighted earnings growth:
#: weights 1, 2, 3 over `y0 -> y1`, `y1 -> y2`, `y2 -> y3` where `.shift(k).over("issuer_id")`
#: accesses prior annual periods and requires positive earnings across all four periods.
_NI_Y3 = pl.col("net_income")
_NI_Y2 = pl.col("net_income").shift(1).over("issuer_id")
_NI_Y1 = pl.col("net_income").shift(2).over("issuer_id")
_NI_Y0 = pl.col("net_income").shift(3).over("issuer_id")

_WEIGHTED_GROWTH_3Y: pl.Expr = (
    1.0 * (_NI_Y1 / _NI_Y0 - 1.0) + 2.0 * (_NI_Y2 / _NI_Y1 - 1.0) + 3.0 * (_NI_Y3 / _NI_Y2 - 1.0)
) / 6.0

EXPR_PEG: pl.Expr = (
    pl.when(
        (pl.col("price") > 0)
        & (pl.col("shares_outstanding") > 0)
        & (_NI_Y0 > 0)
        & (_NI_Y1 > 0)
        & (_NI_Y2 > 0)
        & (_NI_Y3 > 0)
        & (_WEIGHTED_GROWTH_3Y > 0)
    )
    .then(((pl.col("price") * pl.col("shares_outstanding")) / _NI_Y3) / (_WEIGHTED_GROWTH_3Y * 100.0))
    .otherwise(None)
)

#: All intermediate and factor Polars expressions by canonical name.
INTERMEDIATE_EXPRESSIONS: Mapping[str, pl.Expr] = {
    "operating_gross_profit": EXPR_OPERATING_GROSS_PROFIT,
    "capital_charge": EXPR_CAPITAL_CHARGE,
    "capital_adjusted_gross_profit": EXPR_CAPITAL_ADJUSTED_GROSS_PROFIT,
    "tangible_deductions": EXPR_TANGIBLE_DEDUCTIONS,
    "tangible_common_equity": EXPR_TANGIBLE_COMMON_EQUITY,
    "charged_tangible_common_equity": EXPR_CHARGED_TANGIBLE_COMMON_EQUITY,
    "capital_charge_tce": EXPR_CAPITAL_CHARGE_TCE,
    "capital_adjusted_tce": EXPR_CAPITAL_ADJUSTED_TCE,
    "total_gross_return": EXPR_TOTAL_GROSS_RETURN,
    "capital_adjusted_operating_financial": EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL,
}

FACTOR_EXPRESSIONS: Mapping[str, pl.Expr] = {
    "gppe_uniform_charge_v0": EXPR_GPPE_UNIFORM_CHARGE_V0,
    "gppe_banking_tce_v1": EXPR_GPPE_BANKING_TCE_V1,
    "gppe_operating_financial_v1": EXPR_GPPE_OPERATING_FINANCIAL_V1,
    "price_to_sales": EXPR_PRICE_TO_SALES,
    "price_to_gross_profit": EXPR_PRICE_TO_GROSS_PROFIT,
    "peg": EXPR_PEG,
}

PANEL_EXPRESSIONS: Mapping[str, pl.Expr] = {
    **INTERMEDIATE_EXPRESSIONS,
    **FACTOR_EXPRESSIONS,
}

#: Structural index columns of the wide panel that are not metric value columns.
PANEL_INDEX_COLUMNS: frozenset[str] = frozenset({"issuer_id", "period", "issuer_class"})


def consumed_columns(expr: pl.Expr) -> tuple[str, ...]:
    """Discover the root column names consumed by ``expr`` via ``expr.meta.root_names()``."""
    return tuple(name for name in expr.meta.root_names() if name not in PANEL_INDEX_COLUMNS)


def leaf_consumed_columns(
    target: str | pl.Expr,
    *,
    issuer_class: IssuerClass | None = None,
    expressions: Mapping[str, pl.Expr] = PANEL_EXPRESSIONS,
) -> tuple[str, ...]:
    """Recursively expand ``expr.meta.root_names()`` to the leaf metric columns consumed by ``target``."""
    seen: list[str] = []

    def _visit(current: str | pl.Expr) -> None:
        if isinstance(current, str):
            if current in PANEL_INDEX_COLUMNS:
                return
            if current == "operating_gross_profit" and issuer_class is not None:
                branch_col = "pre_provision_profit" if issuer_class is IssuerClass.FINANCIAL else "gross_profit"
                _visit(branch_col)
                return
            if current == "capital_adjusted_operating_financial" and issuer_class is not None:
                if issuer_class is IssuerClass.FINANCIAL:
                    _visit("bank_net_revenue")
                    _visit("capital_charge_tce")
                else:
                    _visit("total_gross_return")
                    _visit("capital_charge")
                return
            sub_expr = expressions.get(current)
            if sub_expr is None:
                if current not in seen:
                    seen.append(current)
                return
            for root in consumed_columns(sub_expr):
                _visit(root)
            return
        for root in consumed_columns(current):
            _visit(root)

    _visit(target)
    return tuple(seen)


def confidence_expr(columns: Sequence[str], *, window_shifts: Sequence[int] = ()) -> pl.Expr:
    """Compute consumed confidence via ``pl.min_horizontal(...)`` over the consumed ``conf__<col>`` columns."""
    conf_exprs: list[pl.Expr] = []
    for col in columns:
        if col in PANEL_INDEX_COLUMNS or col == "risk_free_rate":
            continue
        base_conf = pl.col(f"conf__{col}")
        conf_exprs.append(base_conf)
        for shift_n in window_shifts:
            conf_exprs.append(base_conf.shift(shift_n).over("issuer_id"))
    if not conf_exprs:
        return pl.lit(1.0)
    return pl.min_horizontal(*conf_exprs)


def build_wide_panel(rows: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """Build a standardized wide panel sorted by ``(issuer_id, period)`` from normalized row dicts."""
    if not rows:
        return pl.DataFrame(schema={"issuer_id": pl.String, "period": pl.String, "issuer_class": pl.String})
    df = pl.DataFrame(list(rows))
    if "issuer_class" not in df.columns:
        df = df.with_columns(issuer_class=pl.lit(IssuerClass.NON_FINANCIAL.value))
    if "period" not in df.columns:
        df = df.with_columns(period=pl.lit("latest"))
    return df.sort(["issuer_id", "period"])


def evaluate_wide_panel(
    panel: pl.DataFrame,
    *,
    targets: Sequence[str] = tuple(PANEL_EXPRESSIONS),
    include_confidence: bool = True,
) -> pl.DataFrame:
    """Evaluate intermediate and factor Polars ``pl.Expr`` columns over a standardized wide panel."""
    out = panel.sort(["issuer_id", "period"]) if {"issuer_id", "period"} <= set(panel.columns) else panel
    needed: list[str] = []

    def _schedule(name: str) -> None:
        if name in needed or name not in PANEL_EXPRESSIONS:
            return
        for dep in consumed_columns(PANEL_EXPRESSIONS[name]):
            if dep in PANEL_EXPRESSIONS:
                _schedule(dep)
        needed.append(name)

    for target in targets:
        _schedule(target)

    for name in needed:
        expr = PANEL_EXPRESSIONS[name]
        missing_cols = [
            col for col in consumed_columns(expr) if col not in out.columns and col not in PANEL_EXPRESSIONS
        ]
        if missing_cols:
            out = out.with_columns([pl.lit(None, dtype=pl.Float64).alias(col) for col in missing_cols])
        out = out.with_columns(expr.alias(name))
        if include_confidence:
            leaf_cols = [col for col in leaf_consumed_columns(name) if f"conf__{col}" in out.columns]
            shifts = (1, 2, 3) if name == "peg" else ()
            if leaf_cols:
                conf = confidence_expr(leaf_cols, window_shifts=shifts)
                out = out.with_columns(
                    pl.when(pl.col(name).is_not_null()).then(conf).otherwise(0.0).alias(f"conf__{name}")
                )
    return out


# ---------------------------------------------------------------------------
# 2. Node / Tree Metadata & Exact 34-Digit Decimal Evaluation
# ---------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _sorted_classes(values: frozenset[IssuerClass]) -> list[str]:
    return sorted(value.value for value in values)


_ISSUER_CLASS_VALUES = frozenset(member.value for member in IssuerClass)


class NodeKind(StrEnum):
    #: A captured metric, fused by the metric registry.
    INPUT = "input"
    #: A versioned definition parameter (the risk-free rate); not an observation.
    PARAMETER = "parameter"
    #: Computed from other nodes by exactly one decomposition.
    DERIVED = "derived"
    #: A question with no value of its own; trees realize it.
    CONCEPT = "concept"


class AliasKind(StrEnum):
    """Typed aliases: the node id is the UUID; every name it goes by elsewhere is an alias of
    one kind (#877: entities are identified by UUIDs with typed aliases)."""

    #: The panel's own readable key.
    KEY = "key"
    #: `truealpha_contracts.metrics.METRICS` name.
    METRIC = "metric"
    #: `staging.strategy_backtest_inputs.input_key` spelling.
    INPUT_KEY = "input_key"
    #: Field of `factors.production_topt.ToptCoreSnapshotInput` that carries the value.
    TOPT_SNAPSHOT_FIELD = "topt_snapshot_field"
    #: Definition parameter name (`GppeV0Definition.risk_free_rate`).
    DEFINITION_PARAMETER = "definition_parameter"


class NodeAlias(_Frozen):
    kind: AliasKind
    value: str = Field(min_length=1)


class ProvenanceKind(StrEnum):
    METRIC_REGISTRY = "metric-registry"
    DEFINITION_PARAMETER = "definition-parameter"
    DERIVED = "derived"
    CONCEPT = "concept"


class Provenance(_Frozen):
    """Where a node's value comes from, at the level a factor may know it: a registry entry,
    a definition parameter, or the panel itself. Never a vendor (init.md rule 3)."""

    kind: ProvenanceKind
    reference: str = Field(min_length=1)
    note: str = ""

    @model_validator(mode="after")
    def _registered(self) -> Self:
        if self.kind is ProvenanceKind.METRIC_REGISTRY and self.reference not in METRICS:
            raise ValueError(f"provenance names {self.reference!r}, which the metric registry does not declare")
        return self


class MetricNode(_Frozen):
    node_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: NodeKind
    definition: str = Field(min_length=1)
    #: None only for a concept.
    unit: UnitFamily | None
    period: PeriodSemantics
    applicability: frozenset[IssuerClass] = Field(min_length=1)
    #: One policy per applicable class. A concept declares none.
    sign_policy: Mapping[IssuerClass, SignPolicy]
    provenance: Provenance
    aliases: tuple[NodeAlias, ...] = ()
    #: Optional coalesce default when a captured input is absent (None). Only valid on INPUT nodes.
    #: Absent from `model_dump` when None so existing tree hashes remain unchanged.
    default_when_missing: Decimal | None = None

    @field_serializer("applicability")
    def _dump_applicability(self, values: frozenset[IssuerClass]) -> list[str]:
        return _sorted_classes(values)

    @model_serializer(mode="wrap")
    def _omit_absent_default(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.default_when_missing is None:
            data.pop("default_when_missing", None)
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind is NodeKind.CONCEPT:
            if self.unit is not None or self.sign_policy:
                raise ValueError(f"concept {self.key} carries no value, so no unit or sign policy")
            if self.period is not PeriodSemantics.NONE:
                raise ValueError(f"concept {self.key} describes no period")
        else:
            if self.unit is None:
                raise ValueError(f"node {self.key} must declare its unit")
            if set(self.sign_policy) != set(self.applicability):
                raise ValueError(f"node {self.key} must declare a sign policy for exactly its applicable classes")
        if self.default_when_missing is not None:
            if self.kind is not NodeKind.INPUT:
                raise ValueError(f"node {self.key}: default_when_missing is only valid on INPUT nodes")
            if not self.default_when_missing.is_finite():
                raise ValueError(f"node {self.key}: default_when_missing must be finite")
        if AliasKind.KEY in {alias.kind for alias in self.aliases}:
            raise ValueError(f"node {self.key}: the key alias is implied, not declared")
        return self

    def alias(self, kind: AliasKind) -> str | None:
        if kind is AliasKind.KEY:
            return self.key
        values = [alias.value for alias in self.aliases if alias.kind is kind]
        if len(values) > 1:
            raise ValueError(f"node {self.key} has several {kind} aliases")
        return values[0] if values else None


FormulaId = Literal["identity", "sum", "difference", "product", "ratio", "non_negative"]


def _identity(operands: Sequence[Decimal]) -> Decimal | None:
    (value,) = operands
    return value


def _sum(operands: Sequence[Decimal]) -> Decimal | None:
    return reduce(lambda left, right: left + right, operands)


def _difference(operands: Sequence[Decimal]) -> Decimal | None:
    minuend, subtrahend = operands
    return minuend - subtrahend


def _product(operands: Sequence[Decimal]) -> Decimal | None:
    return reduce(lambda left, right: left * right, operands)


def _ratio(operands: Sequence[Decimal]) -> Decimal | None:
    numerator, denominator = operands
    return None if denominator == 0 else numerator / denominator


def _non_negative(operands: Sequence[Decimal]) -> Decimal | None:
    """The operand when it is not negative; undefined (None) when it is. Zero passes."""
    (value,) = operands
    return None if value < 0 else value


#: Why a formula that returns None leaves its output undefined. `evaluate` reports the reason by node.
UNDEFINED_REASONS: Mapping[str, str] = {"ratio": "zero_denominator", "non_negative": "negative_operand"}

#: (formula id, version) -> (operand arity or None for "two or more", function).
FORMULAS: Mapping[tuple[str, int], tuple[int | None, Callable[[Sequence[Decimal]], Decimal | None]]] = {
    ("identity", 1): (1, _identity),
    ("sum", 1): (None, _sum),
    ("difference", 1): (2, _difference),
    ("product", 1): (None, _product),
    ("ratio", 1): (2, _ratio),
    ("non_negative", 1): (1, _non_negative),
}


class Decomposition(_Frozen):
    """A typed edge: `output` = formula(operands), with the operands bound per issuer class."""

    output: str
    formula_id: FormulaId
    formula_version: int = Field(ge=1)
    operands: Mapping[IssuerClass, tuple[str, ...]] = Field(min_length=1)

    @model_validator(mode="after")
    def _arity(self) -> Self:
        spec = FORMULAS.get((self.formula_id, self.formula_version))
        if spec is None:
            raise ValueError(f"{self.output}: formula {self.formula_id}:v{self.formula_version} is not registered")
        arity = spec[0]
        for issuer_class, operands in self.operands.items():
            if (arity is not None and len(operands) != arity) or (arity is None and len(operands) < 2):
                raise ValueError(
                    f"{self.output}: {self.formula_id}:v{self.formula_version} cannot take {len(operands)} "
                    f"operand(s) for {issuer_class}"
                )
        return self


class MetricTree(_Frozen):
    """A registered, versioned set of decompositions under one root."""

    tree_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    version: str = Field(min_length=1)
    realizes: str
    root: str
    applicability: frozenset[IssuerClass] = Field(min_length=1)
    decompositions: tuple[Decomposition, ...] = Field(min_length=1)
    decimal_precision: int = Field(ge=1)
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"

    @field_serializer("applicability")
    def _dump_applicability(self, values: frozenset[IssuerClass]) -> list[str]:
        return _sorted_classes(values)

    def decomposition_of(self, key: str) -> Decomposition | None:
        matches = [item for item in self.decompositions if item.output == key]
        return matches[0] if matches else None

    def context(self) -> Context:
        return Context(prec=self.decimal_precision, rounding=ROUND_HALF_EVEN)


class Forest(_Frozen):
    """Every registered node and tree. Validates the structure once, at import."""

    nodes: tuple[MetricNode, ...]
    trees: tuple[MetricTree, ...]

    @model_validator(mode="after")
    def _structure(self) -> Self:
        by_key = {node.key: node for node in self.nodes}
        if len(by_key) != len(self.nodes):
            raise ValueError("node keys must be unique")
        if len({node.node_id for node in self.nodes} | {tree.tree_id for tree in self.trees}) != len(self.nodes) + len(
            self.trees
        ):
            raise ValueError("node and tree UUIDs must be unique across the forest")
        if len({(tree.key, tree.version) for tree in self.trees}) != len(self.trees):
            raise ValueError("a tree key and version identify one tree")
        seen: dict[str, Decomposition] = {}
        for tree in self.trees:
            self._validate_tree(tree, by_key)
            for decomposition in tree.decompositions:
                prior = seen.setdefault(decomposition.output, decomposition)
                if prior != decomposition:
                    raise ValueError(
                        f"{decomposition.output} is decomposed two ways; a different formula is a different node"
                    )
        for node in self.nodes:
            if node.kind is NodeKind.DERIVED and node.key not in seen:
                raise ValueError(f"derived node {node.key} has no decomposition")
            if node.kind is not NodeKind.DERIVED and node.key in seen:
                raise ValueError(f"{node.kind} node {node.key} cannot be the output of a decomposition")
        return self

    @staticmethod
    def _validate_decomposition_operands(
        tree: MetricTree,
        decomposition: Decomposition,
        by_key: Mapping[str, MetricNode],
    ) -> None:
        output = by_key.get(decomposition.output)
        if output is None or output.kind is not NodeKind.DERIVED:
            raise ValueError(f"tree {tree.key}: {decomposition.output} is not a registered derived node")
        if not set(decomposition.operands) <= (set(tree.applicability) & set(output.applicability)):
            raise ValueError(
                f"tree {tree.key}: {decomposition.output} must bind operands for every class within its applicability"
            )
        for issuer_class, operands in decomposition.operands.items():
            for operand in operands:
                node = by_key.get(operand)
                if node is None or node.kind is NodeKind.CONCEPT:
                    raise ValueError(f"tree {tree.key}: operand {operand} is not a registered valued node")
                if issuer_class not in node.applicability:
                    raise ValueError(f"tree {tree.key}: {operand} does not apply to {issuer_class}")
                if node.kind is NodeKind.DERIVED and tree.decomposition_of(operand) is None:
                    raise ValueError(f"tree {tree.key}: {operand} is derived but not decomposed in this tree")

    @staticmethod
    def _validate_tree(tree: MetricTree, by_key: Mapping[str, MetricNode]) -> None:
        concept = by_key.get(tree.realizes)
        if concept is None or concept.kind is not NodeKind.CONCEPT:
            raise ValueError(f"tree {tree.key} must realize a registered concept")
        if len({item.output for item in tree.decompositions}) != len(tree.decompositions):
            raise ValueError(f"tree {tree.key} decomposes a node twice")
        root = by_key.get(tree.root)
        if root is None or root.kind is not NodeKind.DERIVED:
            raise ValueError(f"tree {tree.key}: root {tree.root} is not a registered derived node")
        root_decomp = tree.decomposition_of(tree.root)
        if root_decomp is None:
            raise ValueError(f"tree {tree.key}: root {tree.root} is not decomposed in this tree")
        if set(root_decomp.operands) != set(tree.applicability):
            raise ValueError(f"tree {tree.key}: {tree.root} must bind operands for every class")
        for decomposition in tree.decompositions:
            Forest._validate_decomposition_operands(tree, decomposition, by_key)
        reached: set[str] = set()

        def visit_class(key: str, issuer_class: IssuerClass, path: tuple[str, ...]) -> None:
            if key in path:
                raise ValueError(f"tree {tree.key} is cyclic at {' -> '.join((*path, key))}")
            decomposition = tree.decomposition_of(key)
            if decomposition is None:
                return
            if issuer_class not in decomposition.operands:
                raise ValueError(f"tree {tree.key}: {key} must bind operands for every class that reaches it")
            reached.add(key)
            for operand in decomposition.operands[issuer_class]:
                visit_class(operand, issuer_class, (*path, key))

        for issuer_class in tree.applicability:
            visit_class(tree.root, issuer_class, ())
        unreachable = {item.output for item in tree.decompositions} - reached
        if unreachable:
            raise ValueError(f"tree {tree.key}: {sorted(unreachable)} are not reachable from the root")

    def node(self, key: str) -> MetricNode:
        for node in self.nodes:
            if node.key == key:
                return node
        raise KeyError(key)

    def tree(self, key: str, version: str) -> MetricTree:
        for tree in self.trees:
            if (tree.key, tree.version) == (key, version):
                return tree
        raise KeyError((key, version))

    def input_nodes(self) -> tuple[MetricNode, ...]:
        """Every registered INPUT node of the panel, in registration order."""
        return tuple(node for node in self.nodes if node.kind is NodeKind.INPUT)

    def snapshot_field_nodes(self) -> Mapping[str, MetricNode]:
        """`topt_snapshot_field` alias -> `MetricNode` for every registered input node with that alias."""
        out: dict[str, MetricNode] = {}
        for node in self.input_nodes():
            field_name = node.alias(AliasKind.TOPT_SNAPSHOT_FIELD)
            if field_name is not None:
                out[field_name] = node
        return out

    def tree_sha256(self, tree: MetricTree) -> str:
        """The tree's identity: its own fields plus every node it reaches."""
        keys = sorted({tree.root, *leaf_and_derived_keys(tree)})
        return canonical_sha256(
            {
                "tree": tree.model_dump(mode="json"),
                "nodes": [self.node(key).model_dump(mode="json") for key in keys],
                "concept": self.node(tree.realizes).model_dump(mode="json"),
            }
        )


def leaf_and_derived_keys(tree: MetricTree) -> set[str]:
    keys: set[str] = set()
    for decomposition in tree.decompositions:
        keys.add(decomposition.output)
        for operands in decomposition.operands.values():
            keys.update(operands)
    return keys


def required_inputs(forest: Forest, tree: MetricTree, issuer_class: IssuerClass) -> tuple[MetricNode, ...]:
    """The input and parameter nodes an issuer of this class needs, in first-visit order."""
    if issuer_class not in tree.applicability:
        raise ValueError(f"tree {tree.key} does not apply to {issuer_class}")
    ordered: dict[str, MetricNode] = {}

    def visit(key: str) -> None:
        decomposition = tree.decomposition_of(key)
        if decomposition is None:
            ordered.setdefault(key, forest.node(key))
            return
        for operand in decomposition.operands[issuer_class]:
            visit(operand)

    visit(tree.root)
    return tuple(ordered.values())


class TreeEvaluation(_Frozen):
    tree_key: str
    tree_version: str
    issuer_class: IssuerClass
    #: Every node the evaluation touched; None where the node is undefined.
    values: Mapping[str, Decimal | None]
    #: node key -> why it is undefined (`missing_input`, `zero_denominator`, `negative_operand`, `undefined_operand`).
    undefined: Mapping[str, str] = {}


def evaluate(
    forest: Forest,
    tree: MetricTree,
    *,
    issuer_class: IssuerClass,
    inputs: Mapping[str, Decimal | None],
) -> TreeEvaluation:
    """Evaluate `tree` for one issuer of `issuer_class` in the tree's 34-digit Decimal context."""
    values: dict[str, Decimal | None] = {}
    undefined: dict[str, str] = {}

    def resolve(key: str) -> Decimal | None:
        if key in values:
            return values[key]
        decomposition = tree.decomposition_of(key)
        if decomposition is None:
            value = inputs.get(key)
            if value is None:
                node = forest.node(key)
                if node.default_when_missing is not None:
                    value = node.default_when_missing
            values[key] = value
            if value is None:
                undefined[key] = "missing_input"
            return value
        operands = [resolve(operand) for operand in decomposition.operands[issuer_class]]
        present = [operand for operand in operands if operand is not None]
        if len(present) != len(operands):
            values[key] = None
            undefined[key] = "undefined_operand"
            return None
        _arity, function = FORMULAS[(decomposition.formula_id, decomposition.formula_version)]
        with localcontext(tree.context()):
            value = function(present)
        values[key] = value
        if value is None:
            undefined[key] = UNDEFINED_REASONS[decomposition.formula_id]
        return value

    if issuer_class not in tree.applicability:
        raise ValueError(f"tree {tree.key} does not apply to {issuer_class}")
    for node in required_inputs(forest, tree, issuer_class):
        resolve(node.key)
    resolve(tree.root)
    return TreeEvaluation(
        tree_key=tree.key,
        tree_version=tree.version,
        issuer_class=issuer_class,
        values=values,
        undefined=undefined,
    )


class SignFinding(_Frozen):
    """A negative value judged against its node's declared policy."""

    node_key: str
    node_id: UUID
    issuer_class: IssuerClass | None
    policy: SignPolicy | None
    value: Decimal
    subject: str

    @property
    def violation(self) -> bool:
        """A negative value the node forbids, or a value published for a class the node does not declare."""
        return self.policy is None or self.policy is SignPolicy.MUST_BE_NON_NEGATIVE

    @property
    def signal(self) -> bool:
        return self.policy is SignPolicy.SIGN_IS_SIGNAL


def judge_sign(node: MetricNode, *, issuer_class: str, value: Decimal | None, subject: str) -> SignFinding | None:
    """The finding for one published value, or None when there is nothing to say."""
    if value is None:
        return None
    declared = IssuerClass(issuer_class) if issuer_class in _ISSUER_CLASS_VALUES else None
    policy = node.sign_policy.get(declared) if declared is not None else None
    if policy is not None and (value >= 0 or policy is SignPolicy.MAY_BE_NEGATIVE):
        return None
    return SignFinding(
        node_key=node.key,
        node_id=node.node_id,
        issuer_class=declared,
        policy=policy,
        value=value,
        subject=subject,
    )


# ---------------------------------------------------------------------------
# 3. Registered Nodes and GPPE Panel Specifications
# ---------------------------------------------------------------------------

_EVERY_CLASS = ALL_ISSUER_CLASSES
_NON_BANK = frozenset({IssuerClass.NON_FINANCIAL, IssuerClass.INSURANCE})
_FINANCIAL = frozenset({IssuerClass.FINANCIAL})


def _policy(policy: SignPolicy, classes: frozenset[IssuerClass] = _EVERY_CLASS) -> dict[IssuerClass, SignPolicy]:
    return {issuer_class: policy for issuer_class in classes}


def _financial_policy(policy: SignPolicy) -> dict[IssuerClass, SignPolicy]:
    return _policy(policy, _FINANCIAL)


LABOR_EFFICIENCY = MetricNode(
    node_id=UUID("8f78ed40-9625-58eb-a866-4fd42015b43d"),
    key="labor_efficiency",
    kind=NodeKind.CONCEPT,
    definition=(
        "How much real profit one employee produces (init.md §0 question 1, module 2). Realized by "
        "several trees: capital-adjusted gross profit per employee today; the operating + financial "
        "decomposition and the labor-cost variant (#59 item 2) next."
    ),
    unit=None,
    period=PeriodSemantics.NONE,
    applicability=_EVERY_CLASS,
    sign_policy={},
    provenance=Provenance(kind=ProvenanceKind.CONCEPT, reference="init.md#7-module-2"),
)

GROSS_PROFIT = MetricNode(
    node_id=UUID("c682a9c8-c41d-5262-899b-f2d62000e10f"),
    key="gross_profit",
    kind=NodeKind.INPUT,
    definition="Reported gross profit for the fiscal year (revenue minus cost of revenue as the issuer reports it).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_NON_BANK,
    sign_policy=_policy(SignPolicy.MAY_BE_NEGATIVE, _NON_BANK),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="gross_profit"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="gross_profit"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="gross_profit"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="gross_profit"),
    ),
)

PRE_PROVISION_PROFIT = MetricNode(
    node_id=UUID("75bf2b7a-1600-5bcf-9d86-648fb16a18ae"),
    key="pre_provision_profit",
    kind=NodeKind.INPUT,
    definition="A bank's pre-provision net revenue for the fiscal year: net revenue minus noninterest expense.",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=frozenset({IssuerClass.FINANCIAL}),
    sign_policy=_policy(SignPolicy.MAY_BE_NEGATIVE, frozenset({IssuerClass.FINANCIAL})),
    provenance=Provenance(
        kind=ProvenanceKind.METRIC_REGISTRY,
        reference="pre_provision_profit",
    ),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="pre_provision_profit"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="pre_provision_profit"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="pre_provision_profit"),
    ),
)

TOTAL_ASSETS = MetricNode(
    node_id=UUID("c1d19aea-eb4a-525e-b62c-2fc9efe3bf77"),
    key="total_assets",
    kind=NodeKind.INPUT,
    definition="Total assets on the balance sheet at the fiscal year end.",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="total_assets"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="total_assets"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="total_assets"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="total_assets"),
    ),
)

EMPLOYEES_TOTAL = MetricNode(
    node_id=UUID("c7db1ab2-d0fa-5437-800e-34f3be376420"),
    key="employees_total",
    kind=NodeKind.INPUT,
    definition="Company-wide total employees as of the date the latest annual filing states (STANDARDS employees_total).",
    unit=UnitFamily.COUNT,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="employees_total"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="employees_total"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="headcount"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="headcount"),
    ),
)

RISK_FREE_RATE = MetricNode(
    node_id=UUID("c6ae2f22-4b96-5610-8482-71c6ccd132ed"),
    key="risk_free_rate",
    kind=NodeKind.PARAMETER,
    definition="The annual risk-free hurdle, 3-month US T-bill (#59 v0 default); a versioned definition parameter.",
    unit=UnitFamily.RATIO,
    period=PeriodSemantics.DEFINITIONAL,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DEFINITION_PARAMETER, reference="risk_free_rate"),
    aliases=(NodeAlias(kind=AliasKind.DEFINITION_PARAMETER, value="risk_free_rate"),),
)

OPERATING_GROSS_PROFIT = MetricNode(
    node_id=UUID("34a38955-e69e-559b-a483-91f81b9a6ab0"),
    key="operating_gross_profit",
    kind=NodeKind.DERIVED,
    definition=(
        "The issuer class's operating numerator: gross profit, or a bank's pre-provision profit where "
        "cost of revenue is not reported (init.md rule 17's per-class proxy)."
    ),
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe"),
)

CAPITAL_CHARGE = MetricNode(
    node_id=UUID("6b4ce481-f045-5503-9675-921160e2bcd2"),
    key="capital_charge",
    kind=NodeKind.DERIVED,
    definition="The risk-free return on the charged capital base: base × rate, bound per issuer class.",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe"),
)

CAPITAL_ADJUSTED_GROSS_PROFIT = MetricNode(
    node_id=UUID("c3428286-eb85-5107-a32a-b8757ad5b340"),
    key="capital_adjusted_gross_profit",
    kind=NodeKind.DERIVED,
    definition="Real profit v0 (#59): the operating numerator minus the capital charge.",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe"),
    aliases=(NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="capital_adjusted_gross_profit"),),
)

GPPE = MetricNode(
    node_id=UUID("6143cf17-ceba-5f78-a2d3-a7cc423c49a7"),
    key="gppe_uniform_charge_v0",
    kind=NodeKind.DERIVED,
    definition=(
        "gppe_uniform_charge_v0 (#1176, #59, #394): capital-adjusted gross profit per employee, real "
        "profit v0 divided by headcount, with total assets as the capital base for every class. "
        "Negative means the issuer earns less than the risk-free return on its total assets, which "
        "the definition ranks as a low signal."
    ),
    unit=UnitFamily.PER_EMPLOYEE,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe"),
)

GPPE_UNIFORM_CHARGE_V0 = GPPE

STOCKHOLDERS_EQUITY = MetricNode(
    node_id=UUID("0aa53e41-0d12-5f11-92c4-fe6225166328"),
    key="stockholders_equity",
    kind=NodeKind.INPUT,
    definition="Total stockholders' equity at the fiscal year end, preferred equity included (#1176).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="stockholders_equity"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="stockholders_equity"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="stockholders_equity"),
    ),
)

PREFERRED_STOCK_VALUE = MetricNode(
    node_id=UUID("ddf51b59-2c0e-52f0-8798-a555673c9bf6"),
    key="preferred_stock_value",
    kind=NodeKind.INPUT,
    definition=(
        "Preferred stock carrying value at the fiscal year end (#1176 follow-up). Deducted from "
        "stockholders' equity for tangible common equity. Never zero-filled when absent."
    ),
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="preferred_stock_value"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="preferred_stock_value"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="preferred_stock_value"),
    ),
)

GOODWILL = MetricNode(
    node_id=UUID("738c81a6-fd15-53ca-ad2f-679a2b700056"),
    key="goodwill",
    kind=NodeKind.INPUT,
    definition="Goodwill carrying amount at the fiscal year end (#1176).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="goodwill"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="goodwill"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="goodwill"),
    ),
)

INTANGIBLE_ASSETS = MetricNode(
    node_id=UUID("14313d73-f5f7-5a47-a087-ed9ce71ebd88"),
    key="intangible_assets_net_excluding_goodwill",
    kind=NodeKind.INPUT,
    definition="Intangible assets net of amortization, excluding goodwill, at the fiscal year end (#1176).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="intangible_assets_net_excluding_goodwill"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="intangible_assets_net_excluding_goodwill"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="intangible_assets_net_excluding_goodwill"),
    ),
)

INTANGIBLE_ASSETS_EX_GOODWILL = INTANGIBLE_ASSETS

TANGIBLE_DEDUCTIONS = MetricNode(
    node_id=UUID("805d8f77-10aa-5dc4-b2bd-f47647e00593"),
    key="tangible_deductions",
    kind=NodeKind.DERIVED,
    definition="Equity that is not common or not tangible: preferred stock, goodwill and intangible assets (#1176).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
)

TANGIBLE_COMMON_EQUITY = MetricNode(
    node_id=UUID("8c346339-0ca5-588a-90d4-e67c96db8ecb"),
    key="tangible_common_equity",
    kind=NodeKind.DERIVED,
    definition="Measured tangible common equity: stockholders' equity minus tangible deductions (#1176, #1176 follow-up).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
)

CHARGED_TANGIBLE_COMMON_EQUITY = MetricNode(
    node_id=UUID("2cc2205b-4649-51a4-9f21-9e64b238d162"),
    key="charged_tangible_common_equity",
    kind=NodeKind.DERIVED,
    definition=(
        "The tangible common equity that the capital charge applies to (#1518 review): the measured value when it "
        "is not negative, undefined when it is. A negative equity would turn the charge into a credit, and the "
        "real profit would grow with the loss of equity. Zero gives a zero charge. Working choice of the delivery "
        "session, not an owner approval."
    ),
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
)

CAPITAL_CHARGE_TCE = MetricNode(
    node_id=UUID("588a8f4d-b301-5a2e-b0f3-de6743d60af4"),
    key="capital_charge_tce",
    kind=NodeKind.DERIVED,
    definition="The risk-free return on measured tangible common equity: TCE x rate (#1176). TCE is not negative here (#1518).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
)

CAPITAL_ADJUSTED_TCE = MetricNode(
    node_id=UUID("57204502-7b9c-5671-b392-424b4368d9f1"),
    key="capital_adjusted_tce",
    kind=NodeKind.DERIVED,
    definition="Real profit on the tangible base (#1176): the operating numerator minus the TCE charge.",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
)

GPPE_BANKING_TCE_V1 = MetricNode(
    node_id=UUID("582c62d6-4873-5ff4-ad91-eba1d571fc4c"),
    key="gppe_banking_tce_v1",
    kind=NodeKind.DERIVED,
    definition=(
        "gppe_banking_tce_v1 (#1176, #1108): labor efficiency for FINANCIAL issuers. Real profit on "
        "measured tangible common equity, divided by headcount. Missing TCE gives no value; it never "
        "falls back to total assets or a fixed equity share."
    ),
    unit=UnitFamily.PER_EMPLOYEE,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
)

GPPE_BANKING_TCE_TREE = MetricTree(
    tree_id=UUID("96fd6d4a-8983-5caf-b647-360122569d00"),
    key="gppe_banking_tce",
    version="v2",
    realizes=LABOR_EFFICIENCY.key,
    root=GPPE_BANKING_TCE_V1.key,
    applicability=_FINANCIAL,
    decompositions=(
        Decomposition(
            output=GPPE_BANKING_TCE_V1.key,
            formula_id="ratio",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (CAPITAL_ADJUSTED_TCE.key, EMPLOYEES_TOTAL.key)},
        ),
        Decomposition(
            output=CAPITAL_ADJUSTED_TCE.key,
            formula_id="difference",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (PRE_PROVISION_PROFIT.key, CAPITAL_CHARGE_TCE.key)},
        ),
        Decomposition(
            output=CAPITAL_CHARGE_TCE.key,
            formula_id="product",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (CHARGED_TANGIBLE_COMMON_EQUITY.key, RISK_FREE_RATE.key)},
        ),
        Decomposition(
            output=CHARGED_TANGIBLE_COMMON_EQUITY.key,
            formula_id="non_negative",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (TANGIBLE_COMMON_EQUITY.key,)},
        ),
        Decomposition(
            output=TANGIBLE_COMMON_EQUITY.key,
            formula_id="difference",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (STOCKHOLDERS_EQUITY.key, TANGIBLE_DEDUCTIONS.key)},
        ),
        Decomposition(
            output=TANGIBLE_DEDUCTIONS.key,
            formula_id="sum",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (PREFERRED_STOCK_VALUE.key, GOODWILL.key, INTANGIBLE_ASSETS.key)},
        ),
    ),
    decimal_precision=34,
)

GPPE_V0_TREE = MetricTree(
    tree_id=UUID("ef13b940-b695-5f5f-846a-a36face5cc3e"),
    key="gppe",
    version="production-topt-v0.2.0",
    realizes=LABOR_EFFICIENCY.key,
    root=GPPE.key,
    applicability=_EVERY_CLASS,
    decompositions=(
        Decomposition(
            output=GPPE.key,
            formula_id="ratio",
            formula_version=1,
            operands={c: (CAPITAL_ADJUSTED_GROSS_PROFIT.key, EMPLOYEES_TOTAL.key) for c in _EVERY_CLASS},
        ),
        Decomposition(
            output=CAPITAL_ADJUSTED_GROSS_PROFIT.key,
            formula_id="difference",
            formula_version=1,
            operands={c: (OPERATING_GROSS_PROFIT.key, CAPITAL_CHARGE.key) for c in _EVERY_CLASS},
        ),
        Decomposition(
            output=CAPITAL_CHARGE.key,
            formula_id="product",
            formula_version=1,
            operands={c: (TOTAL_ASSETS.key, RISK_FREE_RATE.key) for c in _EVERY_CLASS},
        ),
        Decomposition(
            output=OPERATING_GROSS_PROFIT.key,
            formula_id="identity",
            formula_version=1,
            operands={
                IssuerClass.NON_FINANCIAL: (GROSS_PROFIT.key,),
                IssuerClass.INSURANCE: (GROSS_PROFIT.key,),
                IssuerClass.FINANCIAL: (PRE_PROVISION_PROFIT.key,),
            },
        ),
    ),
    decimal_precision=34,
)

FINANCIAL_ASSETS = MetricNode(
    node_id=UUID("31e866ea-79cd-5bc5-8203-04fc40e3240f"),
    key="financial_assets",
    kind=NodeKind.INPUT,
    definition="Cash and marketable investment balances on the balance sheet at the fiscal year end (#528).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.MUST_BE_NON_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="financial_assets"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="financial_assets"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="financial_assets"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="financial_assets"),
    ),
)

FINANCIAL_RETURNS = MetricNode(
    node_id=UUID("c7f19762-975d-52b1-86db-360f8f7958fc"),
    key="financial_returns",
    kind=NodeKind.INPUT,
    definition=(
        "Investment income, dividends, and investment gains for the fiscal year (#528). Coalesces to 0 "
        "when an issuer holds no marketable securities or reports no separate investment return line."
    ),
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_NON_BANK,
    sign_policy=_policy(SignPolicy.MAY_BE_NEGATIVE, _NON_BANK),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="financial_returns"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="financial_returns"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="financial_returns"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="financial_returns"),
    ),
    default_when_missing=Decimal("0"),
)

BANK_NET_REVENUE = MetricNode(
    node_id=UUID("4e99982c-03e2-510d-84fa-b7d8e988783e"),
    key="bank_net_revenue",
    kind=NodeKind.INPUT,
    definition="A bank's net revenue for the fiscal year: net interest income plus noninterest income (#1518).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="bank_net_revenue"),
    aliases=(
        NodeAlias(kind=AliasKind.METRIC, value="bank_net_revenue"),
        NodeAlias(kind=AliasKind.INPUT_KEY, value="bank_net_revenue"),
        NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="bank_net_revenue"),
    ),
)

TOTAL_GROSS_RETURN = MetricNode(
    node_id=UUID("1519c717-370f-5b3f-a8fd-d37fc895d160"),
    key="total_gross_return",
    kind=NodeKind.DERIVED,
    definition="Total gross return before capital charge (#528, #1518): operating gross profit plus financial returns.",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_NON_BANK,
    sign_policy=_policy(SignPolicy.MAY_BE_NEGATIVE, _NON_BANK),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_operating_financial_v1"),
)

CAPITAL_ADJUSTED_OPERATING_FINANCIAL = MetricNode(
    node_id=UUID("5e4a472f-71cd-57b3-90e6-21a5b94cc4cb"),
    key="capital_adjusted_operating_financial",
    kind=NodeKind.DERIVED,
    definition=(
        "Capital-adjusted operating and financial return (#528, #1518): total gross return minus "
        "total-assets capital charge for non-banks, or bank net revenue minus TCE capital charge for banks."
    ),
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_operating_financial_v1"),
)

GPPE_OPERATING_FINANCIAL_V1 = MetricNode(
    node_id=UUID("25ff84c7-8f4d-515c-a6bb-e33cf52a2ef1"),
    key="gppe_operating_financial_v1",
    kind=NodeKind.DERIVED,
    definition=(
        "gppe_operating_financial_v1 (#528, #1518): capital-adjusted operating and financial return "
        "per employee across all issuer classes."
    ),
    unit=UnitFamily.PER_EMPLOYEE,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_operating_financial_v1"),
)

GPPE_OPERATING_FINANCIAL_TREE = MetricTree(
    tree_id=UUID("7183c828-7252-5591-81a8-99fd05737995"),
    key="gppe_operating_financial",
    version="1.0.0",
    realizes=LABOR_EFFICIENCY.key,
    root=GPPE_OPERATING_FINANCIAL_V1.key,
    applicability=_EVERY_CLASS,
    decompositions=(
        Decomposition(
            output=GPPE_OPERATING_FINANCIAL_V1.key,
            formula_id="ratio",
            formula_version=1,
            operands={c: (CAPITAL_ADJUSTED_OPERATING_FINANCIAL.key, EMPLOYEES_TOTAL.key) for c in _EVERY_CLASS},
        ),
        Decomposition(
            output=CAPITAL_ADJUSTED_OPERATING_FINANCIAL.key,
            formula_id="difference",
            formula_version=1,
            operands={
                IssuerClass.NON_FINANCIAL: (TOTAL_GROSS_RETURN.key, CAPITAL_CHARGE.key),
                IssuerClass.INSURANCE: (TOTAL_GROSS_RETURN.key, CAPITAL_CHARGE.key),
                IssuerClass.FINANCIAL: (BANK_NET_REVENUE.key, CAPITAL_CHARGE_TCE.key),
            },
        ),
        Decomposition(
            output=TOTAL_GROSS_RETURN.key,
            formula_id="sum",
            formula_version=1,
            operands={
                IssuerClass.NON_FINANCIAL: (GROSS_PROFIT.key, FINANCIAL_RETURNS.key),
                IssuerClass.INSURANCE: (GROSS_PROFIT.key, FINANCIAL_RETURNS.key),
            },
        ),
        Decomposition(
            output=CAPITAL_CHARGE.key,
            formula_id="product",
            formula_version=1,
            operands={c: (TOTAL_ASSETS.key, RISK_FREE_RATE.key) for c in _EVERY_CLASS},
        ),
        Decomposition(
            output=CAPITAL_CHARGE_TCE.key,
            formula_id="product",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (CHARGED_TANGIBLE_COMMON_EQUITY.key, RISK_FREE_RATE.key)},
        ),
        Decomposition(
            output=CHARGED_TANGIBLE_COMMON_EQUITY.key,
            formula_id="non_negative",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (TANGIBLE_COMMON_EQUITY.key,)},
        ),
        Decomposition(
            output=TANGIBLE_COMMON_EQUITY.key,
            formula_id="difference",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (STOCKHOLDERS_EQUITY.key, TANGIBLE_DEDUCTIONS.key)},
        ),
        Decomposition(
            output=TANGIBLE_DEDUCTIONS.key,
            formula_id="sum",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: (PREFERRED_STOCK_VALUE.key, GOODWILL.key, INTANGIBLE_ASSETS.key)},
        ),
    ),
    decimal_precision=34,
)

CANONICAL_NODES: tuple[MetricNode, ...] = (
    LABOR_EFFICIENCY,
    GROSS_PROFIT,
    PRE_PROVISION_PROFIT,
    TOTAL_ASSETS,
    EMPLOYEES_TOTAL,
    RISK_FREE_RATE,
    OPERATING_GROSS_PROFIT,
    CAPITAL_CHARGE,
    CAPITAL_ADJUSTED_GROSS_PROFIT,
    GPPE,
    STOCKHOLDERS_EQUITY,
    PREFERRED_STOCK_VALUE,
    GOODWILL,
    INTANGIBLE_ASSETS,
    TANGIBLE_DEDUCTIONS,
    TANGIBLE_COMMON_EQUITY,
    CHARGED_TANGIBLE_COMMON_EQUITY,
    CAPITAL_CHARGE_TCE,
    CAPITAL_ADJUSTED_TCE,
    GPPE_BANKING_TCE_V1,
    FINANCIAL_ASSETS,
    FINANCIAL_RETURNS,
    BANK_NET_REVENUE,
    TOTAL_GROSS_RETURN,
    CAPITAL_ADJUSTED_OPERATING_FINANCIAL,
    GPPE_OPERATING_FINANCIAL_V1,
)

FOREST = Forest(
    nodes=CANONICAL_NODES,
    trees=(GPPE_V0_TREE, GPPE_BANKING_TCE_TREE, GPPE_OPERATING_FINANCIAL_TREE),
)

PUBLISHED_COLUMNS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "mart.topt_gppe_results": MappingProxyType(
            {
                "gppe": GPPE.key,
                "operating_efficiency": GPPE.key,
                "capital_adjusted_gross_profit": CAPITAL_ADJUSTED_GROSS_PROFIT.key,
            }
        ),
        "mart.topt_core_results": MappingProxyType(
            {
                "gppe": GPPE.key,
                "operating_efficiency": GPPE.key,
                "capital_adjusted_gross_profit": CAPITAL_ADJUSTED_GROSS_PROFIT.key,
            }
        ),
    }
)


def published_node(table: str, column: str) -> MetricNode:
    return FOREST.node(PUBLISHED_COLUMNS[table][column])


# ---------------------------------------------------------------------------
# 4. Strategy Definition Binding Helpers
# ---------------------------------------------------------------------------


class UnpinnedClassError(ValueError):
    """The definition pins no tree for the issuer class. A row of that class is unavailable, not an abort."""


def definition_pins(definition: LargeModelValueV0Definition) -> Mapping[str, LaborEfficiencyTreePin]:
    """The tree pins of ``definition``, by issuer class value. A definition with none raises."""
    pins = definition.labor_efficiency_tree_keys
    if pins is None:
        raise ValueError(
            f"definition {definition.definition_version} pins no labor-efficiency tree; it states a formula. "
            "A replay of that definition passes its pins by name (`uniform_tree_pins`)"
        )
    return pins


def bound_tree(
    definition: LargeModelValueV0Definition,
    issuer_class: IssuerClass,
    *,
    forest: Forest = FOREST,
    pins: Mapping[str, LaborEfficiencyTreePin] | None = None,
) -> MetricTree:
    """The registered tree that evaluates labor efficiency for ``issuer_class`` under ``definition``."""
    pinned = definition_pins(definition) if pins is None else pins
    pin = pinned.get(issuer_class.value)
    if pin is None:
        raise UnpinnedClassError(
            f"definition {definition.definition_version} pins no tree for the class {issuer_class.value}"
        )
    try:
        tree = forest.tree(pin.tree_key, pin.tree_version)
    except KeyError:
        raise ValueError(
            f"definition {definition.definition_version} pins tree {pin.tree_key} {pin.tree_version}, "
            "which the forest does not register"
        ) from None
    registered = forest.tree_sha256(tree)
    if registered != pin.tree_sha256:
        raise ValueError(
            f"definition {definition.definition_version} pins tree {pin.tree_key} {pin.tree_version} "
            f"with hash {pin.tree_sha256}, but the registered tree has hash {registered}"
        )
    if issuer_class not in tree.applicability:
        raise ValueError(f"tree {tree.key} {tree.version} does not apply to the class {issuer_class.value}")
    return tree


def capital_adjusted_node(tree: MetricTree, issuer_class: IssuerClass) -> str:
    """The node that holds the real profit after the capital charge: the numerator of the root ratio."""
    decomposition = tree.decomposition_of(tree.root)
    if decomposition is None or decomposition.formula_id != "ratio":
        raise ValueError(f"the root of tree {tree.key} {tree.version} is not a ratio: it has no capital-adjusted node")
    return decomposition.operands[issuer_class][0]


def operating_numerator_node(tree: MetricTree, issuer_class: IssuerClass, *, forest: Forest = FOREST) -> MetricNode:
    """The captured input that the numerator of the capital-adjusted profit reduces to (#1518)."""
    capital_adjusted = tree.decomposition_of(capital_adjusted_node(tree, issuer_class))
    if capital_adjusted is None or capital_adjusted.formula_id != "difference":
        raise ValueError(f"tree {tree.key} {tree.version}: the capital-adjusted node is not numerator minus charge")
    key = capital_adjusted.operands[issuer_class][0]
    while (chain := tree.decomposition_of(key)) is not None:
        if chain.formula_id == "identity":
            (key,) = chain.operands[issuer_class]
            continue
        if chain.formula_id == "sum":
            required = [
                operand for operand in chain.operands[issuer_class] if forest.node(operand).default_when_missing is None
            ]
            if len(required) == 1:
                (key,) = required
                continue
        raise ValueError(f"tree {tree.key} {tree.version}: the numerator {key} is not a captured input")
    return forest.node(key)


def uniform_tree_pins(forest: Forest = FOREST) -> dict[str, LaborEfficiencyTreePin]:
    """The pins of the frozen #21 replay (definition 1.0.0): the uniform total-assets tree for every class."""
    tree = forest.tree(GPPE_V0_TREE.key, GPPE_V0_TREE.version)
    pin = LaborEfficiencyTreePin(tree_key=tree.key, tree_version=tree.version, tree_sha256=forest.tree_sha256(tree))
    return {issuer_class.value: pin for issuer_class in IssuerClass}


def metric_of(node: MetricNode) -> str:
    """The `METRICS` name of a captured input node."""
    alias = node.alias(AliasKind.METRIC)
    if alias is not None:
        return alias
    if node.provenance.kind is ProvenanceKind.METRIC_REGISTRY:
        return node.provenance.reference
    return node.key


def input_key_of(node: MetricNode) -> str:
    """The strategy input key (`staging.strategy_backtest_inputs.input_key`) of a captured input node."""
    return node.alias(AliasKind.INPUT_KEY) or input_key_for_metric(metric_of(node))


__all__ = [
    "ALL_ISSUER_CLASSES",
    "BANK_NET_REVENUE",
    "CANONICAL_NODES",
    "CAPITAL_ADJUSTED_GROSS_PROFIT",
    "CAPITAL_ADJUSTED_OPERATING_FINANCIAL",
    "CAPITAL_ADJUSTED_TCE",
    "CAPITAL_CHARGE",
    "CAPITAL_CHARGE_TCE",
    "CHARGED_TANGIBLE_COMMON_EQUITY",
    "EMPLOYEES_TOTAL",
    "EXPR_CAPITAL_ADJUSTED_GROSS_PROFIT",
    "EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL",
    "EXPR_CAPITAL_ADJUSTED_TCE",
    "EXPR_CAPITAL_CHARGE",
    "EXPR_CAPITAL_CHARGE_TCE",
    "EXPR_CHARGED_TANGIBLE_COMMON_EQUITY",
    "EXPR_GPPE_BANKING_TCE_V1",
    "EXPR_GPPE_OPERATING_FINANCIAL_V1",
    "EXPR_GPPE_UNIFORM_CHARGE_V0",
    "EXPR_OPERATING_GROSS_PROFIT",
    "EXPR_PEG",
    "EXPR_POSITIVE_TANGIBLE_COMMON_EQUITY",
    "EXPR_PRICE_TO_GROSS_PROFIT",
    "EXPR_PRICE_TO_SALES",
    "EXPR_TANGIBLE_COMMON_EQUITY",
    "EXPR_TANGIBLE_DEDUCTIONS",
    "EXPR_TANGIBLE_DEDUCTIONS_FILL_NULL",
    "EXPR_TOTAL_GROSS_RETURN",
    "FACTOR_EXPRESSIONS",
    "FINANCIAL_ASSETS",
    "FINANCIAL_RETURNS",
    "FOREST",
    "FORMULAS",
    "GOODWILL",
    "GPPE",
    "GPPE_BANKING_TCE_TREE",
    "GPPE_BANKING_TCE_V1",
    "GPPE_OPERATING_FINANCIAL_TREE",
    "GPPE_OPERATING_FINANCIAL_V1",
    "GPPE_UNIFORM_CHARGE_V0",
    "GPPE_V0_TREE",
    "GROSS_PROFIT",
    "INTANGIBLE_ASSETS",
    "INTANGIBLE_ASSETS_EX_GOODWILL",
    "INTERMEDIATE_EXPRESSIONS",
    "LABOR_EFFICIENCY",
    "MINTED_NODE_IDS",
    "MINTED_TREE_IDS",
    "NAMESPACE_UUID",
    "OPERATING_GROSS_PROFIT",
    "PANEL_DECIMAL_CONTEXT",
    "PANEL_DECIMAL_PRECISION",
    "PANEL_DECIMAL_ROUNDING",
    "PANEL_EXPRESSIONS",
    "PANEL_INDEX_COLUMNS",
    "PHYSICALLY_NON_NEGATIVE",
    "PREFERRED_STOCK_VALUE",
    "PRE_PROVISION_PROFIT",
    "PUBLISHED_COLUMNS",
    "RISK_FREE_RATE",
    "STOCKHOLDERS_EQUITY",
    "TANGIBLE_COMMON_EQUITY",
    "TANGIBLE_DEDUCTIONS",
    "TOTAL_ASSETS",
    "TOTAL_GROSS_RETURN",
    "UNDEFINED_REASONS",
    "AliasKind",
    "Decomposition",
    "Forest",
    "FormulaId",
    "IssuerClass",
    "MetricNode",
    "MetricTree",
    "NodeAlias",
    "NodeKind",
    "PeriodSemantics",
    "Provenance",
    "ProvenanceKind",
    "SignFinding",
    "SignPolicy",
    "TreeEvaluation",
    "UnpinnedClassError",
    "bound_tree",
    "build_wide_panel",
    "capital_adjusted_node",
    "confidence_expr",
    "consumed_columns",
    "definition_pins",
    "evaluate",
    "evaluate_wide_panel",
    "input_key_of",
    "judge_sign",
    "leaf_and_derived_keys",
    "leaf_consumed_columns",
    "metric_of",
    "node_id_for",
    "operating_numerator_node",
    "published_node",
    "required_inputs",
    "tree_id_for",
    "uniform_tree_pins",
]
