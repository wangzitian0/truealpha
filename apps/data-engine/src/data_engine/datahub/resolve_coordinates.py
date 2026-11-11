"""Entity coordinates resolution for capture pipelines (docs/entity-identity.md §5)."""

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
PRIMARY_LISTINGS_FILENAME = "primary_listings.v1.json"

_LEI_RE = re.compile(r"^issuer:lei:([0-9A-Za-z]{18}[0-9]{2})$")
_CIK_RE = re.compile(r"^issuer:cik:([0-9]{1,10})$")
_CUSIP_RE = re.compile(r"^security:cusip:([0-9A-Za-z*@#]{8}[0-9])$")
_FIGI_RE = re.compile(r"^security:figi:([B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9])$", re.IGNORECASE)
_MIC_TICKER_RE = re.compile(r"^listing:([0-9a-z]{4}):([0-9a-z][0-9a-z.\-]*)$", re.IGNORECASE)


def ontology_kind_namespace(kind: str) -> uuid.UUID:
    return uuid.uuid5(ONTOLOGY_ROOT_NAMESPACE, f"kind:{kind}")


def is_uuid(value: str) -> bool:
    return bool(_UUID_REGEX.match(value))


def lookup_minted_entity(
    connection: psycopg.Connection[Any], scheme: str, value: str, *, known_at: datetime
) -> uuid.UUID | None:
    if existing_row := connection.execute(
        "select entity_id from staging.entities"
        " where birth_scheme = %s and birth_value = staging.entity_alias_normalize(%s, %s) and birth_generation = 1",
        (scheme, scheme, value),
    ).fetchone():
        survivor = connection.execute("select staging.entity_survivor(%s, %s)", (existing_row[0], known_at)).fetchone()
        return survivor[0] if survivor and survivor[0] else existing_row[0]
    return None


@cache
def _corpus_issuer_names() -> dict[str, str]:
    from data_engine.datahub.production_topt.universe_corpus import TOPT_CORPUS_FILENAME, load_corpus

    names = load_corpus(TOPT_CORPUS_FILENAME)["topt_denominator"].get("issuer_names", {})
    return {str(k).strip().upper(): str(v).strip() for k, v in names.items() if str(v).strip()}


def _resolve_issuer_display_name(
    connection: psycopg.Connection[Any], ticker: str, *, issuer_raw: str = "", issuer_uuid: uuid.UUID | None = None
) -> str | None:
    if not (normalized := ticker.strip().upper()):
        return None
    if row := connection.execute(
        "select nullif(btrim(company_name), '') from staging.etf_constituent_facts"
        " where upper(ticker) = %s and nullif(btrim(company_name), '') is not null"
        " and upper(btrim(company_name)) <> upper(btrim(ticker))"
        " order by knowable_at desc, as_of desc, etf_symbol asc limit 1",
        (normalized,),
    ).fetchone():
        return str(row[0])
    if (kg_ids := [item for item in (str(issuer_uuid) if issuer_uuid else "", issuer_raw.strip()) if item]) and (
        kg_row := connection.execute(
            "select nullif(btrim(display_name), '') from staging.kg_entities"
            " where id = any(%s) and nullif(btrim(display_name), '') is not null"
            " and upper(btrim(display_name)) <> %s and btrim(display_name) <> id limit 1",
            (kg_ids, normalized),
        ).fetchone()
    ):
        return str(kg_row[0])
    return _corpus_issuer_names().get(normalized)


def _insert_entity_alias(
    connection: psycopg.Connection[Any],
    entity_id: uuid.UUID,
    scheme: str,
    value: str,
    *,
    known_at: datetime,
    raw_ref: str,
) -> None:
    connection.execute(
        "insert into staging.entity_aliases (entity_id, scheme, value, valid_from, valid_to,"
        " transaction_time, source, raw_ref, method, confidence, mapping_version)"
        " values (%s, %s, %s, '-infinity'::date, null, least(%s, clock_timestamp()),"
        " 'datahub.capture', %s, 'asserted', 1.0, 'datahub:v1') on conflict do nothing",
        (entity_id, scheme, value, known_at, raw_ref),
    )


