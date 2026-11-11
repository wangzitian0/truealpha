"""Module 5: ETF-as-virtual-company consolidation definition (#36, #727)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from truealpha_contracts.common import FrozenModel, canonical_sha256


class EtfConsolidationDefinition(FrozenModel):
    factor_key: Literal["etf_virtual_company"] = "etf_virtual_company"
    factor_version: str = Field(pattern=r"^v[0-9]+$")
    weighting: Literal["filed_net_asset_percent"] = "filed_net_asset_percent"
    renormalization: Literal["valued_mass"] = "valued_mass"
    minimum_resolved_weight: Decimal = Field(ge=0, le=100)
    minimum_valued_weight: Decimal = Field(ge=0, le=100)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


ETF_CONSOLIDATION_V0 = EtfConsolidationDefinition(
    factor_version="v0",
    minimum_resolved_weight=Decimal("50"),
    minimum_valued_weight=Decimal("20"),
)
