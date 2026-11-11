"""Module 3: Supply-chain relationship graph and exposure (#772)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factors.registry import factor
from factors.types import FactorResult, UnitFamily, refused_factor_result

_ZERO = Decimal(0)
_ONE = Decimal(1)

NO_SUPPLY_CHAIN_EXTRACTION = "no_supply_chain_extraction"
NO_DISCLOSED_SUPPLIERS = "no_disclosed_suppliers"
UNQUANTIFIED_PARTNER_SHARES = "unquantified_partner_shares"
CUSTOMER_ONLY_GRAPH = "customer_only_graph"


@dataclass(frozen=True)
class SupplyChainPartner:
    partner_id: str
    partner_name: str
    relation_type: str
    revenue_share: Decimal | None = None
    confidence: Decimal = _ZERO


@dataclass(frozen=True)
class SupplyChainExposure:
    entity_id: str
    result: FactorResult
    exposure_score: Decimal | None
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
    supplies_to_edges_exist: bool,
) -> SupplyChainExposure:
    if partners and not supplies_to_edges_exist:
        raise ValueError(
            f"{entity_id} has {len(partners)} partners but supplies_to_edges_exist is false: "
            "the graph cannot hold a partner without a supplies_to edge"
        )
    if not partners:
        flag = NO_DISCLOSED_SUPPLIERS if supplies_to_edges_exist else NO_SUPPLY_CHAIN_EXTRACTION
        return SupplyChainExposure(
            entity_id=entity_id,
            result=refused_factor_result("supply_chain_exposure", entity_id, as_of, [flag]),
            exposure_score=None,
            direct_partners=0,
            suppliers_count=0,
            customers_count=0,
            max_partner_share=_ZERO,
        )

    suppliers = [p for p in partners if p.relation_type == "supplier"]
    customers = [p for p in partners if p.relation_type == "customer"]
    direct_partners = len(partners)
    shares = [p.revenue_share for p in partners if p.revenue_share is not None and p.revenue_share > _ZERO]
    max_share = max(shares) if shares else _ZERO
    exposure = (
        min(_ONE, sum((s * s for s in shares), start=_ZERO))
        if shares
        else min(_ONE, _ONE / Decimal(max(1, direct_partners)))
    )
    flags = [
        *([UNQUANTIFIED_PARTNER_SHARES] if not shares else []),
        *([CUSTOMER_ONLY_GRAPH] if not suppliers else []),
    ]
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
            data_availability="unverified",
            flags=flags,
        ),
        exposure_score=exposure,
        direct_partners=direct_partners,
        suppliers_count=len(suppliers),
        customers_count=len(customers),
        max_partner_share=max_share,
    )
