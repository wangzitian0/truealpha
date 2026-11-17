"""Immutable subject and universe coordinate contracts."""

from __future__ import annotations

from enum import StrEnum

from truealpha_contracts.common import FrozenModel, ImmutableId, Sha256


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
    id: ImmutableId


class UniverseRef(FrozenModel):
    universe_id: ImmutableId
    universe_version: ImmutableId
    content_sha256: Sha256
