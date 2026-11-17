"""Module 6: content-addressed theme-purity definitions (#772)."""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from pydantic import Field
from truealpha_contracts.common import CanonicalHash, FrozenModel, load_data_json


class ThemeDefinition(CanonicalHash, FrozenModel):
    factor_key: Literal["theme_purity"] = "theme_purity"
    factor_version: str = Field(pattern=r"^v[0-9]+$")
    theme_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    theme: str = Field(min_length=1)
    inclusion: str = Field(min_length=1)
    minimum_classified_share: Decimal = Field(ge=0, le=1)


THEMES: MappingProxyType[str, ThemeDefinition] = MappingProxyType(
    {t["theme_id"]: ThemeDefinition.model_validate(t) for t in load_data_json("registries.v1.json")["themes"]}
)
