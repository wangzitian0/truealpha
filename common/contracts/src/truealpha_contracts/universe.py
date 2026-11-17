"""Immutable universe and point-in-time membership contracts."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, Field, ValidationInfo, model_validator

from truealpha_contracts.common import (
    STABLE_ID_PATTERN,
    AwareDatetime,
    FrozenModel,
    Sha256,
)

_MUTABLE_REFERENCE_MARKERS = frozenset({"current", "head", "latest"})


def _require_stable_reference(value: str, field_name: str) -> str:
    if value != value.strip() or not re.fullmatch(STABLE_ID_PATTERN, value):
        raise ValueError(f"{field_name} must be a non-empty stable identifier")
    mutable_markers = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token} & _MUTABLE_REFERENCE_MARKERS
    if mutable_markers:
        raise ValueError(f"{field_name} cannot use a mutable reference marker: {sorted(mutable_markers)}")
    return value


def _stable_reference_field(value: str, info: ValidationInfo) -> str:
    return _require_stable_reference(value, info.field_name or "reference")


_StableReference = Annotated[str, AfterValidator(_stable_reference_field)]


class SubjectKind(StrEnum):
    ISSUER = "issuer"
    SECURITY = "security"
    LISTING = "listing"
    FUND = "fund"
    ANALYST = "analyst"
    UNIVERSE = "universe"
    THEME = "theme"


class SubjectRef(FrozenModel):
    kind: SubjectKind
    id: _StableReference


class UniverseRef(FrozenModel):
    universe_id: _StableReference
    universe_version: _StableReference
    content_sha256: Sha256


class UniverseMembership(FrozenModel):
    membership_id: _StableReference
    universe_id: _StableReference
    subject: SubjectRef
    valid_from: date
    valid_to: date | None = None
    knowable_at: AwareDatetime
    recorded_at: AwareDatetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_membership(self) -> UniverseMembership:
        if self.subject.kind is SubjectKind.UNIVERSE:
            raise ValueError("a universe cannot contain another universe implicitly")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self
