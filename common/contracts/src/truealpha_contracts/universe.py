"""Immutable universe and point-in-time membership contracts.

Universe references identify exact manifest content. Membership rows preserve both
valid time and the transaction-time evidence needed for replay.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import STABLE_ID_PATTERN, canonical_sha256
from truealpha_contracts.models import _require_aware

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MUTABLE_REFERENCE_MARKERS = frozenset({"current", "head", "latest"})


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_stable_reference(value: str, field_name: str) -> str:
    if value != value.strip() or not re.fullmatch(STABLE_ID_PATTERN, value):
        raise ValueError(f"{field_name} must be a non-empty stable identifier")
    tokens = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
    mutable_markers = tokens & _MUTABLE_REFERENCE_MARKERS
    if mutable_markers:
        raise ValueError(f"{field_name} cannot use a mutable reference marker: {sorted(mutable_markers)}")
    return value


class SubjectKind(StrEnum):
    ISSUER = "issuer"
    SECURITY = "security"
    LISTING = "listing"
    FUND = "fund"
    ANALYST = "analyst"
    UNIVERSE = "universe"
    THEME = "theme"


class SubjectRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SubjectKind
    id: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_stable_reference(value, "id")


class UniverseDefinitionKind(StrEnum):
    FIXED_COHORT = "fixed_cohort"
    PIT_MEMBERSHIP = "pit_membership"


class UniverseClaimKind(StrEnum):
    FIXED_COHORT_DESCRIPTION = "fixed_cohort_description"
    POINT_IN_TIME_MEMBERSHIP = "point_in_time_membership"
    SURVIVORSHIP_SAFE_REPLAY = "survivorship_safe_replay"


class UniverseRef(BaseModel):
    """Exact immutable reference to one universe manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    universe_id: str
    universe_version: str
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("universe_id", "universe_version")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_reference(value, info.field_name)


class UniverseManifest(BaseModel):
    """Content-hashed definition of a fixed cohort or a PIT resolver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: UniverseRef
    definition_kind: UniverseDefinitionKind
    supported_claims: tuple[UniverseClaimKind, ...]
    membership_ids: tuple[str, ...] = ()
    resolver_version: str | None = None
    effective_at: datetime
    owner: str = Field(min_length=1)

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")

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

    @field_validator("resolver_version")
    @classmethod
    def validate_resolver_version(cls, value: str | None) -> str | None:
        return None if value is None else _require_stable_reference(value, "resolver_version")

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
        """Hash the complete semantic manifest payload in canonical order."""

        _require_aware(effective_at, "effective_at")
        payload = {
            "schema": "truealpha.universe-manifest.v1",
            "universe_id": universe_id,
            "universe_version": universe_version,
            "definition_kind": UniverseDefinitionKind(definition_kind).value,
            "supported_claims": sorted(UniverseClaimKind(item).value for item in supported_claims),
            "membership_ids": sorted(membership_ids),
            "resolver_version": resolver_version,
            "effective_at": _canonical_datetime(effective_at),
            "owner": owner,
        }
        return canonical_sha256(payload)

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
        """Create a manifest and its exact content-addressed reference."""

        if supported_claims is None:
            supported_claims = (
                (UniverseClaimKind.FIXED_COHORT_DESCRIPTION,)
                if definition_kind is UniverseDefinitionKind.FIXED_COHORT
                else (
                    UniverseClaimKind.POINT_IN_TIME_MEMBERSHIP,
                    UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY,
                )
            )
        content_sha256 = cls.compute_content_sha256(
            universe_id=universe_id,
            universe_version=universe_version,
            definition_kind=definition_kind,
            supported_claims=supported_claims,
            membership_ids=membership_ids,
            resolver_version=resolver_version,
            effective_at=effective_at,
            owner=owner,
        )
        return cls(
            ref=UniverseRef(
                universe_id=universe_id,
                universe_version=universe_version,
                content_sha256=content_sha256,
            ),
            definition_kind=definition_kind,
            supported_claims=supported_claims,
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
            required_claims = {
                UniverseClaimKind.POINT_IN_TIME_MEMBERSHIP,
                UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY,
            }
            if set(claims) != required_claims:
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


class UniverseMembership(BaseModel):
    """Append-only membership evidence selected by a fixed cohort or PIT resolver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    membership_id: str
    universe_id: str
    subject: SubjectRef
    valid_from: date
    valid_to: date | None = None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator("membership_id", "universe_id")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_reference(value, info.field_name)

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_membership(self) -> UniverseMembership:
        if self.subject.kind is SubjectKind.UNIVERSE:
            raise ValueError("a universe cannot contain another universe implicitly")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self
