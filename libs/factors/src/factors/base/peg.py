"""Module 1: PEG with switchable growth-rate conventions.

This stub fixes the signature convention for every base factor: facts in,
FactorResult (with confidence) out, `as_of` explicit. Implementation lands in Phase 1.
"""

from collections.abc import Sequence
from datetime import datetime

from factors.registry import factor
from factors.types import Fact, FactorResult, GrowthConvention


@factor("peg", kind="base", module=1)
def peg(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    growth_convention: GrowthConvention,
    as_of: datetime,
) -> FactorResult:
    raise NotImplementedError("Phase 1")