def _assert_issuer_name_alias(
    connection: psycopg.Connection[Any], issuer_uuid: uuid.UUID, display_name: str, *, known_at: datetime
) -> None:
    if not connection.execute(
        "select 1 from staging.entity_aliases where entity_id = %s and scheme = 'name' and value = %s",
        (issuer_uuid, display_name),
    ).fetchone():
        _insert_entity_alias(
            connection, issuer_uuid, "name", display_name, known_at=known_at, raw_ref=f"name:{issuer_uuid}"
        )


@cache
def primary_listing_ids() -> frozenset[str]:
    raw = json.loads(resources.files("data_engine.datahub.data").joinpath(PRIMARY_LISTINGS_FILENAME).read_bytes())
    listings = sorted(str(item) for item in raw["listings"])
    if canonical_sha256(listings) != raw["listings_sha256"]:
        raise ValueError(f"{PRIMARY_LISTINGS_FILENAME}: the listings do not match listings_sha256")
    return frozenset(listings)


def parse_alias(raw_id: str, role: str) -> tuple[str, str]:
    raw = raw_id.strip()
    if role == "issuer":
        if m := _LEI_RE.match(raw):
            return "lei", m.group(1).upper()
        if m := _CIK_RE.match(raw):
            return "cik", m.group(1).zfill(10)
    elif role == "instrument":
        if m := _CUSIP_RE.match(raw):
            return "cusip", m.group(1).upper()
        if m := _FIGI_RE.match(raw):
            return "figi", m.group(1).upper()
    elif role == "listing" and (m := _MIC_TICKER_RE.match(raw)):
        return "mic-ticker", f"{m.group(1).upper()}:{m.group(2).upper()}"
    return "legacy-id", raw


def lookup_entity(
    connection: psycopg.Connection[Any],
    raw_id: str,
    role: str,
    *,
    as_of: date,
    known_at: datetime,
) -> uuid.UUID | None:
    raw = raw_id.strip()
    if is_uuid(raw):
        parsed = uuid.UUID(raw)
        survivor = connection.execute("select staging.entity_survivor(%s, %s)", (parsed, known_at)).fetchone()
        return survivor[0] if survivor and survivor[0] else parsed

    scheme, value = parse_alias(raw, role)
    candidates = ((scheme, value), *((("legacy-id", raw),) if scheme != "legacy-id" else ()))
    for cand_scheme, cand_val in candidates:
        resolved = connection.execute(
            "select staging.entity_resolve(%s, %s, %s, %s)",
            (cand_scheme, cand_val, as_of, known_at),
        ).fetchone()
        if resolved and resolved[0]:
            return resolved[0]
    for cand_scheme, cand_val in candidates:
        found = lookup_minted_entity(connection, cand_scheme, cand_val, known_at=known_at)
        if found is not None:
            return found
    return None


def resolve_entity(
    connection: psycopg.Connection[Any],
    raw_id: str,
    role: str,
    *,
    as_of: date,
    known_at: datetime,
) -> uuid.UUID:
    found = lookup_entity(connection, raw_id, role, as_of=as_of, known_at=known_at)
    if found is not None:
        return found

    raw = raw_id.strip()
    scheme, value = parse_alias(raw, role)
    minted_row = connection.execute(
        "select staging.entity_mint(%s, %s, %s, 'datahub.capture')",
        (role, scheme, value),
    ).fetchone()
    if not minted_row or not minted_row[0]:
        raise RuntimeError(f"failed to mint entity for {role} {scheme}:{value}")
    minted_id: uuid.UUID = minted_row[0]
    raw_ref = f"mint:{minted_id}"
    _insert_entity_alias(connection, minted_id, scheme, value, known_at=known_at, raw_ref=raw_ref)
    if scheme != "legacy-id":
        _insert_entity_alias(connection, minted_id, "legacy-id", raw, known_at=known_at, raw_ref=raw_ref)
    return minted_id


