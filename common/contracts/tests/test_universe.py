from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseMembership,
    UniverseRef,
)

KNOWABLE_AT = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
RECORDED_AT = datetime(2026, 5, 28, 12, 1, tzinfo=UTC)


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
