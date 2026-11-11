"""Module 5: ETF as a virtual company (#36, #727)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.etf_virtual_company import EtfConsolidationDefinition

from factors.registry import factor
from factors.types import DataAvailability, FactorResult, UnitFamily, refused_factor_result

_ZERO = Decimal(0)


@dataclass(frozen=True)
class HoldingLine:
    holding_name: str
    weight: Decimal | None
    listing_id: str | None
    valuation_gap: Decimal | None
    availability: str | None
    confidence: Decimal | None = None
    availability_status: str | None = None

    @property
    def resolved(self) -> bool:
        return self.listing_id is not None

    @property
    def valued(self) -> bool:
        core_row_status = self.availability_status if self.availability_status is not None else self.availability
        return (
            self.weight is not None
            and self.resolved
            and core_row_status == "available"
            and self.valuation_gap is not None
        )


@dataclass(frozen=True)
class FundConsolidation:
    fund_id: str
    result: FactorResult
    total_weight: Decimal
    resolved_weight: Decimal
    valued_weight: Decimal
    lines: int
    valued_lines: int

    @property
    def unresolved_weight(self) -> Decimal:
        return self.total_weight - self.resolved_weight

    @property
    def unvalued_weight(self) -> Decimal:
        return self.resolved_weight - self.valued_weight

    @property
    def weighted_valuation_gap(self) -> Decimal | None:
        return self.result.value


def _mass(lines: Sequence[HoldingLine], predicate: Callable[[HoldingLine], bool]) -> Decimal:
    return sum((line.weight for line in lines if line.weight is not None and predicate(line)), _ZERO)


@factor("etf_virtual_company", kind="base", module=5)
def consolidate_fund(
    lines: Sequence[HoldingLine],
    *,
    fund_id: str,
    as_of: datetime,
    definition: EtfConsolidationDefinition,
) -> FundConsolidation:
    total = _mass(lines, lambda _: True)
    resolved = _mass(lines, lambda line: line.resolved)
    valued = _mass(lines, lambda line: line.valued)
    valued_lines = [line for line in lines if line.valued]

    def _wrap(result: FactorResult) -> FundConsolidation:
        return FundConsolidation(fund_id, result, total, resolved, valued, len(lines), len(valued_lines))

    if resolved < definition.minimum_resolved_weight:
        return _wrap(refused_factor_result("etf_virtual_company", fund_id, as_of, ["resolved_weight_below_minimum"]))
    if valued < definition.minimum_valued_weight:
        return _wrap(refused_factor_result("etf_virtual_company", fund_id, as_of, ["valued_weight_below_minimum"]))
    if valued <= _ZERO:
        return _wrap(refused_factor_result("etf_virtual_company", fund_id, as_of, ["no_valued_mass"]))

    weighted = sum(
        (line.weight * line.valuation_gap for line in valued_lines if line.weight is not None),  # type: ignore[operator]
        _ZERO,
    )
    confidences = [line.confidence for line in valued_lines if line.confidence is not None]
    availability: DataAvailability = "verified" if valued >= total else "unverified"
    flags = [
        *(["partial_valued_mass"] if valued < resolved else []),
        *(["unresolved_holdings"] if resolved < total else []),
    ]
    return _wrap(
        FactorResult(
            factor="etf_virtual_company",
            entity_id=fund_id,
            value=weighted / valued,
            unit_family=UnitFamily.RATIO,
            confidence=min(confidences) if confidences else _ZERO,
            as_of=as_of,
            data_availability=availability,
            flags=flags,
        )
    )
