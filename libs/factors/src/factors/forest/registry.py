"""The registered forest (#528, docs/metric-forest.md).

It holds two named GPPE metrics (#1176, owner decision 2026-10-09):

- `gppe_uniform_charge_v0`, in tree `gppe` `production-topt-v0.2.0`. The uniform capital-adjusted
  labor efficiency that `factors.production_topt.compute_topt_gppe` used to spell out by hand.
  Every class uses total assets as the capital base. The node key was `gppe` before #1176; the
  arithmetic and the published values did not change (`tests/forest/test_gppe_tree.py` golden ids).
- `gppe_banking_tce_v1`, in tree `gppe_banking_tce` `v1`. FINANCIAL only. The capital base is
  measured tangible common equity, built from three captured inputs.

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


#: The datahub/factor boundary (owner decision 2026-09-17, #528 D1). The datahub records facts as
#: they are: a value is refused only when it cannot exist or cannot be computed — a quantity that is
#: physically non-negative carrying a negative number (a parse or unit defect), a zero or missing
#: denominator, a non-finite number. Everything economic — profits, income, value added,
#: efficiencies — may be negative, and a negative number is data, not a defect. Distorted or
#: special-case data (a financial issuer's balance sheet under a uniform capital charge, one-offs,
#: currency effects) is interpreted by the FACTOR layer, through declared, versioned edges and
#: node policies, never by rewriting or refusing the fact.
#:
#: So `must-be-non-negative` is reserved for the keys below, each with the reason it cannot be
#: negative; `test_must_be_non_negative_is_reserved_for_physical_quantities` holds every node to it.
PHYSICALLY_NON_NEGATIVE: Mapping[str, str] = MappingProxyType(
    {
        "total_assets": "a balance of assets; a negative total is a sign or unit defect in the filing parse",
        "employees_total": "a count of people",
        "risk_free_rate": "a definition parameter we set, not a captured fact",
        "capital_charge": "total_assets × risk_free_rate, both non-negative by the entries above",
        "goodwill": "a carrying amount of goodwill; a negative balance is a sign or unit defect in the filing parse",
        "intangible_assets_net_excluding_goodwill": (
            "a carrying amount of intangible assets; a negative balance is a sign or unit defect in the filing parse"
        ),
        "preferred_stock_value": "a carrying amount of preferred stock; a negative balance is a sign or unit defect in the filing parse",
        "tangible_deductions": "the sum of three non-negative balances (preferred stock, goodwill and intangibles)",
    }
)


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
    confidence=_DERIVED_CONFIDENCE,
)

# --- gppe_banking_tce_v1 (#1176): FINANCIAL only, capital base = measured tangible common equity.
# Node UUIDs are uuid5 of the key under the forest namespace, minted with the #1176 change.

_FINANCIAL = frozenset({IssuerClass.FINANCIAL})


def _financial_policy(policy: SignPolicy) -> dict[IssuerClass, SignPolicy]:
    return _policy(policy, _FINANCIAL)


STOCKHOLDERS_EQUITY = MetricNode(
    node_id=UUID("0aa53e41-0d12-5f11-92c4-fe6225166328"),
    key="stockholders_equity",
    kind=NodeKind.INPUT,
    definition="Total stockholders' equity at the fiscal year end, preferred equity included (#1176).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    # Negative equity is a real state (buybacks, losses). It is not a defect.
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.METRIC_REGISTRY, reference="stockholders_equity"),
    confidence=_captured("stockholders_equity"),
    aliases=(NodeAlias(kind=AliasKind.METRIC, value="stockholders_equity"),),
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
    confidence=_captured("preferred_stock_value"),
    aliases=(NodeAlias(kind=AliasKind.METRIC, value="preferred_stock_value"),),
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
    confidence=_captured("goodwill"),
    aliases=(NodeAlias(kind=AliasKind.METRIC, value="goodwill"),),
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
    confidence=_captured("intangible_assets_net_excluding_goodwill"),
    aliases=(NodeAlias(kind=AliasKind.METRIC, value="intangible_assets_net_excluding_goodwill"),),
)

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
    confidence=_DERIVED_CONFIDENCE,
)

TANGIBLE_COMMON_EQUITY = MetricNode(
    node_id=UUID("8c346339-0ca5-588a-90d4-e67c96db8ecb"),
    key="tangible_common_equity",
    kind=NodeKind.DERIVED,
    definition="Measured tangible common equity: stockholders' equity minus tangible deductions (#1176, #1176 follow-up).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_END_STOCK,
    applicability=_FINANCIAL,
    # Tangible equity can be negative for a weak balance sheet. That is economic, not a defect.
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
    confidence=_DERIVED_CONFIDENCE,
)

CAPITAL_CHARGE_TCE = MetricNode(
    node_id=UUID("588a8f4d-b301-5a2e-b0f3-de6743d60af4"),
    key="capital_charge_tce",
    kind=NodeKind.DERIVED,
    definition="The risk-free return on measured tangible common equity: TCE x rate (#1176).",
    unit=UnitFamily.CURRENCY,
    period=PeriodSemantics.FISCAL_YEAR_FLOW,
    applicability=_FINANCIAL,
    sign_policy=_financial_policy(SignPolicy.MAY_BE_NEGATIVE),
    provenance=Provenance(kind=ProvenanceKind.DERIVED, reference="gppe_banking_tce_v1"),
    confidence=_DERIVED_CONFIDENCE,
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
    confidence=_DERIVED_CONFIDENCE,
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
    confidence=_DERIVED_CONFIDENCE,
)

GPPE_BANKING_TCE_TREE = MetricTree(
    tree_id=UUID("96fd6d4a-8983-5caf-b647-360122569d00"),
    key="gppe_banking_tce",
    version="v1",
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
        # The banking numerator is the bank's pre-provision profit, read directly. The shared
        # operating node binds every class, so a FINANCIAL-only tree cannot reuse its decomposition.
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
            operands={IssuerClass.FINANCIAL: (TANGIBLE_COMMON_EQUITY.key, RISK_FREE_RATE.key)},
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
        STOCKHOLDERS_EQUITY,
        PREFERRED_STOCK_VALUE,
        GOODWILL,
        INTANGIBLE_ASSETS,
        TANGIBLE_DEDUCTIONS,
        TANGIBLE_COMMON_EQUITY,
        CAPITAL_CHARGE_TCE,
        CAPITAL_ADJUSTED_TCE,
        GPPE_BANKING_TCE_V1,
    ),
    trees=(GPPE_V0_TREE, GPPE_BANKING_TCE_TREE),
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
