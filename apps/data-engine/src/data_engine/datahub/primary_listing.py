"""Primary listing resolver for multi-listing issuers (#1518)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from data_engine.datahub.resolve_coordinates import alias_of, is_uuid, lookup_entity

__all__ = ("PRIMARY_LISTING_UNDECLARED", "choose_primary_listings", "primary_symbols")

PRIMARY_LISTING_UNDECLARED = "primary_listing_undeclared"

_DECLARED_SQL = (
    "select staging.entity_survivor(claim.from_entity_id, %(known_at)s)::text, "
    "staging.entity_survivor(claim.to_entity_id, %(known_at)s)::text from ("
    "select relation.from_entity_id, relation.to_entity_id from staging.entity_relations relation "
    "where relation.relation_type = 'primary_listing' and relation.transaction_time <= %(known_at)s "
    "and not exists (select 1 from staging.entity_retractions retraction where retraction.relation_id = relation.relation_id) "
    "order by relation.transaction_time desc, relation.relation_id desc) claim"
)


def choose_primary_listings(
    connection: psycopg.Connection[Any], candidates: Mapping[str, Sequence[str]], *, known_at: datetime
) -> dict[str, str | None]:
    """Return primary candidate listing per issuer, or None."""
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


def primary_symbols(
    connection: psycopg.Connection[Any], denominator: Mapping[str, Any], *, known_at: datetime
) -> tuple[str, ...]:
    """Sorted tickers of primary listings in `denominator`."""
    fields = list(denominator["instrument_tuple_fields"])
    issuer_at, listing_at, ticker_at = (fields.index(name) for name in ("issuer_id", "listing_id", "ticker"))
    tickers: dict[str, str | None] = {}
    listings: dict[str, list[str]] = {}
    for row in denominator["instruments"]:
        tickers[str(row[listing_at])] = row[ticker_at] or None
        listings.setdefault(str(row[issuer_at]), []).append(str(row[listing_at]))
    chosen = choose_primary_listings(connection, listings, known_at=known_at)
    return tuple(
        sorted({ticker for listing in chosen.values() if listing is not None and (ticker := tickers[listing])})
    )


def _listing_entity(connection: psycopg.Connection[Any], listing: str, as_of: date, known_at: datetime) -> str | None:
    if not (is_uuid(listing) or listing.startswith("listing:")):
        return None
    entity = lookup_entity(connection, listing, "listing", as_of=as_of, known_at=known_at)
    return None if entity is None else str(entity)


def _listing_names(connection: psycopg.Connection[Any], listing_entity: str, known_at: datetime) -> set[str]:
    valid_at = known_at.astimezone(UTC).date()
    names = {str(uuid.UUID(listing_entity))}
    if legacy := alias_of(connection, listing_entity, "legacy-id", valid_at=valid_at, known_at=known_at):
        names.add(legacy)
    mic_ticker = alias_of(connection, listing_entity, "mic-ticker", valid_at=valid_at, known_at=known_at)
    if mic_ticker and ":" in mic_ticker:
        mic, ticker = mic_ticker.split(":", 1)
        names.update({mic_ticker, ticker, f"listing:{mic}:{ticker}"})
    return {name.lower() for name in names}
