"""Module 2: gross profit per employee (v0 capital-adjusted labor efficiency).

Two named metrics (#1176, owner decision 2026-10-09). Each has its own forest node:

- `gross_profit_per_employee` is `gppe_uniform_charge_v0`. Its capital base is total assets for
  every issuer. It has no ratio test and no class branch.
- `gross_profit_per_employee_banking_tce` is `gppe_banking_tce_v1`. Its capital base is measured
  tangible common equity. The strategy applies it to FINANCIAL issuers only. Missing equity,
  goodwill or intangibles gives an unavailable result. It never falls back to total assets.

The uniform formula is frozen by issue #59 (2026-07-18 owner decision): one definition
applies to every issuer, financial or not. The retired 8% leverage branch is gone.

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
(`GPPE_EXPRESSION_DEFINITION`), built only from shared
`truealpha_contracts.ast` node types — no factor-specific node — so a
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
    """`gppe_uniform_charge_v0`: the uniform total-assets charge for every issuer class."""
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


_STOCKHOLDERS_EQUITY = "stockholders_equity"
_PREFERRED_STOCK = "preferred_stock_value"
_GOODWILL = "goodwill"
_INTANGIBLE_ASSETS = "intangible_assets_net_excluding_goodwill"

#: The metrics `gppe_banking_tce_v1` reads. Total assets is not one of them.
BANKING_TCE_INPUTS = (
    _GROSS_PROFIT,
    _STOCKHOLDERS_EQUITY,
    _PREFERRED_STOCK,
    _GOODWILL,
    _INTANGIBLE_ASSETS,
    _EMPLOYEES_TOTAL,
)


def gross_profit_per_employee_banking_tce(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
    risk_free_rate: Decimal,
) -> FactorResult:
    """`gppe_banking_tce_v1` (#1176): `(gross_profit - TCE * risk_free_rate) / headcount`.

    TCE is `stockholders_equity - preferred_stock_value - (goodwill + intangible_assets_net_excluding_goodwill)`.
    It is measured, never estimated. A missing equity, goodwill or intangible input gives the flag
    `missing_tangible_common_equity`. A missing preferred value gives `missing_preferred_stock_value`.
    Neither input is zero-filled. The factor never substitutes total assets or a ratio.
    """
    gross_profit = _find(facts, entity_id, _GROSS_PROFIT)
    equity = _find(facts, entity_id, _STOCKHOLDERS_EQUITY)
    preferred = _find(facts, entity_id, _PREFERRED_STOCK)
    goodwill = _find(facts, entity_id, _GOODWILL)
    intangibles = _find(facts, entity_id, _INTANGIBLE_ASSETS)
    headcount = _find(facts, entity_id, _EMPLOYEES_TOTAL)
    tce_inputs = (equity, goodwill, intangibles)

    flags: list[str] = []
    if gross_profit is None or gross_profit.value is None:
        flags.append("missing_gross_profit")
    if any(item is None or item.value is None for item in tce_inputs):
        flags.append("missing_tangible_common_equity")
    if preferred is None or preferred.value is None:
        flags.append("missing_preferred_stock_value")
    if headcount is None or headcount.value is None:
        flags.append("missing_employees_total")
    elif headcount.value <= 0:
        flags.append("non_positive_employees_total")
    present = [item for item in (gross_profit, headcount, preferred, *tce_inputs) if item is not None]
    if not flags and len({item.fiscal_period for item in present}) > 1:
        flags.append("fiscal_period_mismatch")

    if flags:
        return FactorResult(
            factor="gppe_banking_tce_v1",
            entity_id=entity_id,
            value=None,
            unit_family=UnitFamily.PER_EMPLOYEE,
            confidence=Decimal("0"),
            as_of=as_of,
            data_availability="unverified",
            flags=flags,
        )

    assert gross_profit is not None and headcount is not None
    assert equity is not None and goodwill is not None and intangibles is not None
    assert gross_profit.value is not None and headcount.value is not None
    assert equity.value is not None and goodwill.value is not None and intangibles.value is not None
    assert preferred is not None and preferred.value is not None

    tangible_common_equity = equity.value - preferred.value - (goodwill.value + intangibles.value)
    real_profit = gross_profit.value - tangible_common_equity * risk_free_rate
    value = real_profit / headcount.value
    confidence = min(item.confidence for item in (gross_profit, headcount, preferred, *tce_inputs) if item is not None)

    return FactorResult(
        factor="gppe_banking_tce_v1",
        entity_id=entity_id,
        value=value,
        unit_family=UnitFamily.PER_EMPLOYEE,
        confidence=confidence,
        as_of=as_of,
        data_availability="unverified",
        flags=[],
    )
