"""Module 3: Supply-chain relationship graph and exposure (#772, init.md §7 item 3).

init.md §0 question 3: *"What is this company exposed to, up and down its supply chain?"*
Module 3 computes supply chain concentration, dependency exposure, and partner counts
over verified `staging.kg_edges` relationships (`relation_type='supplies_to'`).

When no supply chain edges are disclosed or verified for an issuer, the factor refuses
cleanly with `no_disclosed_suppliers`, maintaining honest status and avoiding synthetic defaults.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import FactorResult, UnitFamily

_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass(frozen=True)
class SupplyChainPartner:
    """One verified supply-chain relationship edge."""

    partner_id: str
    partner_name: str
    relation_type: str  # 'supplier' | 'customer'
    revenue_share: Decimal | None = None  # Fraction of revenue (e.g. 0.15 for 15%)
    confidence: Decimal = _ZERO


@dataclass(frozen=True)
class SupplyChainExposure:
    """The supply chain exposure index and partner breakdown."""

    entity_id: str
    result: FactorResult
    exposure_score: Decimal | None  # 0.0 (fully diversified) to 1.0 (single-source / maximum concentration)
    direct_partners: int
    suppliers_count: int
    customers_count: int
    max_partner_share: Decimal

    @property
    def value(self) -> Decimal | None:
        return self.result.value


@factor("supply_chain_exposure", kind="base", module=3)
def supply_chain_exposure(
    partners: Sequence[SupplyChainPartner],
    *,
    entity_id: str,
    as_of: datetime,
) -> SupplyChainExposure:
    """Compute supply chain exposure and partner concentration.

    If partners is empty, returns an unavailable result with reason 'no_disclosed_suppliers'.
    """
    if not partners:
        return SupplyChainExposure(
            entity_id=entity_id,
            result=FactorResult(
                factor="supply_chain_exposure",
                entity_id=entity_id,
                value=None,
                unit_family=UnitFamily.RATIO,
                confidence=_ZERO,
                as_of=as_of,
                data_availability="unverified",
                flags=["no_disclosed_suppliers"],
            ),
            exposure_score=None,
            direct_partners=0,
            suppliers_count=0,
            customers_count=0,
            max_partner_share=_ZERO,
        )

    suppliers = [p for p in partners if p.relation_type == "supplier"]
    customers = [p for p in partners if p.relation_type == "customer"]
    direct_partners = len(partners)

    # Compute concentration score based on disclosed shares (or equal-weight proxy)
    shares = [p.revenue_share for p in partners if p.revenue_share is not None and p.revenue_share > _ZERO]
    max_share = max(shares) if shares else _ZERO

    if shares:
        # Herfindahl-Hirschman Index normalized to [0, 1]
        exposure = min(_ONE, sum((s * s for s in shares), start=_ZERO))
    else:
        # If no explicit shares disclosed, exposure inversely scales with partner count
        exposure = min(_ONE, _ONE / Decimal(max(1, direct_partners)))

    confidences = [p.confidence for p in partners if p.confidence > _ZERO]
    avg_confidence = (sum(confidences, start=_ZERO) / Decimal(len(confidences))) if confidences else Decimal("0.8")

    return SupplyChainExposure(
        entity_id=entity_id,
        result=FactorResult(
            factor="supply_chain_exposure",
            entity_id=entity_id,
            value=exposure,
            unit_family=UnitFamily.RATIO,
            confidence=avg_confidence,
            as_of=as_of,
            data_availability="verified",
            flags=[],
        ),
        exposure_score=exposure,
        direct_partners=direct_partners,
        suppliers_count=len(suppliers),
        customers_count=len(customers),
        max_partner_share=max_share,
    )
