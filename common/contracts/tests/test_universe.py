from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.universe import (
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


def test_pit_contracts_reject_invalid_time_intervals() -> None:
    with pytest.raises(ValidationError, match="valid_to must not precede valid_from"):
        UniverseMembership(
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
