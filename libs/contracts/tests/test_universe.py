from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.universe import (
    IdentityLinkSet,
    IssuerSecurityLink,
    ListingRole,
    SecurityKind,
    SecurityListingLink,
    SubjectKind,
    SubjectRef,
    UniverseClaimKind,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    UniverseRef,
)

EFFECTIVE_AT = datetime(2026, 5, 28, 0, 0, tzinfo=UTC)
KNOWABLE_AT = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
RECORDED_AT = datetime(2026, 5, 28, 12, 1, tzinfo=UTC)


def _issuer_security_link(
    *,
    input_id: str,
    security_id: str,
    share_class: str,
    security_kind: SecurityKind = SecurityKind.COMMON_STOCK,
    underlying_security_id: str | None = None,
    ratio: Decimal = Decimal("1"),
    valid_from: date = date(2014, 4, 3),
    valid_to: date | None = None,
) -> IssuerSecurityLink:
    return IssuerSecurityLink(
        input_id=input_id,
        issuer_id="issuer:alphabet",
        security_id=security_id,
        security_kind=security_kind,
        share_class=share_class,
        underlying_security_id=underlying_security_id,
        underlying_shares_per_security_unit=ratio,
        valid_from=valid_from,
        valid_to=valid_to,
        knowable_at=KNOWABLE_AT,
        recorded_at=RECORDED_AT,
        confidence=Decimal("1"),
        raw_ref="raw.fetches:identity",
    )


def _security_listing_link(
    *,
    input_id: str,
    security_id: str,
    listing_id: str,
    ticker: str,
    role: ListingRole = ListingRole.PRIMARY,
    valid_from: date = date(2014, 4, 3),
    valid_to: date | None = None,
) -> SecurityListingLink:
    return SecurityListingLink(
        input_id=input_id,
        security_id=security_id,
        listing_id=listing_id,
        exchange_mic="XNAS",
        ticker=ticker,
        listing_role=role,
        currency="USD",
        timezone="America/New_York",
        trading_calendar_id="calendar:us-equities",
        trading_calendar_version="v1",
        valid_from=valid_from,
        valid_to=valid_to,
        knowable_at=KNOWABLE_AT,
        recorded_at=RECORDED_AT,
        confidence=Decimal("1"),
        raw_ref="raw.fetches:identity",
    )


def test_fixed_universe_manifest_is_content_addressed_and_frozen() -> None:
    manifest = UniverseManifest.create(
        universe_id="universe:topt-etf-us",
        universe_version="2026-05-28",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        membership_ids=("membership:goog", "membership:aapl"),
        effective_at=EFFECTIVE_AT,
        owner="data-platform",
    )

    assert manifest.membership_ids == ("membership:aapl", "membership:goog")
    assert len(manifest.ref.content_sha256) == 64
    with pytest.raises(ValidationError, match="frozen"):
        manifest.owner = "another-owner"  # type: ignore[misc]


def test_universe_manifest_rejects_content_hash_mismatch() -> None:
    manifest = UniverseManifest.create(
        universe_id="universe:topt-etf-us",
        universe_version="2026-05-28",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        membership_ids=("membership:aapl",),
        effective_at=EFFECTIVE_AT,
        owner="data-platform",
    )

    with pytest.raises(ValidationError, match="does not match canonical manifest content"):
        UniverseManifest(
            ref=manifest.ref,
            definition_kind=manifest.definition_kind,
            supported_claims=manifest.supported_claims,
            membership_ids=("membership:aapl", "membership:goog"),
            effective_at=manifest.effective_at,
            owner=manifest.owner,
        )


