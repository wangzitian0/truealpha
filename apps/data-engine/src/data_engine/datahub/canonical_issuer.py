"""The wide row's issuer id for a universe member (#1079, docs/entity-identity.md §5).

The universe corpus names an issuer by a legacy id such as `issuer:lei:...`. The capture tick
resolves that id to an entity UUID, and the UUID is the `issuer_id` of `mart.topt_gppe_results`.
A mart row that the coverage report must join to the wide row stores that same UUID.

`canonicalize_universe` is the one resolver for those rows. It calls `lookup_entity`, which is
what `resolve_entity` calls first at capture time, so both sides read one function. It never
mints: an issuer the store does not know is reported, not invented.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from data_engine.datahub.resolve_coordinates import lookup_entity

__all__ = (
    "NO_CANONICAL_ISSUER_ID",
    "CanonicalIssuer",
    "CanonicalUniverse",
    "UnmappedIssuer",
    "canonicalize_universe",
    "is_canonical_issuer_id",
    "require_canonical_issuer_id",
)

#: Why an issuer got no row: the store holds no entity for its legacy id.
NO_CANONICAL_ISSUER_ID = "no_canonical_issuer_id"


@dataclass(frozen=True)
class CanonicalIssuer:
    """One universe member. `issuer_id` is the wide row's id and the only id a mart row stores.

    `legacy_id` is the id the corpus and the knowledge graph use. A read of the graph takes it.
    """

    issuer_id: str
    legacy_id: str
    ticker: str


@dataclass(frozen=True)
class UnmappedIssuer:
    """A universe member with no entity. No mart row can carry its id, so it gets none."""

    legacy_id: str
    ticker: str
    reason: str = NO_CANONICAL_ISSUER_ID


@dataclass(frozen=True)
class CanonicalUniverse:
    issuers: tuple[CanonicalIssuer, ...]
    unmapped: tuple[UnmappedIssuer, ...] = ()

    def tickers(self) -> dict[str, str]:
        """Canonical issuer id to ticker, for a writer that never reads the graph."""
        return {issuer.issuer_id: issuer.ticker for issuer in self.issuers}

    def total_failure(self) -> str | None:
        """The message when a non-empty universe mapped to nothing, else None.

        Every row would be missing and the coverage report would say `no_row` for all of them.
        """
        if self.unmapped and not self.issuers:
            first = self.unmapped[0]
            return f"{len(self.unmapped)} issuers have no entity ({first.reason}), so no row was written; first: {first.ticker}"
        return None


def is_canonical_issuer_id(value: str) -> bool:
    """True for the lower-case hyphenated UUID text that the wide row stores."""
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def require_canonical_issuer_id(value: str) -> str:
    """`value` unchanged, or ValueError. The last check before a mart row is written."""
    if not is_canonical_issuer_id(value):
        raise ValueError(
            f"issuer id {value!r} is not the wide row's id (a lower-case UUID); resolve it with canonicalize_universe"
        )
    return value


def canonicalize_universe(
    connection: psycopg.Connection[Any],
    tickers: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    cutoff: datetime,
) -> CanonicalUniverse:
    """Map each legacy id of `tickers` to the wide row's issuer id, as known at `cutoff`.

    Two legacy ids of one entity give one issuer: the first one keeps its place.
    Raises when the store matches one id to two entities.
    """
    pairs = tickers.items() if isinstance(tickers, Mapping) else tickers
    as_of = cutoff.astimezone(UTC).date()
    issuers: dict[str, CanonicalIssuer] = {}
    unmapped: list[UnmappedIssuer] = []
    for legacy_id, ticker in pairs:
        entity = lookup_entity(connection, legacy_id, "issuer", as_of=as_of, known_at=cutoff)
        if entity is None:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker))
            continue
        issuers.setdefault(str(entity), CanonicalIssuer(issuer_id=str(entity), legacy_id=legacy_id, ticker=ticker))
    return CanonicalUniverse(issuers=tuple(issuers.values()), unmapped=tuple(unmapped))
