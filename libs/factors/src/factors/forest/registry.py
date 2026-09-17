"""The registered forest (#528, docs/metric-forest.md).

Today it holds one tree: GPPE `production-topt-v0.2.0`, the uniform capital-adjusted labor
efficiency that `factors.production_topt.compute_topt_gppe` used to spell out by hand and now
evaluates from here. Registering it changed no number (the golden result identities in
`tests/forest/test_gppe_tree.py` were captured before the change).

Node and tree UUIDs were minted once as uuid5 values under the namespace
`uuid5(NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/metric-forest")` and are
literals from then on. They are never recomputed from a key, so renaming a key keeps the UUID.

**Sign policy of GPPE v0.2.0.** The definition frozen by #59 (see
`factors.base.gross_profit_per_employee`) says a negative value, for a bank or anyone else, is
a valid low signal ("destroys value per employee at the risk-free hurdle"). The nodes below
declare exactly that, `sign-is-signal` for every issuer class. `tools/output_invariants.py`
and the tick's plausibility gate assert it: they print each negative value by name and
refuse only what a node forbids. This replaces the `gppe-not-negative` invariant and the
`sign-per-branch` gate rule, which claimed the opposite of the definition (#528, 2026-09-04).
The operating + financial decomposition (#528 scope 1) is the next tree, with its own
per-component policies. It will be a new tree version, never an edit to this one.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from truealpha_contracts.metrics import UnitFamily

from factors.forest.model import (
    ALL_ISSUER_CLASSES,
    AliasKind,
    ConfidenceBand,
    ConfidenceRule,
    Decomposition,
    Forest,
    IssuerClass,
    MetricNode,
    MetricTree,
    NodeAlias,
    NodeKind,
    PeriodSemantics,
    Provenance,
    ProvenanceKind,
    SignPolicy,
)

_EVERY_CLASS = ALL_ISSUER_CLASSES
_NON_BANK = frozenset({IssuerClass.NON_FINANCIAL, IssuerClass.INSURANCE})


def _policy(policy: SignPolicy, classes: frozenset[IssuerClass] = _EVERY_CLASS) -> dict[IssuerClass, SignPolicy]:
    return {issuer_class: policy for issuer_class in classes}


def _captured(family: str) -> ConfidenceBand:
    return ConfidenceBand(rule=ConfidenceRule.AS_CAPTURED, family=family)


_DERIVED_CONFIDENCE = ConfidenceBand(rule=ConfidenceRule.MINIMUM_CONSUMED)


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
    confidence=None,
)

GROSS_PROFIT = MetricNode(
    node_id=UUID("c682a9c8-c41d-5262-899b-f2d62000e10f"),
    key="gross_profit",
    kind=NodeKind.INPUT,
    definition="Reported gross profit for the fiscal year (revenue minus cost of revenue as the issuer reports it).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    # A bank reports no cost of revenue; its operating numerator is `pre_provision_profit`.
    # An insurer's parser-branch gross profit is revenue minus policyholder benefits (#496).
    applicability=_NON_BANK,
    # A loss-making product line can sell below cost; the sign carries no rule by itself.
    sign_policy=_policy(SignPolicy.MAY_BE_NEGATIVE, _NON_BANK),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="gross_profit"),
    confidence=_captured("gross_profit"),
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
        reference="gross_profit",
        note="financial-issuer split: a bank's gross-profit proxy (METRICS gross_profit.financial_issuer_split)",
    ),
    confidence=_captured("pre_provision_profit"),
    aliases=(NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="pre_provision_profit"),),
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
    confidence=_captured("total_assets"),
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
    confidence=_captured("headcount"),
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
    confidence=ConfidenceBand(rule=ConfidenceRule.DEFINITIONAL),
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
    confidence=_DERIVED_CONFIDENCE,
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
    confidence=_DERIVED_CONFIDENCE,
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
    confidence=_DERIVED_CONFIDENCE,
    aliases=(NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="capital_adjusted_gross_profit"),),
)

GPPE = MetricNode(
    node_id=UUID("6143cf17-ceba-5f78-a2d3-a7cc423c49a7"),
    key="gppe",
    kind=NodeKind.DERIVED,
    definition=(
        "Capital-adjusted gross profit per employee, v0.2.0 (#59, #394): real profit v0 divided by "
        "headcount. Negative means the issuer earns less than the risk-free return on its total "
        "assets, which the definition ranks as a low signal."
    ),
    unit=UnitFamily.PER_EMPLOYEE,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_EVERY_CLASS,
    sign_policy=_policy(SignPolicy.SIGN_IS_SIGNAL),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe"),
    confidence=_DERIVED_CONFIDENCE,
)

GPPE_V0_TREE = MetricTree(
    tree_id=UUID("ef13b940-b695-5f5f-846a-a36face5cc3e"),
    key="gppe",
    # The same coordinate as `GppeV0Definition.factor_version`; test_gppe_tree binds the two.
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
        # The uniform charge of v0.2.0, now an edge binding: the same base and rate for every
        # class. A class-specific charge is a different binding in a new tree version.
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
    # compute_topt_gppe's context since v0.1.0 (prec 34, half-even).
    decimal_precision=34,
)

FOREST = Forest(
    nodes=(
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
    ),
    trees=(GPPE_V0_TREE,),
)

#: Published mart columns -> the node each one carries, per table. The wide-row generator
#: (docs/metric-forest.md §6) will derive these; until then this is the one place a published
#: column is tied to a node, and both the invariant suite and the tick's gate read it.
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
