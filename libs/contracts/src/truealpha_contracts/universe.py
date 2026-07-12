"""Immutable universe and point-in-time identity-link contracts.

Universe references identify exact manifest content. Identity links keep issuer,
security, and listing identities distinct while preserving both valid time and
the transaction-time evidence needed for replay.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import _require_aware

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
_MUTABLE_REFERENCE_MARKERS = frozenset({"current", "head", "latest"})


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_stable_reference(value: str, field_name: str) -> str:
    if value != value.strip() or not _STABLE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a non-empty stable identifier")
    tokens = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
    mutable_markers = tokens & _MUTABLE_REFERENCE_MARKERS
    if mutable_markers:
        raise ValueError(f"{field_name} cannot use a mutable reference marker: {sorted(mutable_markers)}")
    return value


def _intervals_overlap(
    left_from: date,
    left_to: date | None,
    right_from: date,
    right_to: date | None,
) -> bool:
    return (left_to is None or right_from <= left_to) and (right_to is None or left_from <= right_to)


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


class SecurityKind(StrEnum):
    COMMON_STOCK = "common_stock"
    ADR = "adr"
    ETF = "etf"
    FUND = "fund"
    DEBT = "debt"
    OPTION = "option"
    FUTURE = "future"
    SWAP = "swap"
    DERIVATIVE_OTHER = "derivative_other"
    CASH = "cash"
    CASH_EQUIVALENT = "cash_equivalent"
    OTHER = "other"


class ListingRole(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class IssuerSecurityLink(BaseModel):
    """PIT evidence connecting one reporting issuer to one legal security."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    issuer_id: str
    security_id: str
    security_kind: SecurityKind
    share_class: str | None = None
    underlying_security_id: str | None = None
    underlying_shares_per_security_unit: Decimal = Field(gt=0)
    valid_from: date
    valid_to: date | None = None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator("input_id", "issuer_id", "security_id", "underlying_security_id")
    @classmethod
    def validate_stable_identity(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_stable_reference(value, info.field_name)

    @field_validator("share_class")
    @classmethod
    def validate_share_class(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("share_class must be non-empty and cannot have surrounding whitespace")
        return value

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_link(self) -> IssuerSecurityLink:
        if self.issuer_id == self.security_id:
            raise ValueError("issuer_id and security_id are distinct identity namespaces")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        if self.security_kind is SecurityKind.COMMON_STOCK:
            if self.share_class is None:
                raise ValueError("common-stock links require an explicit share_class")
            if self.underlying_security_id is not None:
                raise ValueError("common stock cannot declare an underlying security")
            if self.underlying_shares_per_security_unit != Decimal("1"):
                raise ValueError("common-stock security units must map one-to-one to their shares")
        if self.security_kind is SecurityKind.ADR and self.underlying_security_id is None:
            raise ValueError("ADR links require an underlying_security_id")
        if self.underlying_security_id == self.security_id:
            raise ValueError("a security cannot be its own underlying security")
        if self.underlying_security_id is None and self.underlying_shares_per_security_unit != Decimal("1"):
            raise ValueError("a non-unit conversion ratio requires an underlying security")
        return self


class SecurityListingLink(BaseModel):
    """PIT evidence connecting one security to an explicit market line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    security_id: str
    listing_id: str
    exchange_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    ticker: str = Field(min_length=1)
    listing_role: ListingRole
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    timezone: str = Field(min_length=1)
    trading_calendar_id: str
    trading_calendar_version: str
    valid_from: date
    valid_to: date | None = None
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator(
        "input_id",
        "security_id",
        "listing_id",
        "trading_calendar_id",
        "trading_calendar_version",
    )
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_reference(value, info.field_name)

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("ticker must be non-empty and cannot contain whitespace")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_link(self) -> SecurityListingLink:
        if self.security_id == self.listing_id:
            raise ValueError("security_id and listing_id are distinct identity namespaces")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class IdentityLinkSet(BaseModel):
    """Validated identity graph slice for one snapshot or capture partition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer_security_links: tuple[IssuerSecurityLink, ...] = Field(min_length=1)
    security_listing_links: tuple[SecurityListingLink, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> IdentityLinkSet:
        issuer_links = tuple(
            sorted(self.issuer_security_links, key=lambda link: (link.security_id, link.valid_from, link.input_id))
        )
        listing_links = tuple(
            sorted(self.security_listing_links, key=lambda link: (link.listing_id, link.valid_from, link.input_id))
        )
        all_input_ids = [link.input_id for link in issuer_links]
        all_input_ids.extend(link.input_id for link in listing_links)
        if len(all_input_ids) != len(set(all_input_ids)):
            raise ValueError("identity-link input_ids must be unique")

        known_securities = {link.security_id for link in issuer_links}
        unknown_securities = {link.security_id for link in listing_links} - known_securities
        if unknown_securities:
            raise ValueError(f"listing links reference unknown securities: {sorted(unknown_securities)}")

        for index, issuer_left in enumerate(issuer_links):
            for issuer_right in issuer_links[index + 1 :]:
                if not _intervals_overlap(
                    issuer_left.valid_from,
                    issuer_left.valid_to,
                    issuer_right.valid_from,
                    issuer_right.valid_to,
                ):
                    continue
                if (
                    issuer_left.security_id == issuer_right.security_id
                    and issuer_left.issuer_id != issuer_right.issuer_id
                ):
                    raise ValueError("one security cannot resolve to multiple issuers at the same valid time")
                if (
                    issuer_left.issuer_id == issuer_right.issuer_id
                    and issuer_left.share_class is not None
                    and issuer_left.share_class == issuer_right.share_class
                    and issuer_left.security_id != issuer_right.security_id
                ):
                    raise ValueError(
                        "one issuer share class cannot resolve to multiple securities at the same valid time"
                    )

        for index, listing_left in enumerate(listing_links):
            for listing_right in listing_links[index + 1 :]:
                if not _intervals_overlap(
                    listing_left.valid_from,
                    listing_left.valid_to,
                    listing_right.valid_from,
                    listing_right.valid_to,
                ):
                    continue
                if (
                    listing_left.listing_id == listing_right.listing_id
                    and listing_left.security_id != listing_right.security_id
                ):
                    raise ValueError("one listing cannot resolve to multiple securities at the same valid time")
                if (
                    listing_left.security_id == listing_right.security_id
                    and listing_left.listing_role is ListingRole.PRIMARY
                    and listing_right.listing_role is ListingRole.PRIMARY
                    and listing_left.listing_id != listing_right.listing_id
                ):
                    raise ValueError("one security cannot have multiple primary listings at the same valid time")

        object.__setattr__(self, "issuer_security_links", issuer_links)
        object.__setattr__(self, "security_listing_links", listing_links)
        return self
