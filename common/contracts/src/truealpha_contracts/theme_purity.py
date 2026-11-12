"""Module 6: content-addressed theme-purity definitions (#772, init.md §0 question 6)."""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from pydantic import Field

from truealpha_contracts.common import CanonicalHash, FrozenModel


class ThemeDefinition(CanonicalHash, FrozenModel):
    factor_key: Literal["theme_purity"] = "theme_purity"
    factor_version: str = Field(pattern=r"^v[0-9]+$")
    theme_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    theme: str = Field(min_length=1)
    inclusion: str = Field(min_length=1)
    minimum_classified_share: Decimal = Field(ge=0, le=1)


THEMES: MappingProxyType[str, ThemeDefinition] = MappingProxyType(
    {
        theme.theme_id: theme
        for theme in (
            ThemeDefinition(
                factor_version="v0",
                theme_id="ai-infrastructure",
                theme="AI infrastructure",
                inclusion=(
                    "Revenue from hardware, software or services whose primary purpose is training "
                    "or serving machine-learning models: accelerators and the networking, memory and "
                    "power systems built for them, data-centre capacity sold for AI workloads, and "
                    "model training or inference sold as a service. General-purpose computing, "
                    "consumer devices, and software that merely embeds a model feature are NOT in "
                    "the theme."
                ),
                minimum_classified_share=Decimal("0.80"),
            ),
            ThemeDefinition(
                factor_version="v0",
                theme_id="cloud-software",
                theme="Cloud software",
                inclusion=(
                    "Revenue from software delivered and billed as a subscription service the vendor "
                    "operates, including the infrastructure and platform capacity sold to run it. "
                    "Perpetual licences, on-premise maintenance, hardware and professional services "
                    "are NOT in the theme."
                ),
                minimum_classified_share=Decimal("0.80"),
            ),
            ThemeDefinition(
                factor_version="v0",
                theme_id="semiconductors",
                theme="Semiconductors",
                inclusion=(
                    "Revenue from designing, manufacturing, packaging or testing integrated circuits, "
                    "and from the equipment and materials used to do so. Systems that merely contain "
                    "semiconductors, and software sold alongside them, are NOT in the theme."
                ),
                minimum_classified_share=Decimal("0.80"),
            ),
        )
    }
)
