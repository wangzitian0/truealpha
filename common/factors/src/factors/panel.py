"""Standardized wide panel and native Polars pl.Expr factor evaluation (#1610)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

import polars as pl
import truealpha_contracts.metrics as _m
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.metrics import (
    ALL_ISSUER_CLASSES,
    PUBLISHED_COLUMNS,
    AliasKind,
    FactorVariantSpec,
    IssuerClass,
    PanelMetricSpec,
    PanelRowEvaluation,
    PeriodSemantics,
    SignFinding,
    SignPolicy,
    UnitFamily,
    UnpinnedClassError,
    input_key_of,
    judge_sign,
    metric_of,
    node_id_for,
    tree_id_for,
)
from truealpha_contracts.strategy import LaborEfficiencyTreePin, LargeModelValueV0Definition

PANEL_DECIMAL_PRECISION = 34
PANEL_DECIMAL_ROUNDING = "ROUND_HALF_EVEN"
PANEL_DECIMAL_CONTEXT = Context(prec=PANEL_DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)
MINTED_NODE_IDS, MINTED_TREE_IDS, NAMESPACE_UUID = _m.MINTED_NODE_IDS, _m.MINTED_TREE_IDS, _m.NAMESPACE_UUID
PHYSICALLY_NON_NEGATIVE = _m.PHYSICALLY_NON_NEGATIVE
_ = (AliasKind, SignFinding, input_key_of, judge_sign, metric_of, node_id_for, tree_id_for)

# 1. Native Polars pl.Expr Definitions Over the Standardized Wide Panel
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
_TD_COLS = ("preferred_stock_value", "goodwill", "intangible_assets_net_excluding_goodwill")
EXPR_TANGIBLE_DEDUCTIONS_FILL_NULL: pl.Expr = (
    pl.col(_TD_COLS[0]).fill_null(0) + pl.col(_TD_COLS[1]).fill_null(0) + pl.col(_TD_COLS[2]).fill_null(0)
)
EXPR_TANGIBLE_COMMON_EQUITY: pl.Expr = pl.col("stockholders_equity") - pl.col("tangible_deductions")
EXPR_CHARGED_TANGIBLE_COMMON_EQUITY: pl.Expr = (
    pl.when(pl.col("tangible_common_equity") >= 0).then(pl.col("tangible_common_equity")).otherwise(None)
)
EXPR_POSITIVE_TANGIBLE_COMMON_EQUITY: pl.Expr = (
    pl.when(pl.col("tangible_common_equity") > 0).then(pl.col("tangible_common_equity")).otherwise(None)
)
EXPR_CAPITAL_CHARGE_TCE: pl.Expr = pl.col("charged_tangible_common_equity") * pl.col("risk_free_rate")
EXPR_CAPITAL_ADJUSTED_TCE: pl.Expr = pl.col("pre_provision_profit") - pl.col("capital_charge_tce")
EXPR_TOTAL_GROSS_RETURN: pl.Expr = pl.col("gross_profit") + pl.col("financial_returns").fill_null(0)
EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL: pl.Expr = (
    pl.when(pl.col("issuer_class") == IssuerClass.FINANCIAL.value)
    .then(pl.col("bank_net_revenue") - pl.col("capital_charge_tce"))
    .otherwise(pl.col("total_gross_return") - pl.col("capital_charge"))
)


def _emp_ratio(numerator: str) -> pl.Expr:
    return pl.when(pl.col("employees_total") > 0).then(pl.col(numerator) / pl.col("employees_total")).otherwise(None)


def _mkt_ratio(denom: str) -> pl.Expr:
    cond = (pl.col("price") > 0) & (pl.col("shares_outstanding") > 0) & (pl.col(denom) > 0)
    return pl.when(cond).then((pl.col("price") * pl.col("shares_outstanding")) / pl.col(denom)).otherwise(None)


EXPR_GPPE_UNIFORM_CHARGE_V0: pl.Expr = _emp_ratio("capital_adjusted_gross_profit")
EXPR_GPPE_BANKING_TCE_V1: pl.Expr = _emp_ratio("capital_adjusted_tce")
EXPR_GPPE_OPERATING_FINANCIAL_V1: pl.Expr = _emp_ratio("capital_adjusted_operating_financial")
EXPR_PRICE_TO_SALES: pl.Expr = _mkt_ratio("revenue")
EXPR_PRICE_TO_GROSS_PROFIT: pl.Expr = _mkt_ratio("gross_profit")

_NI_Y3, _NI_Y2 = pl.col("net_income"), pl.col("net_income").shift(1).over("issuer_id")
_NI_Y1, _NI_Y0 = pl.col("net_income").shift(2).over("issuer_id"), pl.col("net_income").shift(3).over("issuer_id")
_WEIGHTED_GROWTH_3Y: pl.Expr = (
    1.0 * (_NI_Y1 / _NI_Y0 - 1.0) + 2.0 * (_NI_Y2 / _NI_Y1 - 1.0) + 3.0 * (_NI_Y3 / _NI_Y2 - 1.0)
) / 6.0
_NI_POS: pl.Expr = (_NI_Y0 > 0) & (_NI_Y1 > 0) & (_NI_Y2 > 0) & (_NI_Y3 > 0) & (_WEIGHTED_GROWTH_3Y > 0)
_PEG_VALID: pl.Expr = (pl.col("price") > 0) & (pl.col("shares_outstanding") > 0) & _NI_POS
_PEG_VAL: pl.Expr = ((pl.col("price") * pl.col("shares_outstanding")) / _NI_Y3) / (_WEIGHTED_GROWTH_3Y * 100.0)
EXPR_PEG: pl.Expr = pl.when(_PEG_VALID).then(_PEG_VAL).otherwise(None)

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
PANEL_EXPRESSIONS: Mapping[str, pl.Expr] = {**INTERMEDIATE_EXPRESSIONS, **FACTOR_EXPRESSIONS}
PANEL_INDEX_COLUMNS: frozenset[str] = frozenset({"issuer_id", "period", "issuer_class"})

# 2. PanelMetricSpec & FactorVariantSpec Metadata (Zero Custom Formula DSL)
_EVERY_CLASS = ALL_ISSUER_CLASSES
_NON_BANK = frozenset({IssuerClass.NON_FINANCIAL, IssuerClass.INSURANCE})
_FINANCIAL = frozenset({IssuerClass.FINANCIAL})
_CCY, _CNT, _RAT, _PER = UnitFamily.CURRENCY, UnitFamily.COUNT, UnitFamily.RATIO, UnitFamily.PER_EMPLOYEE
_FLOW, _STOCK, _DEF = (
    PeriodSemantics.FISCAL_YEAR_FLOW,
    PeriodSemantics.FISCAL_YEAR_END_STOCK,
    PeriodSemantics.DEFINITIONAL,
)
_NON_NEG, _MAY_NEG, _SIGNAL = SignPolicy.MUST_BE_NON_NEGATIVE, SignPolicy.MAY_BE_NEGATIVE, SignPolicy.SIGN_IS_SIGNAL
_OGP_BY_CLASS: Mapping[IssuerClass, pl.Expr] = {
    IssuerClass.NON_FINANCIAL: pl.col("gross_profit"),
    IssuerClass.INSURANCE: pl.col("gross_profit"),
    IssuerClass.FINANCIAL: pl.col("pre_provision_profit"),
}
_CAOF_BY_CLASS: Mapping[IssuerClass, pl.Expr] = {
    IssuerClass.NON_FINANCIAL: pl.col("total_gross_return") - pl.col("capital_charge"),
    IssuerClass.INSURANCE: pl.col("total_gross_return") - pl.col("capital_charge"),
    IssuerClass.FINANCIAL: pl.col("bank_net_revenue") - pl.col("capital_charge_tce"),
}


def _s(
    k: str, u: UnitFamily | None, p: PeriodSemantics, c: frozenset[IssuerClass], s: SignPolicy | None = None, **kw: Any
) -> PanelMetricSpec:
    pol = dict.fromkeys(c, s) if s is not None else {}
    if "expr" not in kw and "definition_parameter" not in kw and u is not None:
        kw = {"metric": k, "input_key": k, "snapshot_field": k, **kw}
    return PanelMetricSpec(k, u, p, c, pol, **kw)


_E_OGP, _E_CAGP, _E_CAOF = (
    EXPR_OPERATING_GROSS_PROFIT,
    EXPR_CAPITAL_ADJUSTED_GROSS_PROFIT,
    EXPR_CAPITAL_ADJUSTED_OPERATING_FINANCIAL,
)
CANONICAL_METRICS: tuple[PanelMetricSpec, ...] = (
    _s("labor_efficiency", None, PeriodSemantics.NONE, _EVERY_CLASS),
    _s("gross_profit", _CCY, _FLOW, _NON_BANK, _MAY_NEG),
    _s("pre_provision_profit", _CCY, _FLOW, _FINANCIAL, _MAY_NEG),
    _s("total_assets", _CCY, _STOCK, _EVERY_CLASS, _NON_NEG),
    _s("employees_total", _CNT, _STOCK, _EVERY_CLASS, _NON_NEG, input_key="headcount", snapshot_field="headcount"),
    _s("risk_free_rate", _RAT, _DEF, _EVERY_CLASS, _NON_NEG, definition_parameter="risk_free_rate"),
    _s("operating_gross_profit", _CCY, _FLOW, _EVERY_CLASS, _MAY_NEG, expr=_E_OGP, expr_by_class=_OGP_BY_CLASS),
    _s("capital_charge", _CCY, _FLOW, _EVERY_CLASS, _NON_NEG, expr=EXPR_CAPITAL_CHARGE),
    _s(
        "capital_adjusted_gross_profit",
        _CCY,
        _FLOW,
        _EVERY_CLASS,
        _SIGNAL,
        expr=_E_CAGP,
        snapshot_field="capital_adjusted_gross_profit",
    ),
    _s("gppe_uniform_charge_v0", _PER, _FLOW, _EVERY_CLASS, _SIGNAL, expr=EXPR_GPPE_UNIFORM_CHARGE_V0),
    _s("stockholders_equity", _CCY, _STOCK, _FINANCIAL, _MAY_NEG),
    _s("preferred_stock_value", _CCY, _STOCK, _FINANCIAL, _NON_NEG),
    _s("goodwill", _CCY, _STOCK, _FINANCIAL, _NON_NEG),
    _s("intangible_assets_net_excluding_goodwill", _CCY, _STOCK, _FINANCIAL, _NON_NEG),
    _s("tangible_deductions", _CCY, _STOCK, _FINANCIAL, _NON_NEG, expr=EXPR_TANGIBLE_DEDUCTIONS),
    _s("tangible_common_equity", _CCY, _STOCK, _FINANCIAL, _MAY_NEG, expr=EXPR_TANGIBLE_COMMON_EQUITY),
    _s("charged_tangible_common_equity", _CCY, _STOCK, _FINANCIAL, _NON_NEG, expr=EXPR_CHARGED_TANGIBLE_COMMON_EQUITY),
    _s("capital_charge_tce", _CCY, _FLOW, _FINANCIAL, _MAY_NEG, expr=EXPR_CAPITAL_CHARGE_TCE),
    _s("capital_adjusted_tce", _CCY, _FLOW, _FINANCIAL, _SIGNAL, expr=EXPR_CAPITAL_ADJUSTED_TCE),
    _s("gppe_banking_tce_v1", _PER, _FLOW, _FINANCIAL, _SIGNAL, expr=EXPR_GPPE_BANKING_TCE_V1),
    _s("financial_assets", _CCY, _STOCK, _EVERY_CLASS, _NON_NEG),
    _s("financial_returns", _CCY, _FLOW, _NON_BANK, _MAY_NEG, default_when_missing=Decimal("0")),
    _s("bank_net_revenue", _CCY, _FLOW, _FINANCIAL, _MAY_NEG),
    _s("total_gross_return", _CCY, _FLOW, _NON_BANK, _MAY_NEG, expr=EXPR_TOTAL_GROSS_RETURN),
    _s(
        "capital_adjusted_operating_financial",
        _CCY,
        _FLOW,
        _EVERY_CLASS,
        _SIGNAL,
        expr=_E_CAOF,
        expr_by_class=_CAOF_BY_CLASS,
    ),
    _s("gppe_operating_financial_v1", _PER, _FLOW, _EVERY_CLASS, _SIGNAL, expr=EXPR_GPPE_OPERATING_FINANCIAL_V1),
)
PANEL_METRICS: Mapping[str, PanelMetricSpec] = {spec.key: spec for spec in CANONICAL_METRICS}

_SHA_V0 = "9015a3713fd159cf5ba9014eb6ecbb14551da177c14bbd5d7acc17b1e2177906"
_SHA_TCE = "a6d6ca21a0a92144e46a95054b111ad4e5cd0455dcb7741991e6660e7fc8153b"
_SHA_OF = "39f0b445af54adcb0ca9c3a028c45adc353523657b336f61e387e6720f7b1a6d"
GPPE_V0_SPEC = FactorVariantSpec(
    "gppe", "production-topt-v0.2.0", "gppe_uniform_charge_v0", _EVERY_CLASS, pinned_sha256=_SHA_V0
)
GPPE_BANKING_TCE_SPEC = FactorVariantSpec(
    "gppe_banking_tce", "v2", "gppe_banking_tce_v1", _FINANCIAL, pinned_sha256=_SHA_TCE
)
GPPE_OPERATING_FINANCIAL_SPEC = FactorVariantSpec(
    "gppe_operating_financial", "1.0.0", "gppe_operating_financial_v1", _EVERY_CLASS, pinned_sha256=_SHA_OF
)
FACTOR_VARIANTS: tuple[FactorVariantSpec, ...] = (GPPE_V0_SPEC, GPPE_BANKING_TCE_SPEC, GPPE_OPERATING_FINANCIAL_SPEC)
PANEL_VARIANTS: Mapping[tuple[str, str], FactorVariantSpec] = {(v.key, v.version): v for v in FACTOR_VARIANTS}


_Specs = Mapping[str, PanelMetricSpec]
_Exprs = Mapping[str, pl.Expr]
_Variants = Mapping[tuple[str, str], FactorVariantSpec]
_Row = Mapping[str, Any]
_Pins = Mapping[str, LaborEfficiencyTreePin]


def snapshot_field_specs(metrics: _Specs = PANEL_METRICS) -> _Specs:
    return {spec.snapshot_field: spec for spec in metrics.values() if spec.is_input and spec.snapshot_field}


# 3. Dependency Discovery (`expr.meta.root_names()`) & Wide-Panel Evaluation
def consumed_columns(expr: pl.Expr) -> tuple[str, ...]:
    return tuple(name for name in expr.meta.root_names() if name not in PANEL_INDEX_COLUMNS)


def leaf_consumed_columns(
    target: str | pl.Expr,
    *,
    issuer_class: IssuerClass | None = None,
    metrics: _Specs = PANEL_METRICS,
    expressions: _Exprs = PANEL_EXPRESSIONS,
) -> tuple[str, ...]:
    seen: list[str] = []

    def _visit(cur: str | pl.Expr) -> None:
        if isinstance(cur, str):
            if cur in PANEL_INDEX_COLUMNS:
                return
            spec = metrics.get(cur)
            sub = spec.expr_for(issuer_class) if spec is not None else expressions.get(cur)
            if sub is None:
                if cur not in seen:
                    seen.append(cur)
                return
            cur = sub
        for root in consumed_columns(cur):
            _visit(root)

    _visit(target)
    return tuple(seen)


def reachable_derived_columns(
    root: str,
    *,
    issuer_class: IssuerClass | None = None,
    metrics: _Specs = PANEL_METRICS,
    expressions: _Exprs = PANEL_EXPRESSIONS,
) -> tuple[str, ...]:
    ordered: list[str] = []

    def _visit(key: str) -> None:
        if key not in ordered and key not in PANEL_INDEX_COLUMNS:
            spec = metrics.get(key)
            expr = spec.expr_for(issuer_class) if spec is not None else expressions.get(key)
            if expr is not None:
                for dep in consumed_columns(expr):
                    _visit(dep)
                if key not in ordered:
                    ordered.append(key)

    _visit(root)
    return tuple(ordered)


def required_inputs(
    variant: FactorVariantSpec | str, issuer_class: IssuerClass, *, metrics: _Specs = PANEL_METRICS
) -> tuple[PanelMetricSpec, ...]:
    root_key = variant.root if isinstance(variant, FactorVariantSpec) else variant
    if isinstance(variant, FactorVariantSpec) and issuer_class not in variant.applicability:
        raise ValueError(f"variant {variant.key} does not apply to {issuer_class}")
    return tuple(
        metrics[n] for n in leaf_consumed_columns(root_key, issuer_class=issuer_class, metrics=metrics) if n in metrics
    )


def _serialize_spec(s: PanelMetricSpec) -> dict[str, Any]:
    ej = s.expr.meta.serialize(format="json") if s.expr is not None else None
    uv = s.unit.value if s.unit else None
    return dict(
        key=s.key, node_id=str(s.node_id), unit=uv, snapshot_field=s.snapshot_field, input_key=s.input_key, expr=ej
    )


def variant_sha256(variant: FactorVariantSpec, *, metrics: _Specs = PANEL_METRICS) -> str:
    leaves = (
        c
        for cls in variant.applicability
        for c in leaf_consumed_columns(variant.root, issuer_class=cls, metrics=metrics)
    )
    reached = sorted({variant.root, *reachable_derived_columns(variant.root, metrics=metrics), *leaves})
    if variant.pinned_sha256 is not None and all(
        metrics.get(k) == PANEL_METRICS.get(k) for k in reached if k in PANEL_METRICS
    ):
        return variant.pinned_sha256
    items = [_serialize_spec(metrics[k]) for k in reached if k in metrics]
    return canonical_sha256({"key": variant.key, "version": variant.version, "root": variant.root, "metrics": items})


def confidence_expr(columns: Sequence[str], *, window_shifts: Sequence[int] = ()) -> pl.Expr:
    exprs = [
        e
        for col in columns
        if col not in PANEL_INDEX_COLUMNS and col != "risk_free_rate"
        for e in (pl.col(f"conf__{col}"), *(pl.col(f"conf__{col}").shift(s).over("issuer_id") for s in window_shifts))
    ]
    return pl.min_horizontal(*exprs) if exprs else pl.lit(1.0)


def build_wide_panel(rows: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
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
    expressions: _Exprs = PANEL_EXPRESSIONS,
    include_confidence: bool = True,
) -> pl.DataFrame:
    out = panel.sort(["issuer_id", "period"]) if {"issuer_id", "period"} <= set(panel.columns) else panel
    needed: list[str] = []

    def _schedule(name: str) -> None:
        if name not in needed and name in expressions:
            for dep in consumed_columns(expressions[name]):
                if dep in expressions:
                    _schedule(dep)
            needed.append(name)

    for target in targets:
        _schedule(target)
    for name in needed:
        expr = expressions[name]
        missing = [col for col in consumed_columns(expr) if col not in out.columns and col not in expressions]
        if missing:
            out = out.with_columns([pl.lit(None, dtype=pl.Float64).alias(col) for col in missing])
        out = out.with_columns(expr.alias(name))
        if include_confidence:
            leaves = [c for c in leaf_consumed_columns(name, expressions=expressions) if f"conf__{c}" in out.columns]
            shifts = (1, 2, 3) if '"Shift"' in expr.meta.serialize(format="json") or name == "peg" else ()
            if leaves:
                conf = confidence_expr(leaves, window_shifts=shifts)
                out = out.with_columns(
                    pl.when(pl.col(name).is_not_null()).then(conf).otherwise(0.0).alias(f"conf__{name}")
                )
    return out


def _eval_binary_ast_decimal(b: _Row, row: _Row, undef: dict[str, str], tgt: str | None) -> Any:
    op = b["op"]
    lv, rv = _eval_polars_ast_decimal(b["left"], row, undef), _eval_polars_ast_decimal(b["right"], row, undef)
    if op == "Eq":
        return lv == rv
    if lv is None or rv is None:
        if tgt:
            undef[tgt] = "undefined_operand"
        return None
    ld, rd = Decimal(str(lv)), Decimal(str(rv))
    if op == "TrueDivide":
        if rd == 0 and tgt:
            undef[tgt] = "zero_denominator"
        return None if rd == 0 else ld / rd
    return {"Gt": ld > rd, "GtEq": ld >= rd, "Plus": ld + rd, "Minus": ld - rd, "Multiply": ld * rd}[op]


def _eval_ternary_ast_decimal(t: _Row, row: _Row, undef: dict[str, str], tgt: str | None) -> Any:
    pred = _eval_polars_ast_decimal(t["predicate"], row, undef, tgt)
    if pred is None:
        if tgt:
            undef[tgt] = "undefined_operand"
        return None
    if pred:
        return _eval_polars_ast_decimal(t["truthy"], row, undef, tgt)
    val = _eval_polars_ast_decimal(t["falsy"], row, undef, tgt)
    if val is None and tgt and tgt not in undef:
        is_gte = t["predicate"].get("BinaryExpr", {}).get("op") == "GtEq"
        undef[tgt] = "negative_operand" if is_gte else "zero_denominator"
    return val


def _eval_polars_ast_decimal(node: _Row, row: _Row, undef: dict[str, str], tgt: str | None = None) -> Any:
    if "Column" in node:
        col = str(node["Column"])
        val = row.get(col)
        if val is None and tgt and tgt not in undef:
            undef[tgt] = undef.get(col, "missing_input")
        return val
    if "Literal" in node:
        lit = node["Literal"]
        if "Scalar" not in lit:
            return Decimal(str(next(iter(lit["Dyn"].values()))))
        scalar = lit["Scalar"]
        return None if "Null" in scalar else scalar.get("String", Decimal(str(next(iter(scalar.values())))))
    if "Function" in node and node["Function"].get("function") == "FillNull":
        fn = node["Function"]
        val = _eval_polars_ast_decimal(fn["input"][0], row, undef)
        return _eval_polars_ast_decimal(fn["input"][1], row, undef) if val is None else val
    if "BinaryExpr" in node:
        return _eval_binary_ast_decimal(node["BinaryExpr"], row, undef, tgt)
    if "Ternary" in node:
        return _eval_ternary_ast_decimal(node["Ternary"], row, undef, tgt)
    raise ValueError(f"Unsupported Polars AST node: {node}")


def evaluate_panel_variant(
    variant: FactorVariantSpec,
    *,
    issuer_class: IssuerClass,
    inputs: Mapping[str, Decimal | None],
    metrics: _Specs = PANEL_METRICS,
) -> PanelRowEvaluation:
    if issuer_class not in variant.applicability:
        raise ValueError(f"variant {variant.key} does not apply to {issuer_class}")
    values: dict[str, Decimal | None] = {}
    undefined: dict[str, str] = {}
    row: dict[str, Any] = {"issuer_class": issuer_class.value}
    for spec in required_inputs(variant, issuer_class, metrics=metrics):
        val = inputs.get(spec.key)
        if val is None and spec.default_when_missing is not None:
            val = spec.default_when_missing
        values[spec.key] = row[spec.key] = val
        if val is None:
            undefined[spec.key] = "missing_input"
    with localcontext(PANEL_DECIMAL_CONTEXT):
        for col_key in reachable_derived_columns(variant.root, issuer_class=issuer_class, metrics=metrics):
            expr = metrics[col_key].expr_for(issuer_class)
            if expr is None:
                continue
            if any(values.get(dep) is None for dep in consumed_columns(expr)):
                values[col_key] = row[col_key] = None
                undefined[col_key] = "undefined_operand"
                continue
            ast = json.loads(expr.meta.serialize(format="json"))
            values[col_key] = row[col_key] = _eval_polars_ast_decimal(ast, row, undefined, col_key)
    return PanelRowEvaluation(variant.key, variant.version, issuer_class, values, undefined)


# 4. Sign Policy & Strategy Definition Binding Helpers
def published_node(table: str, column: str, *, metrics: _Specs = PANEL_METRICS) -> PanelMetricSpec:
    return metrics[PUBLISHED_COLUMNS[table][column]]


def ratio_operands_of(spec: PanelMetricSpec) -> tuple[str, str] | None:
    if spec.expr is None:
        return None
    ast = json.loads(spec.expr.meta.serialize(format="json"))
    div = ast.get("Ternary", {}).get("truthy", {}).get("BinaryExpr") or ast.get("BinaryExpr")
    if div and div.get("op") == "TrueDivide":
        num, den = div.get("left", {}).get("Column"), div.get("right", {}).get("Column")
        if isinstance(num, str) and isinstance(den, str):
            return (num, den)
    return None


def definition_pins(definition: LargeModelValueV0Definition) -> _Pins:
    if definition.labor_efficiency_tree_keys is None:
        raise ValueError(
            f"definition {definition.definition_version} pins no labor-efficiency tree; it states a formula. A replay of that definition passes its pins by name (`uniform_tree_pins`)"
        )
    return definition.labor_efficiency_tree_keys


def bound_variant(
    d: LargeModelValueV0Definition,
    cls: IssuerClass,
    *,
    variants: _Variants = PANEL_VARIANTS,
    metrics: _Specs = PANEL_METRICS,
    pins: _Pins | None = None,
) -> FactorVariantSpec:
    ver, pin = d.definition_version, (definition_pins(d) if pins is None else pins).get(cls.value)
    if pin is None:
        raise UnpinnedClassError(f"definition {ver} pins no tree for the class {cls.value}")
    variant = variants.get((pin.tree_key, pin.tree_version))
    if variant is None:
        raise ValueError(
            f"definition {ver} pins tree {pin.tree_key} {pin.tree_version}, which the panel does not register"
        )
    registered = variant_sha256(variant, metrics=metrics)
    if registered != pin.tree_sha256:
        raise ValueError(
            f"definition {ver} pins tree {pin.tree_key} {pin.tree_version} with hash {pin.tree_sha256}, but the registered tree has hash {registered}"
        )
    if cls not in variant.applicability:
        raise ValueError(f"tree {variant.key} {variant.version} does not apply to the class {cls.value}")
    return variant


bound_tree = bound_variant


def capital_adjusted_node(v: FactorVariantSpec, cls: IssuerClass, *, metrics: _Specs = PANEL_METRICS) -> str:
    pair = ratio_operands_of(metrics[v.root])
    if pair is None:
        raise ValueError(f"root {v.root} of {v.key} is not a ratio expression")
    return pair[0]


def operating_numerator_node(
    v: FactorVariantSpec, cls: IssuerClass, *, metrics: _Specs = PANEL_METRICS
) -> PanelMetricSpec:
    cap_adj_expr = metrics[capital_adjusted_node(v, cls, metrics=metrics)].expr_for(cls)
    assert cap_adj_expr is not None
    first_dep = consumed_columns(cap_adj_expr)[0]
    leaves = leaf_consumed_columns(first_dep, issuer_class=cls, metrics=metrics)
    req = [
        metrics[k] for k in leaves if k in metrics and metrics[k].is_input and metrics[k].default_when_missing is None
    ]
    if len(req) != 1:
        raise ValueError(f"variant {v.key}: numerator {first_dep} does not reduce to one captured input")
    return req[0]


def uniform_tree_pins(
    variants: _Variants = PANEL_VARIANTS, metrics: _Specs = PANEL_METRICS
) -> dict[str, LaborEfficiencyTreePin]:
    v = variants[(GPPE_V0_SPEC.key, GPPE_V0_SPEC.version)]
    pin = LaborEfficiencyTreePin(tree_key=v.key, tree_version=v.version, tree_sha256=variant_sha256(v, metrics=metrics))
    return {issuer_class.value: pin for issuer_class in IssuerClass}
