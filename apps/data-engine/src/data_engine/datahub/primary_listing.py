"""The one answer to "which listing of an issuer is its primary listing" (#1518, step 2).

The owner of the fact is the relation `primary_listing` (issuer to listing) in
`staging.entity_relations`. `resolve_coordinates` writes it from the packaged classification
file. `choose_primary_listings` is the one function that reads it. Every engine calls it: the
core snapshot, the strategy seed, the history projector, the QQQ price lane, the standards
planner and the issuer labels. No caller ranks listings by ticker or by id.

An issuer with one listing uses that listing. An issuer with several listings uses the listing
that a row names. An issuer with several listings and no row has no primary listing. Its caller
marks it unavailable with the reason `PRIMARY_LISTING_UNDECLARED`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from data_engine.datahub.resolve_coordinates import alias_of, is_uuid, lookup_entity

__all__ = ("PRIMARY_LISTING_UNDECLARED", "choose_primary_listings")

#: The reason of an unavailable issuer: it has several listings, and no `primary_listing` row names one.
PRIMARY_LISTING_UNDECLARED = "primary_listing_undeclared"

# The `primary_listing` claims known at one time, newest first. A retracted claim is not a claim.
# The survivor of each end follows a merge. Few rows exist: one for each declared multi-listing issuer.
_DECLARED_SQL = """
select staging.entity_survivor(claim.from_entity_id, %(known_at)s)::text,
       staging.entity_survivor(claim.to_entity_id, %(known_at)s)::text
from (
    select relation.from_entity_id, relation.to_entity_id
    from staging.entity_relations relation
    where relation.relation_type = 'primary_listing'
      and relation.transaction_time <= %(known_at)s
      and not exists (
          select 1 from staging.entity_retractions retraction where retraction.relation_id = relation.relation_id)
    order by relation.transaction_time desc, relation.relation_id desc
) claim
"""


def choose_primary_listings(
    connection: psycopg.Connection[Any],
    candidates: Mapping[str, Sequence[str]],
    *,
    known_at: datetime,
) -> dict[str, str | None]:
    """For each issuer, the one candidate listing that the engines use, or None when undeclared.

    `candidates` maps an issuer id to the listings it has in the caller's data. An id may be an entity
    UUID or a legacy id. A listing may be an entity UUID, a legacy id (`listing:xnas:googl`) or a ticker.
    The answer is the candidate as the caller spelled it. One candidate is its own answer. Several
    candidates give the one that a `primary_listing` row, known at `known_at`, names for the issuer.
    """
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise ValueError("known_at must carry a time zone")
    chosen: dict[str, str | None] = {}
    contested: dict[str, list[str]] = {}
    for issuer, listings in candidates.items():
        distinct = list(dict.fromkeys(listings))
        if len(distinct) > 1:
            contested[issuer] = distinct
        else:
            chosen[issuer] = distinct[0] if distinct else None
    if not contested:
        return chosen
    declared: dict[str, str] = {}
    for issuer_entity, listing_entity in connection.execute(_DECLARED_SQL, {"known_at": known_at}).fetchall():
        declared.setdefault(issuer_entity, listing_entity)
    as_of = known_at.astimezone(UTC).date()
    for issuer, listings in contested.items():
        entities = {listing: _listing_entity(connection, listing, as_of, known_at) for listing in listings}
        # Two spellings of one listing (a legacy id and its entity UUID) are one listing, not a choice.
        if len({entity or listing.strip().lower() for listing, entity in entities.items()}) == 1:
            chosen[issuer] = listings[0]
            continue
        issuer_entity = lookup_entity(connection, issuer, "issuer", as_of=as_of, known_at=known_at)
        declared_entity = None if issuer_entity is None else declared.get(str(issuer_entity))
        names = set() if declared_entity is None else _listing_names(connection, declared_entity, known_at)
        chosen[issuer] = next(
            (
                listing
                for listing in listings
                if (declared_entity is not None and entities[listing] == declared_entity)
                or listing.strip().lower() in names
            ),
            None,
        )
    return chosen


def _listing_entity(connection: psycopg.Connection[Any], listing: str, as_of: date, known_at: datetime) -> str | None:
    """The entity that a listing id or entity UUID names, or None for a ticker or an unknown id."""
    if not (is_uuid(listing) or listing.startswith("listing:")):
        return None
    entity = lookup_entity(connection, listing, "listing", as_of=as_of, known_at=known_at)
    return None if entity is None else str(entity)


def _listing_names(connection: psycopg.Connection[Any], listing_entity: str, known_at: datetime) -> set[str]:
    """Every name of one listing, in lower case: its entity id, legacy id, `mic:ticker` and ticker."""
    valid_at = known_at.astimezone(UTC).date()
    names = {str(uuid.UUID(listing_entity))}
    legacy = alias_of(connection, listing_entity, "legacy-id", valid_at=valid_at, known_at=known_at)
    mic_ticker = alias_of(connection, listing_entity, "mic-ticker", valid_at=valid_at, known_at=known_at)
    if legacy:
        names.add(legacy)
    if mic_ticker and ":" in mic_ticker:
        mic, ticker = mic_ticker.split(":", 1)
        names.update({mic_ticker, ticker, f"listing:{mic}:{ticker}"})
    return {name.lower() for name in names}
