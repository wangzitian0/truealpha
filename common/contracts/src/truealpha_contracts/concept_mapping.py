"""Content-addressed vendor concept mapping ruleset (#539)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from truealpha_contracts.common import FrozenModel, identify


class ResolutionKind(StrEnum):
    SYNONYM = "synonym"
    FALLBACK = "fallback"


class ConceptRef(FrozenModel):
    taxonomy: str = Field(min_length=1)
    concept: str = Field(min_length=1)


class FieldMapping(FrozenModel):
    field: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    kind: ResolutionKind
    concepts: tuple[ConceptRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        seen = [(item.taxonomy, item.concept) for item in self.concepts]
        if len(set(seen)) != len(seen):
            raise ValueError(f"{self.field}: concept list repeats an entry")
        return self


class ConceptMappingRuleset(FrozenModel):
    ruleset_id: str = Field(default="", pattern=r"^(?:|concept-mapping:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    version: str = Field(min_length=1)
    mappings: tuple[FieldMapping, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identify(self) -> Self:
        fields = [item.field for item in self.mappings]
        if len(set(fields)) != len(fields):
            raise ValueError("ruleset declares the same field twice")
        try:
            identify(self, id_field="ruleset_id", prefix="concept-mapping")
        except ValueError as err:
            raise ValueError("concept mapping identity does not match its canonical content") from err
        return self

    def mapping_for(self, field: str) -> FieldMapping | None:
        return next((item for item in self.mappings if item.field == field), None)
