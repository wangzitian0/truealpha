"""The metric forest's types: nodes, typed decompositions, trees (#528, docs/metric-forest.md).

Owner decision 2026-09-17 on #528: keep several ways to decompose the same question (gross
profit, market value added, stock-based compensation, labor cost, marketing cost, ...), and
do not hardcode GPPE in the engineering structure. #59 item 2 already said it: formula
variants are configuration, not pipeline changes.

A **node** is one metric with a meaning: its unit, what period it describes, which issuer
classes it applies to, where its value comes from, how confident it can be, and what a
negative value means. A **decomposition** is a typed edge from one derived node to its
operands under a versioned formula; its operands may differ per issuer class, which is how a
class-specific proxy or a class-specific capital charge is declared instead of coded. A
**tree** is a registered, versioned set of decompositions under one root that realizes a
**concept** (a question with no value of its own, such as labor efficiency). Several trees may
realize the same concept; a derived node has exactly one decomposition across the whole
forest, so its value never depends on which tree reached it and one wide-row column per node
is well defined.

Everything here is data and pure arithmetic. Factors still never see provenance (init.md
rule 3): `Provenance` names the registry entry a node is fused from, not a vendor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from functools import reduce
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.metrics import METRICS, UnitFamily


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _sorted_classes(values: frozenset[IssuerClass]) -> list[str]:
    # A set dumps in hash order, which varies between processes; identities must not.
    return sorted(value.value for value in values)


class IssuerClass(StrEnum):
    """The classes a node or a decomposition may treat differently. The values are the
    `operating_branch` values the TOPT snapshot and `mart.topt_*_results` already carry."""

    NON_FINANCIAL = "non_financial"
    FINANCIAL = "financial"
    INSURANCE = "insurance"


ALL_ISSUER_CLASSES: frozenset[IssuerClass] = frozenset(IssuerClass)
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


class PeriodSemantics(StrEnum):
    #: A flow over the fiscal year (income statement).
    FISCAL_YEAR_FLOW = "fiscal-year-flow"
    #: A stock at the fiscal year end (balance sheet, headcount).
    FISCAL_YEAR_END_STOCK = "fiscal-year-end-stock"
    #: As of the snapshot cutoff (price, market value).
    CUTOFF_INSTANT = "cutoff-instant"
    #: A definition parameter; describes no period.
    DEFINITIONAL = "definitional"
    #: A concept carries no value, so no period.
    NONE = "none"


class SignPolicy(StrEnum):
    """What a negative value of a node means (init.md rule 17: recorded per component with the
    versioned definition; the output invariants assert exactly this)."""

    #: Arithmetic may make it negative and that says nothing by itself.
    MAY_BE_NEGATIVE = "may-be-negative"
    #: A negative value is a defect: the invariant suite and the tick's gate refuse it.
    MUST_BE_NON_NEGATIVE = "must-be-non-negative"
    #: A negative value is a valid, ranked low signal. It passes, and it is printed by name on
    #: every check so it is never hidden.
    SIGN_IS_SIGNAL = "sign-is-signal"


class AliasKind(StrEnum):
    """Typed aliases: the node id is the UUID; every name it goes by elsewhere is an alias of
    one kind (#877: entities are identified by UUIDs with typed aliases)."""

    #: The forest's own readable key.
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
    a definition parameter, or the forest itself. Never a vendor (init.md rule 3)."""

    kind: ProvenanceKind
    #: The `METRICS` name for a registry input; the parameter name; the tree key otherwise.
    reference: str = Field(min_length=1)
    #: How the registry entry is specialised for this node, e.g. the financial-issuer split
    #: of `gross_profit` that a bank reports as pre-provision profit.
    note: str = ""

    @model_validator(mode="after")
    def _registered(self) -> Self:
        if self.kind is ProvenanceKind.METRIC_REGISTRY and self.reference not in METRICS:
            raise ValueError(f"provenance names {self.reference!r}, which the metric registry does not declare")
        return self


class ConfidenceRule(StrEnum):
    #: The selected observation's own confidence (fusion carries it; the node does not set it).
    AS_CAPTURED = "as-captured"
    #: A composite never exceeds the minimum confidence it consumed (init.md §7).
    MINIMUM_CONSUMED = "minimum-consumed"
    #: A definition parameter is a declaration, not a measurement.
    DEFINITIONAL = "definitional"


class ConfidenceBand(_Frozen):
    """How a node's confidence is formed and which confidence-report family bands it."""

    rule: ConfidenceRule
    #: The `data_engine.datahub.confidence_report` family an input is banded under (its
    #: high/medium/low/missing band). None for a derived node, whose band is the worst band it
    #: consumed, and for a parameter, which is declared rather than measured.
    family: str | None = None

    @model_validator(mode="after")
    def _family(self) -> Self:
        if (self.rule is ConfidenceRule.AS_CAPTURED) != (self.family is not None):
            raise ValueError("exactly the captured inputs name a confidence-report family")
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
    confidence: ConfidenceBand | None
    aliases: tuple[NodeAlias, ...] = ()

    @field_serializer("applicability")
    def _dump_applicability(self, values: frozenset[IssuerClass]) -> list[str]:
        return _sorted_classes(values)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind is NodeKind.CONCEPT:
            if self.unit is not None or self.sign_policy or self.confidence is not None:
                raise ValueError(f"concept {self.key} carries no value, so no unit, sign policy or confidence")
            if self.period is not PeriodSemantics.NONE:
                raise ValueError(f"concept {self.key} describes no period")
        else:
            if self.unit is None or self.confidence is None:
                raise ValueError(f"node {self.key} must declare its unit and confidence band")
            if set(self.sign_policy) != set(self.applicability):
                raise ValueError(f"node {self.key} must declare a sign policy for exactly its applicable classes")
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


FormulaId = Literal["identity", "sum", "difference", "product", "ratio"]


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


#: (formula id, version) -> (operand arity or None for "two or more", function). A formula
#: version is immutable: a different arithmetic is a new version, never an edit.
FORMULAS: Mapping[tuple[str, int], tuple[int | None, Callable[[Sequence[Decimal]], Decimal | None]]] = {
    ("identity", 1): (1, _identity),
    ("sum", 1): (None, _sum),
    ("difference", 1): (2, _difference),
    ("product", 1): (None, _product),
    ("ratio", 1): (2, _ratio),
}


class Decomposition(_Frozen):
    """A typed edge: `output` = formula(operands), with the operands bound per issuer class.

    A class-specific proxy (a bank's pre-provision profit standing in for gross profit) and a
    class-specific capital charge (a different base or rate for one class) are both operand
    bindings here, not branches in factor code.
    """

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
    """A registered, versioned set of decompositions under one root. Its content hash covers
    every decomposition, every node it reaches and the arithmetic context, so any edit to what
    the tree computes changes the hash and must come with a new version."""

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
    def _validate_tree(tree: MetricTree, by_key: Mapping[str, MetricNode]) -> None:
        concept = by_key.get(tree.realizes)
        if concept is None or concept.kind is not NodeKind.CONCEPT:
            raise ValueError(f"tree {tree.key} must realize a registered concept")
        if len({item.output for item in tree.decompositions}) != len(tree.decompositions):
            raise ValueError(f"tree {tree.key} decomposes a node twice")
        for decomposition in tree.decompositions:
            output = by_key.get(decomposition.output)
            if output is None or output.kind is not NodeKind.DERIVED:
                raise ValueError(f"tree {tree.key}: {decomposition.output} is not a registered derived node")
            if set(decomposition.operands) != set(tree.applicability):
                raise ValueError(f"tree {tree.key}: {decomposition.output} must bind operands for every class")
            if not tree.applicability <= output.applicability:
                raise ValueError(f"tree {tree.key}: {decomposition.output} does not apply to every class of the tree")
            for issuer_class, operands in decomposition.operands.items():
                for operand in operands:
                    node = by_key.get(operand)
                    if node is None or node.kind is NodeKind.CONCEPT:
                        raise ValueError(f"tree {tree.key}: operand {operand} is not a registered valued node")
                    if issuer_class not in node.applicability:
                        raise ValueError(f"tree {tree.key}: {operand} does not apply to {issuer_class}")
                    if node.kind is NodeKind.DERIVED and tree.decomposition_of(operand) is None:
                        raise ValueError(f"tree {tree.key}: {operand} is derived but not decomposed in this tree")
        # Every decomposition is reachable from the root, and nothing is cyclic.
        reached: set[str] = set()

        def visit(key: str, path: tuple[str, ...]) -> None:
            if key in path:
                raise ValueError(f"tree {tree.key} is cyclic at {' -> '.join((*path, key))}")
            decomposition = tree.decomposition_of(key)
            if decomposition is None:
                return
            reached.add(key)
            for operands in decomposition.operands.values():
                for operand in operands:
                    visit(operand, (*path, key))

        root = by_key.get(tree.root)
        if root is None or root.kind is not NodeKind.DERIVED:
            raise ValueError(f"tree {tree.key}: root {tree.root} is not a registered derived node")
        visit(tree.root, ())
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

    def tree_sha256(self, tree: MetricTree) -> str:
        """The tree's identity: its own fields plus every node it reaches, so a changed unit,
        sign policy or alias of a reached node is a changed tree too."""
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
    #: node key -> why it is undefined (`missing_input`, `zero_denominator`, `undefined_operand`).
    undefined: Mapping[str, str] = {}


def evaluate(
    forest: Forest,
    tree: MetricTree,
    *,
    issuer_class: IssuerClass,
    inputs: Mapping[str, Decimal | None],
) -> TreeEvaluation:
    """Evaluate `tree` for one issuer of `issuer_class`. `inputs` maps input and parameter node
    keys to values. Arithmetic runs in the tree's declared decimal context, operand order as
    declared, so a registered tree reproduces the hand-written kernel it replaced digit for
    digit. A missing input or a zero denominator makes the affected nodes undefined; it never
    raises and never substitutes a value."""
    values: dict[str, Decimal | None] = {}
    undefined: dict[str, str] = {}

    def resolve(key: str) -> Decimal | None:
        if key in values:
            return values[key]
        decomposition = tree.decomposition_of(key)
        if decomposition is None:
            value = inputs.get(key)
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
            undefined[key] = "zero_denominator"
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
        """A negative value the node forbids, or a value published for a class the node does not
        declare (then no policy can vouch for it)."""
        return self.policy is None or self.policy is SignPolicy.MUST_BE_NON_NEGATIVE

    @property
    def signal(self) -> bool:
        return self.policy is SignPolicy.SIGN_IS_SIGNAL


def judge_sign(node: MetricNode, *, issuer_class: str, value: Decimal | None, subject: str) -> SignFinding | None:
    """The finding for one published value, or None when there is nothing to say: the value is
    absent, non-negative for a class the node declares, or negative where the node allows it
    without meaning anything. A value published for a class the node does not declare is a
    finding whatever its sign, because no policy vouches for it."""
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
