"""Module 2: gross profit per employee (v0 capital-adjusted labor efficiency).

Uniform formula frozen by issue #59 (2026-07-18 owner decision): one definition
applies to every issuer, financial or not — no per-issuer arithmetic branch.

    real_profit_v0 = gross_profit - total_assets * risk_free_rate
    labor_efficiency_v0 = real_profit_v0 / employees_total

The capital charge (`total_assets * risk_free_rate`) is subtracted for every
issuer, banks included. A financial issuer whose real profit falls below the
risk-free return on its balance sheet produces a *negative* labor efficiency:
that is a valid low signal ("destroys value per employee at the risk-free
hurdle"), ranked accordingly by the strategy, not a special-case exclusion.
This supersedes the earlier financial branch (which computed
`gross_profit / employees_total` with no capital charge and then marked banks
`financial_valuation_not_comparable`); the owner decision is a single uniform
definition with no blanket sector special-casing.

`gross_profit` is still the parser's industry-branch definition per issuer
(`truealpha_contracts.metrics.METRICS["gross_profit"]` is `financial_issuer_split=True`
so a bank's value is its pre-provision-profit proxy, not a cost-of-revenue
subtotal it never reports). That is a *metric-definition* concern owned by the
parser; the factor consumes whatever grounded `gross_profit` fact it is given
and applies the one uniform formula. `issuer_branch` is therefore no longer an
input to this factor — a bank and a software issuer take the identical path.

This still does not implement #59's fuller "operating-vs-investment profit
decomposition" (investment returns minus a risk-free return on investable
assets) — that remains a later calibration step, a new versioned definition of
this same schema, not a per-issuer branch here.

`risk_free_rate` is a versioned parameter (#59: "3-month US T-bill yield (v0
default; versioned parameter)"), not a live per-period market fact this factor
looks up itself — the caller supplies the frozen v0 value, exactly like
`growth_convention` is an explicit parameter to the PEG factor.

The arithmetic is also expressed as a matrix-compatible AST expression
(`GPPE_EXPRESSION_DEFINITION`), built only from the
Add/Div/Mean/Mul/Ref/Sub/Std/Rank nodes of `truealpha_contracts.ast`, so a
compiled Polars execution through `factors.expressions.compiler` reproduces
this function's Decimal output — proven by the cross-check test, not invoked
on every call (the Decimal path above is the fast, exact source of truth;
the vectorised execution is the reproducibility proof, and the compiler it
runs through is meant to carry future base factors expressible the same way).
Because the formula is now uniform, this single expression covers every issuer.

Input metric names match the canonical registry
(`truealpha_contracts.metrics.METRICS`) so staging fusion and factor
consumption never drift apart; `Fact` itself rejects a metric/unit_family
combination that doesn't match that registry (see `factors.types.Fact`), so
this function does not re-check unit compatibility.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.ast import Div, Feature, Mul, Sub

from factors.registry import factor
from factors.types import Fact, FactorResult, UnitFamily

_GROSS_PROFIT = "gross_profit"
_TOTAL_ASSETS = "total_assets"
_EMPLOYEES_TOTAL = "employees_total"

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


@factor("gross_profit_per_employee", kind="base", module=2, inputs=("gross_profit", "total_assets", "employees_total"))
def gross_profit_per_employee(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
    risk_free_rate: Decimal,
) -> FactorResult:
    gross_profit = _find(facts, entity_id, _GROSS_PROFIT)
    total_assets = _find(facts, entity_id, _TOTAL_ASSETS)
    headcount = _find(facts, entity_id, _EMPLOYEES_TOTAL)

    flags: list[str] = []
    if gross_profit is None or gross_profit.value is None:
        flags.append("missing_gross_profit")
    if total_assets is None or total_assets.value is None:
        flags.append("missing_total_assets")
    if headcount is None or headcount.value is None:
        flags.append("missing_employees_total")
    elif headcount.value <= 0:
        flags.append("non_positive_employees_total")
    if not flags and gross_profit is not None and headcount is not None and total_assets is not None:
        fiscal_periods = {gross_profit.fiscal_period, headcount.fiscal_period, total_assets.fiscal_period}
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

    assert gross_profit is not None and headcount is not None and total_assets is not None
    assert gross_profit.value is not None and headcount.value is not None and total_assets.value is not None

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
