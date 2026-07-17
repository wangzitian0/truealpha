"""Module 2: gross profit per employee (v0 capital-adjusted labor efficiency).

Formula frozen by issue #59 (2026-07-17 owner decision, round 3):

    real_profit_v0 = gross_profit - total_assets * risk_free_rate
    labor_efficiency_v0 = real_profit_v0 / employee_headcount

v0 deliberately uses only the two rigid standardized fields #59 names for the
capital charge (gross profit, total assets). It does not yet apply the round-2
financial pre-provision-profit proxy or the operating-vs-investment income
decomposition — #59 explicitly defers that as "a later calibration step, not
v0." A financial issuer that does not disclose `gross_profit` is therefore
unavailable under v0 and must surface that in `flags`, never be silently
substituted or dropped.

`risk_free_rate` is a versioned parameter (#59: "3-month US T-bill yield (v0
default; versioned parameter)"), not a live per-period market fact this factor
looks up itself — the caller supplies the frozen v0 value, exactly like
`growth_convention` is an explicit parameter to the PEG factor.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import Fact, FactorResult

_GROSS_PROFIT = "gross_profit"
_TOTAL_ASSETS = "total_assets"
_EMPLOYEE_HEADCOUNT = "employee_headcount"


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
) -> FactorResult:
    gross_profit = _find(facts, entity_id, _GROSS_PROFIT)
    total_assets = _find(facts, entity_id, _TOTAL_ASSETS)
    headcount = _find(facts, entity_id, _EMPLOYEE_HEADCOUNT)

    flags: list[str] = []
    if gross_profit is None or gross_profit.value is None:
        flags.append("missing_gross_profit")
    if total_assets is None or total_assets.value is None:
        flags.append("missing_total_assets")
    if headcount is None or headcount.value is None:
        flags.append("missing_employee_headcount")
    elif headcount.value <= 0:
        flags.append("non_positive_employee_headcount")
    if (
        not flags
        and gross_profit is not None
        and total_assets is not None
        and headcount is not None
        and len({gross_profit.fiscal_period, total_assets.fiscal_period, headcount.fiscal_period}) > 1
    ):
        flags.append("fiscal_period_mismatch")

    if flags:
        return FactorResult(
            factor="gross_profit_per_employee",
            entity_id=entity_id,
            value=None,
            confidence=Decimal("0"),
            as_of=as_of,
            data_availability="unverified",
            flags=flags,
        )

    assert gross_profit is not None and total_assets is not None and headcount is not None
    assert gross_profit.value is not None and total_assets.value is not None and headcount.value is not None

    real_profit = gross_profit.value - total_assets.value * risk_free_rate
    value = real_profit / headcount.value
    confidence = min(gross_profit.confidence, total_assets.confidence, headcount.confidence)

    return FactorResult(
        factor="gross_profit_per_employee",
        entity_id=entity_id,
        value=value,
        confidence=confidence,
        as_of=as_of,
        data_availability="unverified",
        flags=[],
    )
