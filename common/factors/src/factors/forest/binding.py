"""Bind a strategy definition to the labor-efficiency tree of an issuer class (#1518).

Definition 1.1.0 states no labor-efficiency formula. It pins one tree of the registered forest
for each issuer class, by key, version and content hash (`labor_efficiency_tree_keys`). The TOPT
core and the strategy composite call `bound_tree`, so both evaluate the one tree that the
definition names. A pin that differs from the registered tree raises: a definition cannot run on
a tree that it did not pin. The tick fails before it writes a row, and
`common/factors/tests/test_bound_tree.py` shows the cause in CI.

Contracts cannot import factors, so the binding lives here and not on the definition.
"""

from __future__ import annotations

from collections.abc import Mapping

from truealpha_contracts.metrics import input_key_for_metric
from truealpha_contracts.strategy import LaborEfficiencyTreePin, LargeModelValueV0Definition

from factors.forest.model import AliasKind, Forest, IssuerClass, MetricNode, MetricTree, ProvenanceKind
from factors.forest.registry import FOREST, GPPE_V0_TREE


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
    """The registered tree that evaluates labor efficiency for ``issuer_class`` under ``definition``.

    ``pins`` replaces the pins of the definition. Only the replay of a frozen definition that
    states a formula (1.0.0) passes it. The function raises ``ValueError`` when the definition
    pins no tree for the class, when the forest does not register the pinned key and version, when
    the registered tree has another content hash, or when the tree does not apply to the class.
    """
    pinned = definition_pins(definition) if pins is None else pins
    pin = pinned.get(issuer_class.value)
    if pin is None:
        raise ValueError(f"definition {definition.definition_version} pins no tree for the class {issuer_class.value}")
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
    """The node that holds the real profit after the capital charge: the numerator of the root ratio.

    Every pinned tree has the root `capital-adjusted profit / employees`. The TOPT results publish the
    numerator as `capital_adjusted_gross_profit` (`gppe`: `capital_adjusted_gross_profit`;
    `gppe_banking_tce`: `capital_adjusted_tce`). A tree whose root is not a ratio raises.
    """
    decomposition = tree.decomposition_of(tree.root)
    if decomposition is None or decomposition.formula_id != "ratio":
        raise ValueError(f"the root of tree {tree.key} {tree.version} is not a ratio: it has no capital-adjusted node")
    return decomposition.operands[issuer_class][0]


def uniform_tree_pins(forest: Forest = FOREST) -> dict[str, LaborEfficiencyTreePin]:
    """The pins of the frozen #21 replay (definition 1.0.0): the uniform total-assets tree for every class.

    Definition 1.0.0 states one formula, `(numerator - total_assets * rate) / headcount`, for every
    issuer. That formula is the tree `gppe`. The replay of the frozen golden decisions passes these
    pins to `evaluate_cutoff`. The hash comes from the registered tree, never from a literal.
    """
    tree = forest.tree(GPPE_V0_TREE.key, GPPE_V0_TREE.version)
    pin = LaborEfficiencyTreePin(tree_key=tree.key, tree_version=tree.version, tree_sha256=forest.tree_sha256(tree))
    return {issuer_class.value: pin for issuer_class in IssuerClass}


def metric_of(node: MetricNode) -> str:
    """The `METRICS` name of a captured input node.

    A node names it by its metric alias. A node without one names it by its registry provenance:
    a bank's pre-provision profit is the financial-issuer split of `gross_profit`.
    """
    alias = node.alias(AliasKind.METRIC)
    if alias is not None:
        return alias
    if node.provenance.kind is ProvenanceKind.METRIC_REGISTRY:
        return node.provenance.reference
    return node.key


def input_key_of(node: MetricNode) -> str:
    """The strategy input key (`staging.strategy_backtest_inputs.input_key`) of a captured input node."""
    return node.alias(AliasKind.INPUT_KEY) or input_key_for_metric(metric_of(node))