def _assert_relation(
    connection: psycopg.Connection[Any],
    relation_type: str,
    from_entity: uuid.UUID,
    to_entity: uuid.UUID,
    *,
    known_at: datetime,
) -> None:
    if not connection.execute(
        "select 1 from staging.entity_relations where relation_type = %s and from_entity_id = %s and to_entity_id = %s and source = 'datahub.capture'",
        (relation_type, from_entity, to_entity),
    ).fetchone():
        connection.execute(
            "with t as (select least(%(known_at)s::timestamptz, clock_timestamp()) as tx_time),"
            " r as (select staging.entity_relation_uuid(%(type)s, %(from)s, %(to)s, '-infinity'::date, t.tx_time, 'datahub.capture', 'asserted') as rel_id, t.tx_time from t)"
            " insert into staging.entity_relations (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)"
            " select r.rel_id, %(type)s, %(from)s, %(to)s, '-infinity'::date, r.tx_time, 'datahub.capture', 'rel:' || r.rel_id, 'asserted', 1.0, 'datahub:v1' from r on conflict do nothing",
            {"known_at": known_at, "type": relation_type, "from": from_entity, "to": to_entity},
        )


def resolve_coordinates(
    connection: psycopg.Connection[Any] | None,
    instruments: Sequence[Sequence[str]],
    *,
    as_of: date,
    known_at: datetime | None = None,
) -> dict[str, tuple[str, str, str, str]]:
    if known_at is None:
        known_at = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    result: dict[str, tuple[str, str, str, str]] = {}
    for row in instruments:
        issuer_raw, instrument_raw, listing_raw, ticker = str(row[0]), str(row[1]), str(row[2]), str(row[3])
        if connection is None:
            result[listing_raw] = (issuer_raw, instrument_raw, listing_raw, ticker)
            continue
        issuer_uuid = resolve_entity(connection, issuer_raw, "issuer", as_of=as_of, known_at=known_at)
        instrument_uuid = resolve_entity(connection, instrument_raw, "instrument", as_of=as_of, known_at=known_at)
        listing_uuid = resolve_entity(connection, listing_raw, "listing", as_of=as_of, known_at=known_at)
        _assert_relation(connection, "issues", issuer_uuid, instrument_uuid, known_at=known_at)
        _assert_relation(connection, "listed_as", instrument_uuid, listing_uuid, known_at=known_at)
        if listing_raw in primary_listing_ids():
            _assert_relation(connection, "primary_listing", issuer_uuid, listing_uuid, known_at=known_at)
        if name := _resolve_issuer_display_name(connection, ticker, issuer_raw=issuer_raw, issuer_uuid=issuer_uuid):
            _assert_issuer_name_alias(connection, issuer_uuid, name, known_at=known_at)
        result[listing_raw] = (str(issuer_uuid), str(instrument_uuid), str(listing_uuid), ticker)
    return result


def alias_of(
    connection: psycopg.Connection[Any],
    entity_id: uuid.UUID | str,
    scheme: str,
    *,
    valid_at: date,
    known_at: datetime,
) -> str | None:
    entity_uuid = uuid.UUID(str(entity_id))
    row = connection.execute(
        "select alias.value from staging.entity_aliases alias"
        " where staging.entity_survivor(alias.entity_id, %s) = staging.entity_survivor(%s, %s)"
        " and alias.scheme = %s and alias.valid_from <= %s"
        " and coalesce(%s < staging.entity_alias_valid_to(alias.alias_id, %s), true)"
        " and alias.transaction_time <= %s order by alias.confidence desc, alias.transaction_time desc limit 1",
        (known_at, entity_uuid, known_at, scheme, valid_at, valid_at, known_at, known_at),
    ).fetchone()
    return row[0] if row else None