@pytest.mark.parametrize(
    ("universe_id", "universe_version"),
    [
        ("universe:current", "v1"),
        ("universe:topt", "latest"),
        ("universe:topt", "release-head"),
    ],
)
def test_universe_ref_rejects_mutable_latest_like_references(universe_id: str, universe_version: str) -> None:
    with pytest.raises(ValidationError, match="mutable reference marker"):
        UniverseRef(
            universe_id=universe_id,
            universe_version=universe_version,
            content_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("kind", "membership_ids", "resolver_version", "message"),
    [
        (UniverseDefinitionKind.FIXED_COHORT, (), None, "require immutable membership_ids"),
        (UniverseDefinitionKind.FIXED_COHORT, ("membership:aapl",), "resolver:v1", "cannot declare a PIT resolver"),
        (UniverseDefinitionKind.PIT_MEMBERSHIP, ("membership:aapl",), "resolver:v1", "cannot freeze membership_ids"),
        (UniverseDefinitionKind.PIT_MEMBERSHIP, (), None, "require a resolver_version"),
    ],
)
def test_universe_manifest_requires_exactly_one_membership_mode(
    kind: UniverseDefinitionKind,
    membership_ids: tuple[str, ...],
    resolver_version: str | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        UniverseManifest.create(
            universe_id="universe:topt-etf-us",
            universe_version="2026-05-28",
            definition_kind=kind,
            membership_ids=membership_ids,
            resolver_version=resolver_version,
            effective_at=EFFECTIVE_AT,
            owner="data-platform",
        )


def test_pit_manifest_binds_resolver_version_without_frozen_members() -> None:
    manifest = UniverseManifest.create(
        universe_id="universe:sp500-survivorship-safe",
        universe_version="v1",
        definition_kind=UniverseDefinitionKind.PIT_MEMBERSHIP,
        resolver_version="membership-resolver:v3",
        effective_at=EFFECTIVE_AT,
        owner="research-platform",
    )

    assert manifest.membership_ids == ()
    assert manifest.resolver_version == "membership-resolver:v3"
    assert set(manifest.supported_claims) == {
        UniverseClaimKind.POINT_IN_TIME_MEMBERSHIP,
        UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY,
    }


def test_fixed_cohort_cannot_claim_survivorship_safe_replay() -> None:
    with pytest.raises(ValidationError, match="fixed-cohort descriptive claims"):
        UniverseManifest.create(
            universe_id="universe:topt-etf-us",
            universe_version="2026-05-28",
            definition_kind=UniverseDefinitionKind.FIXED_COHORT,
            supported_claims=(UniverseClaimKind.SURVIVORSHIP_SAFE_REPLAY,),
            membership_ids=("membership:aapl",),
            effective_at=EFFECTIVE_AT,
            owner="data-platform",
        )


def test_universe_manifest_rejects_naive_effective_time_and_duplicate_memberships() -> None:
    with pytest.raises(ValueError, match="effective_at must be timezone-aware"):
        UniverseManifest.create(
            universe_id="universe:topt-etf-us",
            universe_version="2026-05-28",
            definition_kind=UniverseDefinitionKind.FIXED_COHORT,
            membership_ids=("membership:aapl",),
            effective_at=datetime(2026, 5, 28),
            owner="data-platform",
        )

    with pytest.raises(ValidationError, match="membership_ids must be unique"):
        UniverseManifest.create(
            universe_id="universe:topt-etf-us",
            universe_version="2026-05-28",
            definition_kind=UniverseDefinitionKind.FIXED_COHORT,
            membership_ids=("membership:aapl", "membership:aapl"),
            effective_at=EFFECTIVE_AT,
            owner="data-platform",
        )


def test_membership_keeps_immutable_subject_and_pit_times() -> None:
    membership = UniverseMembership(
        membership_id="membership:alphabet",
        universe_id="universe:topt-etf-us",
        subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
        valid_from=date(2026, 5, 28),
        knowable_at=KNOWABLE_AT,
        recorded_at=RECORDED_AT,
        confidence=Decimal("0.99"),
        raw_ref="raw.fetches:nport",
    )

    with pytest.raises(ValidationError, match="frozen"):
        membership.membership_id = "membership:changed"  # type: ignore[misc]


def test_goog_and_googl_remain_distinct_securities_and_listings() -> None:
    goog = _issuer_security_link(
        input_id="identity:alphabet-class-c",
        security_id="security:alphabet-class-c",
        share_class="C",
    )
    googl = _issuer_security_link(
        input_id="identity:alphabet-class-a",
        security_id="security:alphabet-class-a",
        share_class="A",
    )
    goog_listing = _security_listing_link(
        input_id="identity:xnas-goog",
        security_id=goog.security_id,
        listing_id="listing:xnas-goog",
        ticker="GOOG",
    )
    googl_listing = _security_listing_link(
        input_id="identity:xnas-googl",
        security_id=googl.security_id,
        listing_id="listing:xnas-googl",
        ticker="GOOGL",
    )

    links = IdentityLinkSet(
        issuer_security_links=(goog, googl),
        security_listing_links=(goog_listing, googl_listing),
    )

    assert {link.security_id for link in links.issuer_security_links} == {
        "security:alphabet-class-a",
        "security:alphabet-class-c",
    }
    assert {link.listing_id for link in links.security_listing_links} == {
        "listing:xnas-goog",
        "listing:xnas-googl",
    }


def test_identity_graph_rejects_goog_googl_security_or_listing_collision() -> None:
    one_security = _issuer_security_link(
        input_id="identity:alphabet-class-c",
        security_id="security:alphabet-collapsed",
        share_class="C",
    )
    goog = _security_listing_link(
        input_id="identity:xnas-goog",
        security_id=one_security.security_id,
        listing_id="listing:xnas-goog",
        ticker="GOOG",
    )
    googl = _security_listing_link(
        input_id="identity:xnas-googl",
        security_id=one_security.security_id,
        listing_id="listing:xnas-googl",
        ticker="GOOGL",
    )

    with pytest.raises(ValidationError, match="multiple primary listings"):
        IdentityLinkSet(
            issuer_security_links=(one_security,),
            security_listing_links=(goog, googl),
        )


@pytest.mark.parametrize("ratio", [Decimal("0"), Decimal("-0.5")])
def test_adr_ratio_must_be_strictly_positive(ratio: Decimal) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        _issuer_security_link(
            input_id="identity:adr",
            security_id="security:adr",
            share_class="ADR",
            security_kind=SecurityKind.ADR,
            underlying_security_id="security:ordinary-share",
            ratio=ratio,
        )


def test_adr_requires_a_distinct_underlying_security() -> None:
    with pytest.raises(ValidationError, match="require an underlying_security_id"):
        _issuer_security_link(
            input_id="identity:adr",
            security_id="security:adr",
            share_class="ADR",
            security_kind=SecurityKind.ADR,
        )

    with pytest.raises(ValidationError, match="cannot be its own underlying"):
        _issuer_security_link(
            input_id="identity:adr",
            security_id="security:adr",
            share_class="ADR",
            security_kind=SecurityKind.ADR,
            underlying_security_id="security:adr",
        )


@pytest.mark.parametrize("kind", ["membership", "issuer_security", "security_listing"])
def test_pit_contracts_reject_invalid_time_intervals(kind: str) -> None:
    if kind == "membership":
        constructor = lambda: UniverseMembership(  # noqa: E731
            membership_id="membership:alphabet",
            universe_id="universe:topt-etf-us",
            subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
            valid_from=date(2026, 6, 1),
            valid_to=date(2026, 5, 31),
            knowable_at=KNOWABLE_AT,
            recorded_at=RECORDED_AT,
            confidence=Decimal("1"),
            raw_ref="raw.fetches:nport",
        )
    elif kind == "issuer_security":
        constructor = lambda: _issuer_security_link(  # noqa: E731
            input_id="identity:alphabet-class-c",
            security_id="security:alphabet-class-c",
            share_class="C",
            valid_from=date(2026, 6, 1),
            valid_to=date(2026, 5, 31),
        )
    else:
        constructor = lambda: _security_listing_link(  # noqa: E731
            input_id="identity:xnas-goog",
            security_id="security:alphabet-class-c",
            listing_id="listing:xnas-goog",
            ticker="GOOG",
            valid_from=date(2026, 6, 1),
            valid_to=date(2026, 5, 31),
        )

    with pytest.raises(ValidationError, match="valid_to must not precede valid_from"):
        constructor()


def test_listing_requires_explicit_valid_venue_currency_and_calendar() -> None:
    common = {
        "input_id": "identity:xnas-goog",
        "security_id": "security:alphabet-class-c",
        "listing_id": "listing:xnas-goog",
        "exchange_mic": "XNAS",
        "ticker": "GOOG",
        "listing_role": ListingRole.PRIMARY,
        "currency": "USD",
        "timezone": "America/New_York",
        "trading_calendar_id": "calendar:us-equities",
        "trading_calendar_version": "v1",
        "valid_from": date(2014, 4, 3),
        "knowable_at": KNOWABLE_AT,
        "recorded_at": RECORDED_AT,
        "confidence": Decimal("1"),
        "raw_ref": "raw.fetches:identity",
    }

    for override in (
        {"exchange_mic": "NASDAQ"},
        {"currency": "usd"},
        {"timezone": "New_York"},
        {"trading_calendar_version": "latest"},
    ):
        with pytest.raises(ValidationError):
            SecurityListingLink(**(common | override))
