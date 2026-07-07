"""Module 2: gross profit per employee.

Financial companies have no gross-profit field (industry branch required);
headcount gaps must surface in FactorResult.flags, never be silently dropped.
Implementation lands in Phase 2.
"""

from collections.abc import Sequence
from datetime import datetime

from factors.registry import factor
from factors.types import Fact, FactorResult


@factor("gross_profit_per_employee", kind="base", module=2)
def gross_profit_per_employee(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    raise NotImplementedError("Phase 2")
