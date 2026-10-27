"""#877 PR-3: entity coordinates resolution for capture pipelines (docs/entity-identity.md §5).

Resolves legacy string coordinates (issuer, instrument, listing, ticker) to canonical
entity UUIDs from staging.entities / staging.entity_aliases.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from functools import cache
from importlib import resources
from typing import Any

import psycopg
from truealpha_contracts.common import canonical_sha256

_UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
ONTOLOGY_ROOT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/wangzitian0/truealpha/blob/main/docs/entity-identity.md"
)


def ontology_kind_namespace(kind: str) -> uuid.UUID:
    """Return the deterministic UUIDv5 namespace for an ontology entity kind (`issuer`, `instrument`, `listing`, `fund`)."""
    return uuid.uuid5(ONTOLOGY_ROOT_NAMESPACE, f"kind:{kind}")


def lookup_minted_entity(
    connection: psycopg.Connection[Any],
    scheme: str,
    value: str,
    *,
    known_at: datetime,
) -> uuid.UUID | None:
    """Return the surviving entity UUID if `(scheme, value)` was already minted in `staging.entities` (generation 1)."""
    existing_row = connection.execute(
        """
        select entity_id from staging.entities
        where birth_scheme = %s and birth_value = staging.entity_alias_normalize(%s, %s) and birth_generation = 1
        """,
        (scheme, scheme, value),
    ).fetchone()
    if existing_row and existing_row[0]:
        survivor = connection.execute(
            "select staging.entity_survivor(%s, %s)",
            (existing_row[0], known_at),
        ).fetchone()
        return survivor[0] if survivor and survivor[0] else existing_row[0]
    return None


def is_uuid(value: str) -> bool:
    """Return True if value is a valid formatted UUID string."""
    return bool(_UUID_REGEX.match(value))


#: The classification file of primary listings (data/primary_listings.v1.json).
PRIMARY_LISTINGS_FILENAME = "primary_listings.v1.json"


@cache
def primary_listing_ids() -> frozenset[str]:
    """The listing ids that the packaged classification declares as primary. The file is hash-pinned.

    The file holds a classification, not a measurement. A listing id that is not in it is not primary.
    The load refuses a file whose listings do not reproduce its own hash.
    """
    raw = json.loads(resources.files("data_engine.datahub.data").joinpath(PRIMARY_LISTINGS_FILENAME).read_bytes())
    listings = sorted(str(item) for item in raw["listings"])
    if canonical_sha256(listings) != raw["listings_sha256"]:
        raise ValueError(f"{PRIMARY_LISTINGS_FILENAME}: the listings do not match listings_sha256")
    return frozenset(listings)


_LEI_RE = re.compile(r"^issuer:lei:([0-9A-Za-z]{18}[0-9]{2})$")
_CIK_RE = re.compile(r"^issuer:cik:([0-9]{1,10})$")
_CUSIP_RE = re.compile(r"^security:cusip:([0-9A-Za-z*@#]{8}[0-9])$")
_FIGI_RE = re.compile(r"^security:figi:([B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9])$", re.IGNORECASE)
_MIC_TICKER_RE = re.compile(r"^listing:([0-9a-z]{4}):([0-9a-z][0-9a-z.\-]*)$", re.IGNORECASE)


def parse_alias(raw_id: str, role: str) -> tuple[str, str]:
    """Parse raw identifier string into (scheme, canonical_value)."""
    raw = raw_id.strip()
    if role == "issuer":
        m = _LEI_RE.match(raw)
        if m:
            return "lei", m.group(1).upper()
        m = _CIK_RE.match(raw)
        if m:
            return "cik", m.group(1).zfill(10)
        return "legacy-id", raw
    if role == "instrument":
        m = _CUSIP_RE.match(raw)
        if m:
            return "cusip", m.group(1).upper()
        m = _FIGI_RE.match(raw)
        if m:
            return "figi", m.group(1).upper()
        return "legacy-id", raw
    if role == "listing":
        m = _MIC_TICKER_RE.match(raw)
        if m:
            return "mic-ticker", f"{m.group(1).upper()}:{m.group(2).upper()}"
        return "legacy-id", raw
    return "legacy-id", raw


def lookup_entity(
    connection: psycopg.Connection[Any],
    raw_id: str,
    role: str,
    *,
    as_of: date,
    known_at: datetime,
) -> uuid.UUID | None:
    """The entity `raw_id` names, or None when the store holds none. Never writes.

    `resolve_entity` mints on None. A reader that must not mint calls this instead (#1079).
    """
    raw = raw_id.strip()
    if is_uuid(raw):
        parsed = uuid.UUID(raw)
        survivor = connection.execute("select staging.entity_survivor(%s, %s)", (parsed, known_at)).fetchone()
        return survivor[0] if survivor and survivor[0] else parsed

    scheme, value = parse_alias(raw, role)

    # 1. Try resolving by typed scheme/value
    resolved = connection.execute(
        "select staging.entity_resolve(%s, %s, %s, %s)",
        (scheme, value, as_of, known_at),
    ).fetchone()
    if resolved and resolved[0]:
        return resolved[0]

    # 2. If typed resolution missed and scheme != legacy-id, try legacy-id
    if scheme != "legacy-id":
        resolved_legacy = connection.execute(
            "select staging.entity_resolve('legacy-id', %s, %s, %s)",
            (raw, as_of, known_at),
        ).fetchone()
        if resolved_legacy and resolved_legacy[0]:
            return resolved_legacy[0]

    # 2b. Check if entity was already minted in immutable registry staging.entities
    found_typed = lookup_minted_entity(connection, scheme, value, known_at=known_at)
    if found_typed is not None:
        return found_typed

    if scheme != "legacy-id":
        found_legacy = lookup_minted_entity(connection, "legacy-id", raw, known_at=known_at)
        if found_legacy is not None:
            return found_legacy

    return None


def resolve_entity(
    connection: psycopg.Connection[Any],
    raw_id: str,
    role: str,
    *,
    as_of: date,
    known_at: datetime,
) -> uuid.UUID:
    """Resolve an entity string to its canonical UUIDv5 identity."""
    found = lookup_entity(connection, raw_id, role, as_of=as_of, known_at=known_at)
    if found is not None:
        return found

    raw = raw_id.strip()
    scheme, value = parse_alias(raw, role)

    # 3. Mint entity on miss
    kind = role  # 'issuer', 'instrument', 'listing'
    minted_row = connection.execute(
        "select staging.entity_mint(%s, %s, %s, 'datahub.capture')",
        (kind, scheme, value),
    ).fetchone()
    if not minted_row or not minted_row[0]:
        raise RuntimeError(f"failed to mint entity for {role} {scheme}:{value}")
    minted_id: uuid.UUID = minted_row[0]

    # Insert birth alias to satisfy require_birth_alias trigger
    connection.execute(
        """
        insert into staging.entity_aliases (
            entity_id, scheme, value, valid_from, valid_to, transaction_time, source,
            raw_ref, method, confidence, mapping_version
        ) values (%s, %s, %s, '-infinity'::date, null, least(%s, clock_timestamp()), 'datahub.capture', %s, 'asserted', 1.0, 'datahub:v1')
        on conflict do nothing
        """,
        (minted_id, scheme, value, known_at, f"mint:{minted_id}"),
    )

    # If scheme was not legacy-id, also record legacy-id alias
    if scheme != "legacy-id":
        connection.execute(
            """
            insert into staging.entity_aliases (
                entity_id, scheme, value, valid_from, valid_to, transaction_time, source,
                raw_ref, method, confidence, mapping_version
            ) values (%s, 'legacy-id', %s, '-infinity'::date, null, least(%s, clock_timestamp()), 'datahub.capture', %s, 'asserted', 1.0, 'datahub:v1')
            on conflict do nothing
            """,
            (minted_id, raw, known_at, f"mint:{minted_id}"),
        )

    return minted_id


def _assert_relation(
    connection: psycopg.Connection[Any],
    relation_type: str,
    from_entity: uuid.UUID,
    to_entity: uuid.UUID,
    *,
    known_at: datetime,
) -> None:
    """Write one `datahub.capture` relation, unless the store already holds that claim.

    The transaction time is the cutoff, or the clock when the cutoff lies ahead of it.
    """
    existing = connection.execute(
        """
        select 1 from staging.entity_relations
        where relation_type = %s and from_entity_id = %s and to_entity_id = %s and source = 'datahub.capture'
        """,
        (relation_type, from_entity, to_entity),
    ).fetchone()
    if existing:
        return
    connection.execute(
        """
        with t as (
            select least(%(known_at)s::timestamptz, clock_timestamp()) as tx_time
        ),
        r as (
            select staging.entity_relation_uuid(%(type)s, %(from)s, %(to)s, '-infinity'::date, t.tx_time, 'datahub.capture', 'asserted') as rel_id,
                   t.tx_time
            from t
        )
        insert into staging.entity_relations (
            relation_id, relation_type, from_entity_id, to_entity_id, valid_from,
            transaction_time, source, raw_ref, method, confidence, mapping_version
        )
        select
            r.rel_id, %(type)s, %(from)s, %(to)s, '-infinity'::date, r.tx_time, 'datahub.capture', 'rel:' || r.rel_id, 'asserted', 1.0, 'datahub:v1'
        from r
        on conflict do nothing
        """,
        {"known_at": known_at, "type": relation_type, "from": from_entity, "to": to_entity},
    )


def resolve_coordinates(
    connection: psycopg.Connection[Any] | None,
    instruments: Sequence[Sequence[str]],
    *,
    as_of: date,
    known_at: datetime | None = None,
) -> dict[str, tuple[str, str, str, str]]:
    """Resolve universe instruments to dictionary mapping listing_id -> (issuer_uuid, instrument_uuid, listing_uuid, ticker).

    If connection is None, returns raw string coordinates.
    """
    if known_at is None:
        known_at = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)

    result: dict[str, tuple[str, str, str, str]] = {}
    for row in instruments:
        issuer_raw = str(row[0])
        instrument_raw = str(row[1])
        listing_raw = str(row[2])
        ticker = str(row[3])
        subject_id = listing_raw

        if connection is None:
            result[subject_id] = (issuer_raw, instrument_raw, listing_raw, ticker)
            continue

        issuer_uuid = resolve_entity(connection, issuer_raw, "issuer", as_of=as_of, known_at=known_at)
        instrument_uuid = resolve_entity(connection, instrument_raw, "instrument", as_of=as_of, known_at=known_at)
        listing_uuid = resolve_entity(connection, listing_raw, "listing", as_of=as_of, known_at=known_at)

        _assert_relation(connection, "issues", issuer_uuid, instrument_uuid, known_at=known_at)
        _assert_relation(connection, "listed_as", instrument_uuid, listing_uuid, known_at=known_at)
        # The classification file names the primary listing. This resolver writes it, and no one else does.
        if listing_raw in primary_listing_ids():
            _assert_relation(connection, "primary_listing", issuer_uuid, listing_uuid, known_at=known_at)

        result[subject_id] = (str(issuer_uuid), str(instrument_uuid), str(listing_uuid), ticker)

    return result


def alias_of(
    connection: psycopg.Connection[Any],
    entity_id: uuid.UUID | str,
    scheme: str,
    *,
    valid_at: date,
    known_at: datetime,
) -> str | None:
    """Retrieve the authoritative alias for an entity at a given validity and known_at time."""
    entity_uuid = uuid.UUID(str(entity_id))
    row = connection.execute(
        """
        select alias.value
        from staging.entity_aliases alias
        where staging.entity_survivor(alias.entity_id, %s) = staging.entity_survivor(%s, %s)
          and alias.scheme = %s
          and alias.valid_from <= %s
          and coalesce(%s < staging.entity_alias_valid_to(alias.alias_id, %s), true)
          and alias.transaction_time <= %s
        order by alias.confidence desc, alias.transaction_time desc
        limit 1
        """,
        (known_at, entity_uuid, known_at, scheme, valid_at, valid_at, known_at, known_at),
    ).fetchone()
    return row[0] if row else None
