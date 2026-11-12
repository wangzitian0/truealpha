"""Immutable universe and point-in-time membership contracts."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, Field, ValidationInfo, field_validator, model_validator

from truealpha_contracts.common import (
    STABLE_ID_PATTERN,
    AwareDatetime,
    FrozenModel,
    Sha256,
    canonical_sha256,
    require_aware,
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


class UniverseDefinitionKind(StrEnum):
    FIXED_COHORT = "fixed_cohort"
    PIT_MEMBERSHIP = "pit_membership"


class UniverseClaimKind(StrEnum):
    FIXED_COHORT_DESCRIPTION = "fixed_cohort_description"
    POINT_IN_TIME_MEMBERSHIP = "point_in_time_membership"
    SURVIVORSHIP_SAFE_REPLAY = "survivorship_safe_replay"


class UniverseRef(FrozenModel):
    universe_id: _StableReference
    universe_version: _StableReference
    content_sha256: Sha256


class UniverseManifest(FrozenModel):
    ref: UniverseRef
    definition_kind: UniverseDefinitionKind
    supported_claims: tuple[UniverseClaimKind, ...]
    membership_ids: tuple[str, ...] = ()
    resolver_version: _StableReference | None = None
    effective_at: AwareDatetime
    owner: str = Field(min_length=1)

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("owner must be non-empty and cannot have surrounding whitespace")
        return value

    @field_validator("membership_ids")
    @classmethod
    def validate_membership_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("membership_ids must be unique")
        for value in values:
            _require_stable_reference(value, "membership_id")
        return tuple(sorted(values))

    @classmethod
    def compute_content_sha256(
        cls,
        *,
        universe_id: str,
        universe_version: str,
        definition_kind: UniverseDefinitionKind,
        supported_claims: tuple[UniverseClaimKind, ...],
        membership_ids: tuple[str, ...] = (),
        resolver_version: str | None = None,
        effective_at: datetime,
        owner: str,
    ) -> str:
        require_aware(effective_at, "effective_at")
        return canonical_sha256(
            {
                "schema": "truealpha.universe-manifest.v1",
                "universe_id": universe_id,
                "universe_version": universe_version,
                "definition_kind": UniverseDefinitionKind(definition_kind).value,
                "supported_claims": sorted(UniverseClaimKind(item).value for item in supported_claims),
                "membership_ids": sorted(membership_ids),
                "resolver_version": resolver_version,
                "effective_at": effective_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "owner": owner,
            }
        )

    @classmethod
    def create(
        cls,
        *,
        universe_id: str,
        universe_version: str,
        definition_kind: UniverseDefinitionKind,
        effective_at: datetime,
        owner: str,
        membership_ids: tuple[str, ...] = (),
        resolver_version: str | None = None,
        supported_claims: tuple[UniverseClaimKind, ...] | None = None,
    ) -> UniverseManifest:
        claims = supported_claims or (
            (UniverseClaimKind.FIXED_COHORT_DESCRIPTION,)
            if definition_kind is UniverseDefinitionKind.FIXED_COHORT
            else (UniverseClaimKind.POINT_IN_TIME_MEMBERSHIP, UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY)
        )
        digest = cls.compute_content_sha256(
            universe_id=universe_id,
            universe_version=universe_version,
            definition_kind=definition_kind,
            supported_claims=claims,
            membership_ids=membership_ids,
            resolver_version=resolver_version,
            effective_at=effective_at,
            owner=owner,
        )
        return cls(
            ref=UniverseRef(universe_id=universe_id, universe_version=universe_version, content_sha256=digest),
            definition_kind=definition_kind,
            supported_claims=claims,
            membership_ids=membership_ids,
            resolver_version=resolver_version,
            effective_at=effective_at,
            owner=owner,
        )

    @model_validator(mode="after")
    def validate_definition_and_hash(self) -> UniverseManifest:
        claims = tuple(sorted(self.supported_claims, key=lambda item: item.value))
        if len(claims) != len(set(claims)):
            raise ValueError("supported_claims must be unique")
        object.__setattr__(self, "supported_claims", claims)
        if self.definition_kind is UniverseDefinitionKind.FIXED_COHORT:
            if not self.membership_ids:
                raise ValueError("fixed cohorts require immutable membership_ids")
            if self.resolver_version is not None:
                raise ValueError("fixed cohorts cannot declare a PIT resolver")
            if set(claims) != {UniverseClaimKind.FIXED_COHORT_DESCRIPTION}:
                raise ValueError("fixed cohorts can support only fixed-cohort descriptive claims")
        else:
            if self.membership_ids:
                raise ValueError("PIT universes cannot freeze membership_ids")
            if self.resolver_version is None:
                raise ValueError("PIT universes require a resolver_version")
            if set(claims) != {UniverseClaimKind.POINT_IN_TIME_MEMBERSHIP, UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY}:
                raise ValueError("PIT universes must explicitly support PIT and survivorship-safe claims")
        expected = self.compute_content_sha256(
            universe_id=self.ref.universe_id,
            universe_version=self.ref.universe_version,
            definition_kind=self.definition_kind,
            supported_claims=self.supported_claims,
            membership_ids=self.membership_ids,
            resolver_version=self.resolver_version,
            effective_at=self.effective_at,
            owner=self.owner,
        )
        if self.ref.content_sha256 != expected:
            raise ValueError("UniverseRef content_sha256 does not match canonical manifest content")
        return self


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
