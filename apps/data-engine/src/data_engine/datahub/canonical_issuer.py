"""The wide row's issuer id for a universe member (#1079, docs/entity-identity.md §5).

The universe corpus names an issuer by a legacy id such as `issuer:lei:...`. The capture tick
resolves that id to an entity UUID, and the UUID is the `issuer_id` of `mart.topt_gppe_results`.
A mart row that the coverage report must join to the wide row stores that same UUID.

`canonicalize_universe` is the one resolver for those rows. It calls `lookup_entity`, which is
what `resolve_entity` calls first at capture time. It never mints.

The wide row stays the authority. A lookup repeats capture's question later, and evidence
recorded since can change its answer. An issuer whose answer is not a wide-row id gets no row.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from data_engine.datahub.resolve_coordinates import is_uuid, lookup_entity

__all__ = (
    "NOT_IN_WIDE_ROW",
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
#: Why an issuer got no row: its entity is not an `issuer_id` of the head's wide row.
NOT_IN_WIDE_ROW = "not_in_wide_row"


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
    """A universe member that gets no row: no entity, or an entity the wide row does not hold."""

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

    def unmapped_by_reason(self) -> dict[str, int]:
        return dict(sorted(Counter(issuer.reason for issuer in self.unmapped).items()))

    def total_failure(self) -> str | None:
        """The message when a non-empty universe maps to no wide-row issuer, else None.

        Every row would be missing and the coverage report would say `no_row` for all of them.
        """
        if self.unmapped and not self.issuers:
            reasons = ", ".join(f"{reason} {count}" for reason, count in self.unmapped_by_reason().items())
            return f"{len(self.unmapped)} issuers get no row ({reasons}); first: {self.unmapped[0].ticker}"
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


def _entity_of(
    connection: psycopg.Connection[Any], legacy_id: str, *, as_of: date, known_at: datetime
) -> uuid.UUID | None:
    """The entity `legacy_id` names, or None. A UUID-form id must also be an entity of the store."""
    entity = lookup_entity(connection, legacy_id, "issuer", as_of=as_of, known_at=known_at)
    if entity is not None and is_uuid(legacy_id):
        held = connection.execute("select 1 from staging.entities where entity_id = %s", (entity,)).fetchone()
        return entity if held else None
    return entity


def canonicalize_universe(
    connection: psycopg.Connection[Any],
    tickers: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    cutoff: datetime,
    as_of: date,
    wide_row_ids: Collection[str],
) -> CanonicalUniverse:
    """Map each legacy id of `tickers` to its wide-row issuer id, or to a reason it has none.

    `as_of` is the date capture resolved with, the corpus report date. A claim that ended or
    began between that date and the cutoff names another entity under a later date.
    `known_at` is the head's cutoff, as at capture, so evidence knowable later stays unseen.
    Two legacy ids of one entity give one issuer: the first one keeps its place.
    Raises when the store matches one id to two entities.
    """
    pairs = tickers.items() if isinstance(tickers, Mapping) else tickers
    issuers: dict[str, CanonicalIssuer] = {}
    unmapped: list[UnmappedIssuer] = []
    for legacy_id, ticker in pairs:
        entity = _entity_of(connection, legacy_id, as_of=as_of, known_at=cutoff)
        if entity is None:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker))
        elif str(entity) not in wide_row_ids:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker, reason=NOT_IN_WIDE_ROW))
        else:
            issuers.setdefault(str(entity), CanonicalIssuer(issuer_id=str(entity), legacy_id=legacy_id, ticker=ticker))
    return CanonicalUniverse(issuers=tuple(issuers.values()), unmapped=tuple(unmapped))
