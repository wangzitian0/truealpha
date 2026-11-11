"""Module 2: gross profit per employee (capital-adjusted labor efficiency over pinned panel variants)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from factors.panel import (
    PANEL_METRICS,
    AliasKind,
    FactorVariantSpec,
    IssuerClass,
    PanelMetricSpec,
    evaluate_panel_variant,
    metric_of,
    required_inputs,
)
from factors.registry import factor
from factors.types import Fact, FactorResult, UnitFamily, refused_factor_result, sole_fact

_GROSS_PROFIT = "gross_profit"
_EMPLOYEES_TOTAL = "employees_total"

MISSING_TCE_FLAG = "missing_tangible_common_equity"
MISSING_PREFERRED_FLAG = "missing_preferred_stock_value"
NEGATIVE_TCE_FLAG = "negative_tangible_common_equity"
NONPOSITIVE_EMPLOYEES_FLAG = "non_positive_employees_total"
_MISSING_LEAF_FLAGS = {
    "stockholders_equity": MISSING_TCE_FLAG,
    "goodwill": MISSING_TCE_FLAG,
    "intangible_assets_net_excluding_goodwill": MISSING_TCE_FLAG,
    "preferred_stock_value": MISSING_PREFERRED_FLAG,
}


def _resolve_leaf_fact(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    fact = sole_fact(facts, entity_id, metric)
    if fact is None and metric == "pre_provision_profit":
        return sole_fact(facts, entity_id, _GROSS_PROFIT)
    if fact is None and metric == "bank_net_revenue":
        return sole_fact(facts, entity_id, "pre_provision_profit") or sole_fact(facts, entity_id, _GROSS_PROFIT)
    return fact


@factor(
    "gross_profit_per_employee",
    kind="base",
    module=2,
    inputs=(
        "gross_profit",
        "pre_provision_profit",
        "bank_net_revenue",
        "financial_returns",
        "total_assets",
        "employees_total",
        "stockholders_equity",
        "preferred_stock_value",
        "goodwill",
        "intangible_assets_net_excluding_goodwill",
    ),
)
def gross_profit_per_employee(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
    risk_free_rate: Decimal,
    tree: FactorVariantSpec,
    issuer_class: IssuerClass,
    metrics: Mapping[str, PanelMetricSpec] = PANEL_METRICS,
) -> FactorResult:
    unit_family = metrics[tree.root].unit or UnitFamily.PER_EMPLOYEE
    tree_inputs: dict[str, Decimal | None] = {}
    flags: list[str] = []
    consumed: list[Fact] = []
    for node in required_inputs(tree, issuer_class, metrics=metrics):
        if node.alias(AliasKind.DEFINITION_PARAMETER) == "risk_free_rate":
            tree_inputs[node.key] = risk_free_rate
            continue
        metric = metric_of(node)
        fact = _resolve_leaf_fact(facts, entity_id, metric)
        tree_inputs[node.key] = None
        if fact is None or fact.value is None:
            if node.default_when_missing is not None:
                continue
            flag = _MISSING_LEAF_FLAGS.get(metric, f"missing_{metric}")
            if flag not in flags:
                flags.append(flag)
        elif metric == _EMPLOYEES_TOTAL and fact.value <= 0:
            flags.append(NONPOSITIVE_EMPLOYEES_FLAG)
        else:
            tree_inputs[node.key] = fact.value
            consumed.append(fact)
    if not flags and len({fact.fiscal_period for fact in consumed}) > 1:
        flags.append("fiscal_period_mismatch")
    if flags:
        return refused_factor_result(tree.root, entity_id, as_of, flags, unit_family=unit_family)

    evaluation = evaluate_panel_variant(tree, issuer_class=issuer_class, inputs=tree_inputs, metrics=metrics)
    value = evaluation.values.get(tree.root)
    if value is None:
        reason = (
            NEGATIVE_TCE_FLAG
            if "negative_operand" in evaluation.undefined.values()
            else evaluation.undefined.get(tree.root, "undefined_operand")
        )
        return refused_factor_result(tree.root, entity_id, as_of, [reason], unit_family=unit_family)
    return FactorResult(
        factor=tree.root,
        entity_id=entity_id,
        value=value,
        unit_family=unit_family,
        confidence=min(fact.confidence for fact in consumed) if consumed else Decimal("1"),
        as_of=as_of,
        data_availability="unverified",
        flags=[],
    )
