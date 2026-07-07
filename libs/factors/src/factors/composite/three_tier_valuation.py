"""Module 7: three-tier valuation tagging (traditional / tech / large-model-native P/S tier).

Reads module 2's mart output (and other base factors as needed). Verification
(Phase 2.5): cross-check against companies with an obvious, undisputed tier.
"""

from collections.abc import Sequence
from datetime import datetime

from factors.registry import factor
from factors.types import FactorResult


@factor("three_tier_valuation", kind="composite", module=7)
def three_tier_valuation(
    inputs: Sequence[FactorResult],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    raise NotImplementedError("Phase 2.5")
