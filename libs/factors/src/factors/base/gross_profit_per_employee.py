"""Module 2: gross profit per employee (v0 capital-adjusted labor efficiency).

Non-financial formula frozen by issue #59 (2026-07-17 owner decision, round 3):

    real_profit_v0 = gross_profit - total_assets * risk_free_rate
    labor_efficiency_v0 = real_profit_v0 / employees_total

Financial issuers (`issuer_branch="financial"`) take the mandatory #59 factor
branch instead: `gross_profit` is the parser's industry-branch definition for
financial issuers (`truealpha_contracts.metrics.METRICS["gross_profit"]` is
declared `financial_issuer_split=True` for exactly this reason -- a bank's
pre-provision-profit proxy, not a cost-of-revenue subtotal it never reports),
and no capital charge is subtracted:

    financial_efficiency_v0 = gross_profit / employees_total

A capital-charge subtraction using `total_assets` does not apply to a
financial issuer whose balance sheet size *is* the business rather than
deployed capital against it -- subtracting `total_assets * risk_free_rate`
would swamp a bank's profit and produce a meaningless negative result. This
mirrors the convention two independent implementations already converged on
(`factors.batches.core_strategy_tiny.e0_slice._compute_level` and
`factors.production_topt.core.compute_topt_gppe`), so `total_assets` is not
required and not consumed for the financial branch. `issuer_branch` is an
explicit, required caller input (like `factors.batches.core_strategy_tiny`'s
`IssuerBranch` and `factors.production_topt.core`'s `OperatingBranch`) — a
legitimate semantic classification of the subject, not vendor/provenance
metadata this factor is forbidden from branching on.

This still does not implement #59's fuller "operating-vs-investment profit
decomposition" (investment returns minus a risk-free return on investable
assets) — that remains a later calibration step. What v0 now guarantees is
that a financial issuer's branch is actually computed, never silently skipped
via a missing-fact fallthrough; whether its output is usable for P/S-tier
valuation is a separate, explicit strategy-eligibility decision (see
`truealpha_contracts.strategy.ExclusionReason.FINANCIAL_VALUATION_NOT_COMPARABLE`),
made by the caller, not this factor.

`risk_free_rate` is a versioned parameter (#59: "3-month US T-bill yield (v0
default; versioned parameter)"), not a live per-period market fact this factor
looks up itself — the caller supplies the frozen v0 value, exactly like
`growth_convention` is an explicit parameter to the PEG factor. It is unused
for the financial branch (accepted but ignored, never silently reinterpreted).

The non-financial arithmetic is also expressed as a matrix-compatible Qlib
expression (`GPPE_EXPRESSION_DEFINITION`), built only from the approved
Add/Div/Mean/Mul/Ref/Sub operators, so a pinned-Qlib execution through
`factors.qlib_engine` reproduces this function's Decimal output — proven by
the cross-check test, not invoked on every call (the Decimal path above is
the fast, dependency-light source of truth; Qlib execution is the
reproducibility proof, and the reusable engine it runs through is meant to
carry future base factors expressible the same way). The financial branch has
no Qlib expression yet — a known, separate gap from #21's criterion 3, not
silently assumed equivalent to the non-financial expression.

Input metric names match the canonical registry
(`truealpha_contracts.metrics.METRICS`) so staging fusion and factor
consumption never drift apart; `Fact` itself rejects a metric/unit_family
combination that doesn't match that registry (see `factors.types.Fact`), so
this function does not re-check unit compatibility.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from truealpha_contracts.qlib_expression import (
    QlibCallNode,
    QlibFactorExpressionDefinition,
    QlibFeatureBinding,
    QlibFeatureNode,
)

from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY
from factors.registry import factor
from factors.types import Fact, FactorResult, UnitFamily

_GROSS_PROFIT = "gross_profit"
_TOTAL_ASSETS = "total_assets"
_EMPLOYEES_TOTAL = "employees_total"

GPPE_EXPRESSION_DEFINITION = QlibFactorExpressionDefinition(
    factor_id="factor.gross_profit_per_employee.capital_adjusted",
    factor_version="0.1.0",
    operator_registry_id=BUILTIN_OPERATOR_REGISTRY.operator_registry_id,
    feature_bindings=(
        QlibFeatureBinding(feature_binding_id="feature.gross_profit", qlib_field_name="gross_profit"),
        QlibFeatureBinding(feature_binding_id="feature.total_assets", qlib_field_name="total_assets"),
        QlibFeatureBinding(feature_binding_id="feature.risk_free_rate", qlib_field_name="risk_free_rate"),
        QlibFeatureBinding(feature_binding_id="feature.employees_total", qlib_field_name="employees_total"),
    ),
    root=QlibCallNode(
        operator_id="truealpha.qlib.div.v1",
        arguments=(
            QlibCallNode(
                operator_id="truealpha.qlib.sub.v1",
                arguments=(
                    QlibFeatureNode(feature_binding_id="feature.gross_profit"),
                    QlibCallNode(
                        operator_id="truealpha.qlib.mul.v1",
                        arguments=(
                            QlibFeatureNode(feature_binding_id="feature.total_assets"),
                            QlibFeatureNode(feature_binding_id="feature.risk_free_rate"),
                        ),
                    ),
                ),
            ),
            QlibFeatureNode(feature_binding_id="feature.employees_total"),
        ),
    ),
    maximum_lookback_sessions=0,
)


def _find(facts: Sequence[Fact], entity_id: str, metric: str) -> Fact | None:
    # Facts already reflect one PIT-resolved vintage per metric; a factor never
    # re-selects among candidates (init.md Section 6) — take the sole match.
    matches = [f for f in facts if f.entity_id == entity_id and f.metric == metric]
    if len(matches) > 1:
        raise ValueError(f"{entity_id}: multiple PIT-resolved facts for metric {metric!r}")
    return matches[0] if matches else None


@factor("gross_profit_per_employee", kind="base", module=2)
def gross_profit_per_employee(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
    risk_free_rate: Decimal,
    issuer_branch: Literal["non_financial", "financial"],
) -> FactorResult:
    is_financial = issuer_branch == "financial"
    gross_profit = _find(facts, entity_id, _GROSS_PROFIT)
    total_assets = None if is_financial else _find(facts, entity_id, _TOTAL_ASSETS)
    headcount = _find(facts, entity_id, _EMPLOYEES_TOTAL)

    flags: list[str] = []
    if gross_profit is None or gross_profit.value is None:
        flags.append("missing_gross_profit")
    if not is_financial and (total_assets is None or total_assets.value is None):
        flags.append("missing_total_assets")
    if headcount is None or headcount.value is None:
        flags.append("missing_employees_total")
    elif headcount.value <= 0:
        flags.append("non_positive_employees_total")
    if not flags and gross_profit is not None and headcount is not None:
        fiscal_periods = {gross_profit.fiscal_period, headcount.fiscal_period}
        if not is_financial and total_assets is not None:
            fiscal_periods.add(total_assets.fiscal_period)
        if len(fiscal_periods) > 1:
            flags.append("fiscal_period_mismatch")

    if flags:
        return FactorResult(
            factor="gross_profit_per_employee",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.PER_EMPLOYEE,
            confidence=Decimal("0"),
            as_of=as_of,
            data_availability="unverified",
            flags=flags,
        )

    assert gross_profit is not None and headcount is not None
    assert gross_profit.value is not None and headcount.value is not None

    if is_financial:
        value = gross_profit.value / headcount.value
        confidence = min(gross_profit.confidence, headcount.confidence)
    else:
        assert total_assets is not None and total_assets.value is not None
        real_profit = gross_profit.value - total_assets.value * risk_free_rate
        value = real_profit / headcount.value
        confidence = min(gross_profit.confidence, total_assets.confidence, headcount.confidence)

    return FactorResult(
        factor="gross_profit_per_employee",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.PER_EMPLOYEE,
        confidence=confidence,
        as_of=as_of,
        data_availability="unverified",
        flags=[],
    )
