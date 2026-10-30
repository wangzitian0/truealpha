"""Module 2: gross profit per employee (v0 capital-adjusted labor efficiency).

The factor evaluates one tree of the metric forest. It holds no formula of its own (#1518).

The strategy definition pins one tree for each issuer class (`labor_efficiency_tree_keys`,
`factors.forest.bound_tree`): the uniform total-assets tree `gppe` (`gppe_uniform_charge_v0`) and the
banking tree `gppe_banking_tce` (`gppe_banking_tce_v1`, measured tangible common equity, FINANCIAL
issuers only). The caller passes the pinned tree and the issuer class. The factor reads each captured
leaf of the tree from `facts` and evaluates the tree with `factors.forest.evaluate`. So the TOPT core and
the strategy composite evaluate the same tree, and one issuer has one value.

The uniform formula is frozen by issue #59 (2026-07-18 owner decision):

    real_profit_v0 = gross_profit - total_assets * risk_free_rate
    labor_efficiency_v0 = real_profit_v0 / employees_total

A negative value is a valid low signal ("destroys value per employee at the risk-free hurdle"), ranked
accordingly by the strategy, and not an exclusion. The banking tree charges the risk-free rate on
measured tangible common equity (owner decision D2, 2026-10-26). A missing leaf gives an unavailable
result with a flag that names it. No leaf is zero-filled and no capital base replaces another.

Each operating branch consumes its own registered numerator (`gross_profit`, `pre_provision_profit`,
or `insurance_pre_claims_profit` mapped into the tree's declared leaf).

`risk_free_rate` is a versioned parameter (#59: "3-month US T-bill yield (v0 default; versioned
parameter)"), not a live per-period market fact this factor looks up itself. The caller supplies it,
exactly like `growth_convention` is an explicit parameter to the PEG factor.

The uniform arithmetic is also expressed as a matrix-compatible AST expression
(`GPPE_EXPRESSION_DEFINITION`), built only from shared `truealpha_contracts.ast` node types, so a compiled
Polars execution reproduces the Decimal output of the uniform tree. The compiler is a test-time oracle
(`common/factors/tests/support/expression_oracle`, #758). No deployed path runs it.

Input metric names match the canonical registry (`truealpha_contracts.metrics.METRICS`), and `Fact`
rejects a metric and unit family that do not match it, so this function does not re-check units.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.ast import Div, Feature, Mul, Sub

from factors.forest import AliasKind, Forest, IssuerClass, MetricTree, evaluate, metric_of, required_inputs
from factors.registry import factor
from factors.types import Fact, FactorResult, UnitFamily

_GROSS_PROFIT = "gross_profit"
_TOTAL_ASSETS = "total_assets"
_EMPLOYEES_TOTAL = "employees_total"

#: The flags that a missing leaf of the banking tree raises (#1176). Equity, goodwill and intangibles
#: share one flag. Preferred stock has its own. The strategy maps each flag to its exclusion reason.
MISSING_TCE_FLAG = "missing_tangible_common_equity"
MISSING_PREFERRED_FLAG = "missing_preferred_stock_value"
#: The flag of a negative tangible common equity: the tree refuses it, so the charge is never a credit (#1518).
NEGATIVE_TCE_FLAG = "negative_tangible_common_equity"
#: The flag of a headcount that is zero or negative. The strategy maps it to `nonpositive_headcount`, as the core does.
NONPOSITIVE_EMPLOYEES_FLAG = "non_positive_employees_total"
_MISSING_LEAF_FLAGS = {
    "stockholders_equity": MISSING_TCE_FLAG,
    "goodwill": MISSING_TCE_FLAG,
    "intangible_assets_net_excluding_goodwill": MISSING_TCE_FLAG,
    "preferred_stock_value": MISSING_PREFERRED_FLAG,
}

#: `(gross_profit - total_assets * risk_free_rate) / employees_total` as a
#: `truealpha_contracts.ast` expression. Feature names are the panel column
#: names the compiler resolves with `pl.col(...)`, so they match the canonical
#: metric registry exactly as the Decimal path's inputs do.
GPPE_EXPRESSION_DEFINITION = Div(
    left=Sub(
        left=Feature(name=_GROSS_PROFIT),
        right=Mul(left=Feature(name=_TOTAL_ASSETS), right=Feature(name="risk_free_rate")),
    ),
    right=Feature(name=_EMPLOYEES_TOTAL),
)


def _find(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    # Facts already reflect one PIT-resolved vintage per metric; a factor never
    # re-selects among candidates (init.md Section 6) — take the sole match.
    matches = [f for f in facts if f.entity_id == entity_id and f.metric == metric]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple PIT-resolved facts for metric {metric!r}")
    return matches[0] if matches else None


@factor(
    "gross_profit_per_employee",
    kind="base",
    module=2,
    inputs=(
        "gross_profit",
        "pre_provision_profit",
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
    forest: Forest,
    tree: MetricTree,
    issuer_class: IssuerClass,
) -> FactorResult:
    """The labor efficiency of one issuer on the pinned ``tree`` (`gppe_uniform_charge_v0`, `gppe_banking_tce_v1`).

    A missing captured input gives no value and a flag that names it (`missing_<metric>`; the banking
    leaves have their own flags). A headcount that is not positive gives no value. Facts that carry two
    fiscal periods give no value (`fiscal_period_mismatch`): one period answers for the whole tree.
    The confidence is the minimum over the consumed inputs.
    """
    root = forest.node(tree.root)
    unit_family = root.unit or UnitFamily.PER_EMPLOYEE
    nodes = required_inputs(forest, tree, issuer_class)
    tree_inputs: dict[str, Decimal | None] = {}
    flags: list[str] = []
    consumed: list[Fact] = []
    for node in nodes:
        if node.alias(AliasKind.DEFINITION_PARAMETER) == "risk_free_rate":
            tree_inputs[node.key] = risk_free_rate
            continue
        metric = metric_of(node)
        fact = _find(facts, entity_id, metric)
        if fact is None and metric == "pre_provision_profit":
            fact = _find(facts, entity_id, _GROSS_PROFIT)
        tree_inputs[node.key] = None
        if fact is None or fact.value is None:
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

    def unavailable(flag_list: list[str]) -> FactorResult:
        return FactorResult(
            factor=tree.root,
            entity_id=entity_id,
            value=None,
            unit_family=unit_family,
            confidence=Decimal("0"),
            as_of=as_of,
            data_availability="unverified",
            flags=flag_list,
        )

    if flags:
        return unavailable(flags)
    evaluation = evaluate(forest, tree, issuer_class=issuer_class, inputs=tree_inputs)
    value = evaluation.values.get(tree.root)
    if value is None:
        if "negative_operand" in evaluation.undefined.values():
            return unavailable([NEGATIVE_TCE_FLAG])
        return unavailable([evaluation.undefined.get(tree.root, "undefined_operand")])
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
