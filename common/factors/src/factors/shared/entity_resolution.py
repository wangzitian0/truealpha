"""PIT entity resolution backed by the canonical staging entity ontology."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_LEGACY_ID_RE = re.compile(r"^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$")
_SEC_SERIES_RE = re.compile(r"^etf:series:(S[0-9]{9})$", re.IGNORECASE)
_CIK_RE = re.compile(r"^(?:issuer|company):cik:([0-9]{1,10})$", re.IGNORECASE)
_LEI_RE = re.compile(r"^(?:issuer|company):lei:([0-9A-Za-z]{18}[0-9]{2})$")
_ISIN_RE = re.compile(r"^(?:company|security):isin:([A-Za-z]{2}[0-9A-Za-z]{9}[0-9])$")
_CUSIP_RE = re.compile(r"^security:cusip:([0-9A-Za-z*@#]{8}[0-9])$")
_FIGI_RE = re.compile(r"^security:figi:([B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9])$", re.IGNORECASE)
_MIC_TICKER_RE = re.compile(r"^listing:([0-9a-z]{4}):([0-9a-z][0-9a-z.\-]*)$", re.IGNORECASE)

_SCHEME_ALIASES = {
    "sec_series": "sec-series",
    "moomoo": "moomoo-code",
}
_REVERSE_SCHEMES = {v: k for k, v in _SCHEME_ALIASES.items()}


def _map_scheme(identifier_type: str) -> str:
    return _SCHEME_ALIASES.get(identifier_type, identifier_type)


def _is_uuid(value: str | uuid.UUID) -> bool:
    return isinstance(value, uuid.UUID) or bool(_UUID_RE.match(str(value).strip()))


def _parse_entity_coords(entity_id: str, entity_type: str) -> tuple[str, str, str]:
    raw = str(entity_id).strip()
    etype = entity_type.strip().lower()
    if etype in ("etf", "fund") or raw.startswith("etf:"):
        kind = "fund"
    elif etype in ("analyst", "person") or raw.startswith("analyst:"):
        kind = "person"
    elif etype == "instrument" or raw.startswith(("company:isin:", "security:")):
        kind = "instrument"
    elif etype == "listing" or raw.startswith("listing:"):
        kind = "listing"
    else:
        kind = "issuer"

    if m := _SEC_SERIES_RE.match(raw):
        return kind, "sec-series", m.group(1).upper()
    if m := _CIK_RE.match(raw):
        return kind, "cik", m.group(1).zfill(10)
    if m := _LEI_RE.match(raw):
        return kind, "lei", m.group(1).upper()
    if m := _ISIN_RE.match(raw):
        return kind, "isin", m.group(1).upper()
    if m := _CUSIP_RE.match(raw):
        return kind, "cusip", m.group(1).upper()
    if m := _FIGI_RE.match(raw):
        return kind, "figi", m.group(1).upper()
    if m := _MIC_TICKER_RE.match(raw):
        return kind, "mic-ticker", f"{m.group(1).upper()}:{m.group(2).upper()}"
    if _LEGACY_ID_RE.match(raw):
        return kind, "legacy-id", raw
    return kind, "name", raw


def _survivor(conn, entity_uuid: uuid.UUID, as_of: datetime | None = None) -> uuid.UUID:
    row = conn.execute(
        "select staging.entity_survivor(%s, coalesce(%s, 'infinity'::timestamptz))",
        (entity_uuid, as_of),
    ).fetchone()
    return row[0] if row and row[0] else entity_uuid


def coerce_entity_uuid(conn, entity_ref: str | uuid.UUID, *, as_of: datetime | None = None) -> uuid.UUID | None:
    if isinstance(entity_ref, uuid.UUID):
        row = conn.execute("select entity_id from staging.entities where entity_id = %s", (entity_ref,)).fetchone()
        return _survivor(conn, row[0], as_of) if row else None
    raw = str(entity_ref).strip()
    if _UUID_RE.match(raw):
        parsed = uuid.UUID(raw)
        row = conn.execute("select entity_id from staging.entities where entity_id = %s", (parsed,)).fetchone()
        return _survivor(conn, row[0], as_of) if row else None
    if _LEGACY_ID_RE.match(raw):
        row = conn.execute(
            "select entity_id from staging.entity_aliases where scheme = 'legacy-id' and value = %s "
            "order by transaction_time desc, alias_id desc limit 1",
            (raw,),
        ).fetchone()
        if row:
            return _survivor(conn, row[0], as_of)
    for role_hint in ("issuer", "fund", "instrument", "listing", "person"):
        kind, scheme, value = _parse_entity_coords(raw, role_hint)
        if scheme != "legacy-id":
            if scheme != "name":
                row = conn.execute(
                    "select ea.entity_id from staging.entity_aliases ea "
                    "where ea.scheme = %s and ea.value = staging.entity_alias_normalize(%s, %s) "
                    "order by ea.transaction_time desc, ea.alias_id desc limit 1",
                    (scheme, scheme, value),
                ).fetchone()
                if row:
                    return _survivor(conn, row[0], as_of)
            minted = conn.execute(
                "select entity_id from staging.entities "
                "where kind = %s and birth_scheme = %s and birth_value = staging.entity_alias_normalize(%s, %s) "
                "and birth_generation = 1 limit 1",
                (kind, scheme, scheme, value),
            ).fetchone()
            if minted:
                return _survivor(conn, minted[0], as_of)
    return None


def _insert_alias_if_compatible(
    conn,
    entity_uuid: uuid.UUID,
    scheme: str,
    value: str,
    *,
    confidence: float,
    valid_from: str,
    tx_time: datetime,
    raw_ref: str,
    source: str,
) -> bool:
    norm_row = conn.execute("select staging.entity_alias_normalize(%s, %s)", (scheme, value)).fetchone()
    norm_val = norm_row[0] if norm_row else value
    scheme_row = conn.execute(
        "select s.applies_to_kind, s.is_unique, s.value_reusable, s.value_pattern, e.kind, "
        "staging.entity_kind_is_a(e.kind, s.applies_to_kind) "
        "from staging.entity_alias_schemes s, staging.entities e "
        "where s.scheme = %s and e.entity_id = %s",
        (scheme, entity_uuid),
    ).fetchone()
    if not scheme_row:
        return False
    _applies_to, is_unique, value_reusable, pattern, _ekind, compatible = scheme_row
    if not compatible or not norm_val or not re.match(pattern, norm_val):
        return False

    latest = conn.execute(
        "select ea.alias_id, ea.entity_id, ea.confidence "
        "from staging.entity_aliases ea "
        "left join lateral ("
        "  select min(r.valid_to) as valid_to from staging.entity_retractions r "
        "  where r.alias_id = ea.alias_id and r.transaction_time <= least(%s::timestamptz, clock_timestamp())"
        ") ret on true "
        "where ea.scheme = %s and ea.value = %s "
        "  and ea.transaction_time <= least(%s::timestamptz, clock_timestamp()) "
        "  and coalesce(least(ea.valid_to, ret.valid_to), 'infinity'::date) > ea.valid_from "
        "order by ea.transaction_time desc, ea.alias_id desc limit 1",
        (tx_time, scheme, norm_val, tx_time),
    ).fetchone()
    if scheme != "name" and latest is not None and latest[1] == entity_uuid and float(latest[2]) == float(confidence):
        return False
    if is_unique and latest is not None and latest[1] != entity_uuid:
        if _survivor(conn, latest[1], tx_time) != _survivor(conn, entity_uuid, tx_time):
            if not value_reusable:
                return False
            conn.execute(
                "insert into staging.entity_retractions "
                "(alias_id, valid_to, reason, transaction_time, source, raw_ref) "
                "values (%s, %s::date, 'superseded-identifier', least(%s::timestamptz, clock_timestamp()), %s, %s) "
                "on conflict do nothing",
                (latest[0], valid_from, tx_time, source, raw_ref),
            )
    inserted = conn.execute(
        "insert into staging.entity_aliases "
        "(entity_id, scheme, value, valid_from, valid_to, transaction_time, source, raw_ref, method, confidence, mapping_version) "
        "values (%s, %s, %s, %s::date, null, least(%s::timestamptz, clock_timestamp()), %s, %s, 'asserted', %s, 'er:v1') "
        "on conflict do nothing returning alias_id",
        (entity_uuid, scheme, norm_val, valid_from, tx_time, source, raw_ref, confidence),
    ).fetchone()
    return inserted is not None


def _validated_birth_coords(conn, kind: str, scheme: str, value: str, raw: str) -> tuple[str, str]:
    norm_row = conn.execute("select staging.entity_alias_normalize(%s, %s)", (scheme, value)).fetchone()
    norm_val = (norm_row[0] if norm_row else value) or ""
    row = conn.execute(
        "select s.value_pattern, staging.entity_kind_is_a(%s, s.applies_to_kind) "
        "from staging.entity_alias_schemes s where s.scheme = %s",
        (kind, scheme),
    ).fetchone()
    if row and len(row) >= 2 and row[1] and norm_val and re.match(str(row[0]), norm_val):
        return scheme, norm_val
    fallback = re.sub(r"\s+", "_", raw.strip()) or "synthetic"
    return "legacy-id", fallback


def ensure_entity(
    conn,
    entity_id: str | uuid.UUID,
    entity_type: str,
    display_name: str,
    *,
    transaction_time: datetime | None = None,
    raw_ref: str = "entity_resolution:v1",
) -> uuid.UUID:
    """Resolve or mint an entity in staging.entities and return its canonical UUID."""
    tx = transaction_time or datetime.now(UTC)
    raw = str(entity_id).strip()
    existing = coerce_entity_uuid(conn, entity_id)
    if existing is None:
        if _UUID_RE.match(raw):
            kind = {
                "etf": "fund",
                "fund": "fund",
                "analyst": "person",
                "person": "person",
                "instrument": "instrument",
                "listing": "listing",
            }.get(entity_type.lower(), "issuer")
            birth_val = f"uuid:{raw.lower()}"
            minted = conn.execute(
                "select staging.entity_mint(%s, 'legacy-id', %s, 'entity_resolution:v1')",
                (kind, birth_val),
            ).fetchone()
            existing = minted[0]
            _insert_alias_if_compatible(
                conn,
                existing,
                "legacy-id",
                birth_val,
                confidence=1.0,
                valid_from="-infinity",
                tx_time=tx,
                raw_ref=raw_ref,
                source="entity_resolution",
            )
        else:
            kind, scheme, value = _parse_entity_coords(raw, entity_type)
            norm_val = conn.execute("select staging.entity_alias_normalize(%s, %s)", (scheme, value)).fetchone()[0]
            if _LEGACY_ID_RE.match(raw):
                minted = conn.execute(
                    "select staging.entity_mint(%s, 'legacy-id', %s, 'entity_resolution:v1')",
                    (kind, raw),
                ).fetchone()
                existing = minted[0]
                _insert_alias_if_compatible(
                    conn,
                    existing,
                    "legacy-id",
                    raw,
                    confidence=1.0,
                    valid_from="-infinity",
                    tx_time=tx,
                    raw_ref=raw_ref,
                    source="entity_resolution",
                )
                if scheme != "legacy-id":
                    _insert_alias_if_compatible(
                        conn,
                        existing,
                        scheme,
                        norm_val,
                        confidence=1.0,
                        valid_from="-infinity",
                        tx_time=tx,
                        raw_ref=raw_ref,
                        source="entity_resolution",
                    )
            else:
                mint_scheme, mint_val = _validated_birth_coords(conn, kind, scheme, norm_val, raw)
                minted = conn.execute(
                    "select staging.entity_mint(%s, %s, %s, 'entity_resolution:v1')",
                    (kind, mint_scheme, mint_val),
                ).fetchone()
                existing = minted[0]
                _insert_alias_if_compatible(
                    conn,
                    existing,
                    mint_scheme,
                    mint_val,
                    confidence=1.0,
                    valid_from="-infinity",
                    tx_time=tx,
                    raw_ref=raw_ref,
                    source="entity_resolution",
                )
    elif _LEGACY_ID_RE.match(raw):
        _insert_alias_if_compatible(
            conn,
            existing,
            "legacy-id",
            raw,
            confidence=1.0,
            valid_from="-infinity",
            tx_time=tx,
            raw_ref=raw_ref,
            source="entity_resolution",
        )

    clean_name = (display_name or "").strip()
    if clean_name and clean_name != str(entity_id).strip():
        already_named = conn.execute(
            "select 1 from staging.entity_aliases where entity_id = %s and scheme = 'name' and value = %s limit 1",
            (existing, clean_name),
        ).fetchone()
        if not already_named:
            _insert_alias_if_compatible(
                conn,
                existing,
                "name",
                clean_name,
                confidence=0.9,
                valid_from="-infinity",
                tx_time=tx,
                raw_ref=raw_ref,
                source="entity_resolution",
            )
    return existing


def _append_if_changed(
    conn,
    *,
    latest_sql: str,
    latest_params: tuple[object, ...],
    expected_head: object,
    confidence: float,
    insert_sql: str,
    insert_params: tuple[object, ...],
) -> bool:
    latest = conn.execute(latest_sql, latest_params).fetchone()
    if latest is not None and latest[0] == expected_head and float(latest[1]) == confidence:
        return False
    return conn.execute(insert_sql, insert_params).fetchone() is not None


def _linked_instrument_for(
    conn,
    base_uuid: uuid.UUID,
    scheme: str,
    value: str,
    *,
    tx_time: datetime,
    valid_from: str,
    raw_ref: str,
    source: str,
) -> uuid.UUID:
    existing = conn.execute(
        "select entity_id from staging.entity_aliases where scheme = %s and value = staging.entity_alias_normalize(%s, %s) "
        "order by transaction_time desc, alias_id desc limit 1",
        (scheme, scheme, value),
    ).fetchone()
    if existing:
        inst_uuid = _survivor(conn, existing[0], tx_time)
    else:
        inst_uuid = ensure_entity(
            conn, f"security:{scheme}:{value}", "instrument", value, transaction_time=tx_time, raw_ref=raw_ref
        )
    add_edge(
        conn,
        from_id=base_uuid,
        to_id=inst_uuid,
        relation_type="issues",
        confidence=1.0,
        source=source,
        transaction_time=tx_time,
        valid_from=valid_from,
        raw_ref=raw_ref,
    )
    return inst_uuid


def _ensure_synthetic_instrument(
    conn,
    owner_uuid: uuid.UUID,
    *,
    confidence: float,
    source: str,
    tx_time: datetime,
    valid_from: str,
    raw_ref: str,
) -> uuid.UUID:
    inst_row = conn.execute(
        "select to_entity_id from staging.entity_relations "
        "where from_entity_id = %s and relation_type = 'issues' order by transaction_time desc, relation_id desc limit 1",
        (owner_uuid,),
    ).fetchone()
    if inst_row:
        return inst_row[0]
    synthetic_id = f"security:synthetic:{owner_uuid}"
    inst_uuid = conn.execute(
        "select staging.entity_mint('instrument', 'legacy-id', %s, 'entity_resolution:v1')",
        (synthetic_id,),
    ).fetchone()[0]
    _insert_alias_if_compatible(
        conn,
        inst_uuid,
        "legacy-id",
        synthetic_id,
        confidence=1.0,
        valid_from="-infinity",
        tx_time=tx_time,
        raw_ref=raw_ref,
        source=source,
    )
    add_edge(
        conn,
        from_id=owner_uuid,
        to_id=inst_uuid,
        relation_type="issues",
        confidence=confidence,
        source=source,
        transaction_time=tx_time,
        valid_from=valid_from,
        raw_ref=raw_ref,
    )
    return inst_uuid


def _linked_listing_for(
    conn,
    base_uuid: uuid.UUID,
    scheme: str,
    value: str,
    *,
    tx_time: datetime,
    valid_from: str,
    raw_ref: str,
    source: str,
) -> uuid.UUID:
    existing = conn.execute(
        "select entity_id from staging.entity_aliases where scheme = %s and value = staging.entity_alias_normalize(%s, %s) "
        "order by transaction_time desc, alias_id desc limit 1",
        (scheme, scheme, value),
    ).fetchone()
    if existing:
        list_uuid = _survivor(conn, existing[0], tx_time)
    else:
        mint_scheme, mint_val = _validated_birth_coords(conn, "listing", scheme, value, f"listing:{scheme}:{value}")
        minted = conn.execute(
            "select staging.entity_mint('listing', %s, %s, 'entity_resolution:v1')",
            (mint_scheme, mint_val),
        ).fetchone()
        list_uuid = minted[0]
        _insert_alias_if_compatible(
            conn,
            list_uuid,
            mint_scheme,
            mint_val,
            confidence=1.0,
            valid_from="-infinity",
            tx_time=tx_time,
            raw_ref=raw_ref,
            source=source,
        )

    base_kind_row = conn.execute("select kind from staging.entities where entity_id = %s", (base_uuid,)).fetchone()
    base_kind = base_kind_row[0] if base_kind_row else "issuer"
    if base_kind == "instrument":
        inst_uuid = base_uuid
    else:
        inst_uuid = _ensure_synthetic_instrument(
            conn,
            base_uuid,
            confidence=1.0,
            source=source,
            tx_time=tx_time,
            valid_from=valid_from,
            raw_ref=raw_ref,
        )
    add_edge(
        conn,
        from_id=inst_uuid,
        to_id=list_uuid,
        relation_type="listed_as",
        confidence=1.0,
        source=source,
        transaction_time=tx_time,
        valid_from=valid_from,
        raw_ref=raw_ref,
    )
    return list_uuid


def assert_identifier(
    conn,
    *,
    entity_id: str | uuid.UUID,
    source: str,
    identifier_type: str,
    identifier_value: str,
    confidence: float,
    transaction_time: datetime,
    valid_from: str,
    raw_ref: str,
) -> bool:
    base_uuid = coerce_entity_uuid(conn, entity_id)
    if base_uuid is None:
        base_uuid = ensure_entity(
            conn, entity_id, "company", str(entity_id), transaction_time=transaction_time, raw_ref=raw_ref
        )

    scheme = _map_scheme(identifier_type)
    norm_val = conn.execute("select staging.entity_alias_normalize(%s, %s)", (scheme, identifier_value)).fetchone()[0]
    info = conn.execute(
        "select s.applies_to_kind, e.kind, staging.entity_kind_is_a(e.kind, s.applies_to_kind) "
        "from staging.entity_alias_schemes s, staging.entities e where s.scheme = %s and e.entity_id = %s",
        (scheme, base_uuid),
    ).fetchone()
    if not info:
        return False
    applies_to, base_kind, compatible = info
    target_uuid = base_uuid
    if not compatible:
        if applies_to == "instrument" and base_kind in ("issuer", "fund"):
            target_uuid = _linked_instrument_for(
                conn,
                base_uuid,
                scheme,
                norm_val,
                tx_time=transaction_time,
                valid_from=valid_from,
                raw_ref=raw_ref,
                source=source,
            )
        elif applies_to == "listing" and base_kind in ("issuer", "fund", "instrument"):
            target_uuid = _linked_listing_for(
                conn,
                base_uuid,
                scheme,
                norm_val,
                tx_time=transaction_time,
                valid_from=valid_from,
                raw_ref=raw_ref,
                source=source,
            )
        else:
            return False

    return _insert_alias_if_compatible(
        conn,
        target_uuid,
        scheme,
        norm_val,
        confidence=confidence,
        valid_from=valid_from,
        tx_time=transaction_time,
        raw_ref=raw_ref,
        source=source,
    )


def add_edge(
    conn,
    *,
    from_id: str | uuid.UUID,
    to_id: str | uuid.UUID,
    relation_type: str,
    confidence: float,
    source: str,
    transaction_time: datetime,
    valid_from: str,
    raw_ref: str,
    valid_to: str | None = None,
) -> bool:
    from_uuid = coerce_entity_uuid(conn, from_id)
    to_uuid = coerce_entity_uuid(conn, to_id)
    if from_uuid is None or to_uuid is None:
        return False

    from_kind = conn.execute("select kind from staging.entities where entity_id = %s", (from_uuid,)).fetchone()[0]
    to_kind = conn.execute("select kind from staging.entities where entity_id = %s", (to_uuid,)).fetchone()[0]

    # Convert legacy cross-kind same_as between issuer/fund and instrument into issues(issuer/fund -> instrument).
    if relation_type == "same_as" and from_kind == "instrument" and to_kind in ("issuer", "fund"):
        relation_type = "issues"
        from_uuid, to_uuid = to_uuid, from_uuid
        from_kind, to_kind = to_kind, from_kind
    elif relation_type == "same_as" and from_kind in ("issuer", "fund") and to_kind == "instrument":
        relation_type = "issues"

    if relation_type == "holds" and from_kind == "fund" and to_kind != "instrument":
        to_uuid = _ensure_synthetic_instrument(
            conn,
            to_uuid,
            confidence=confidence,
            source=source,
            tx_time=transaction_time,
            valid_from=valid_from,
            raw_ref=raw_ref,
        )
        to_kind = "instrument"

    rel_meta = conn.execute(
        "select domain_kind, range_kind, same_kind from staging.entity_relation_types where relation_type = %s",
        (relation_type,),
    ).fetchone()
    if rel_meta is not None:
        domain_kind, range_kind, same_kind = rel_meta
        domain_ok = conn.execute("select staging.entity_kind_is_a(%s, %s)", (from_kind, domain_kind)).fetchone()[0]
        range_ok = conn.execute("select staging.entity_kind_is_a(%s, %s)", (to_kind, range_kind)).fetchone()[0]
        if not domain_ok or not range_ok or (same_kind and from_kind != to_kind):
            return False

    latest = conn.execute(
        "select source, confidence from staging.entity_relations "
        "where from_entity_id = %s and to_entity_id = %s and relation_type = %s "
        "order by transaction_time desc, relation_id desc limit 1",
        (from_uuid, to_uuid, relation_type),
    ).fetchone()
    if latest is not None and latest[0] == source and float(latest[1]) == float(confidence):
        return False

    row = conn.execute(
        "with eff as (select least(%s::timestamptz, clock_timestamp()) as tx) "
        "insert into staging.entity_relations "
        "(relation_id, relation_type, from_entity_id, to_entity_id, valid_from, valid_to, "
        " transaction_time, source, raw_ref, method, confidence, mapping_version) "
        "select staging.entity_relation_uuid(%s, %s, %s, %s::date, eff.tx, %s, 'asserted'), "
        "       %s, %s, %s, %s::date, %s::date, eff.tx, %s, %s, 'asserted', %s, 'er:v1' "
        "from eff on conflict do nothing returning relation_id",
        (
            transaction_time,
            relation_type,
            from_uuid,
            to_uuid,
            valid_from,
            source,
            relation_type,
            from_uuid,
            to_uuid,
            valid_from,
            valid_to,
            source,
            raw_ref,
            confidence,
        ),
    ).fetchone()
    return row is not None


def resolve(conn, identifier_type: str, value: str, *, as_of: datetime) -> str | None:
    scheme = _map_scheme(identifier_type)
    valid_date = as_of.date() if isinstance(as_of, datetime) else as_of
    row = conn.execute(
        "select ea.entity_id from staging.entity_aliases ea "
        "where ea.scheme = %s and ea.value = staging.entity_alias_normalize(%s, %s) "
        "  and ea.transaction_time <= %s "
        "  and ea.valid_from <= %s::date "
        "  and coalesce(staging.entity_alias_valid_to(ea.alias_id, %s), 'infinity'::date) > %s::date "
        "order by ea.transaction_time desc, ea.confidence desc, ea.alias_id desc limit 1",
        (scheme, scheme, value, as_of, valid_date, as_of, valid_date),
    ).fetchone()
    if row is None:
        return None
    ent_uuid = _survivor(conn, row[0], as_of)
    seen = {ent_uuid}
    for _ in range(16):
        merged = conn.execute(
            "select r.to_entity_id from staging.entity_relations r "
            "where r.from_entity_id = %s and r.relation_type = 'same_as' and r.transaction_time <= %s "
            "  and r.valid_from <= %s::date "
            "  and coalesce(staging.entity_relation_valid_to(r.relation_id, %s), 'infinity'::date) > %s::date "
            "order by r.transaction_time desc, r.relation_id desc limit 1",
            (ent_uuid, as_of, valid_date, as_of, valid_date),
        ).fetchone()
        if not merged:
            break
        nxt = _survivor(conn, merged[0], as_of)
        if nxt in seen:
            break
        seen.add(nxt)
        ent_uuid = nxt
    return str(ent_uuid)


def crosswalk(conn, entity_id: str | uuid.UUID, *, as_of: datetime) -> dict[str, list[str]]:
    target = coerce_entity_uuid(conn, entity_id, as_of=as_of)
    if target is None:
        return {}
    valid_date = as_of.date() if isinstance(as_of, datetime) else as_of
    rows = conn.execute(
        "with recursive merged as ("
        "  select %s::uuid as eid "
        "  union "
        "  select r.from_entity_id from staging.entity_relations r "
        "  join merged m on m.eid = r.to_entity_id "
        "  where r.relation_type = 'superseded_by' and r.transaction_time <= %s "
        "    and not exists (select 1 from staging.entity_retractions ret where ret.relation_id = r.relation_id and ret.transaction_time <= %s)"
        "), related as ("
        "  select eid from merged "
        "  union select r.to_entity_id from staging.entity_relations r join merged m on m.eid = r.from_entity_id "
        "        where r.relation_type in ('issues', 'listed_as') and r.transaction_time <= %s "
        "          and r.valid_from <= %s::date and coalesce(staging.entity_relation_valid_to(r.relation_id, %s), 'infinity'::date) > %s::date "
        "  union select r.from_entity_id from staging.entity_relations r join merged m on m.eid = r.to_entity_id "
        "        where r.relation_type = 'issues' and r.transaction_time <= %s "
        "          and r.valid_from <= %s::date and coalesce(staging.entity_relation_valid_to(r.relation_id, %s), 'infinity'::date) > %s::date "
        "  union select r2.to_entity_id from staging.entity_relations r1 join merged m on m.eid = r1.from_entity_id "
        "        join staging.entity_relations r2 on r2.from_entity_id = r1.to_entity_id "
        "        where r1.relation_type = 'issues' and r2.relation_type = 'listed_as' "
        "          and r1.transaction_time <= %s and r2.transaction_time <= %s "
        "          and r1.valid_from <= %s::date and coalesce(staging.entity_relation_valid_to(r1.relation_id, %s), 'infinity'::date) > %s::date "
        "          and r2.valid_from <= %s::date and coalesce(staging.entity_relation_valid_to(r2.relation_id, %s), 'infinity'::date) > %s::date"
        ") "
        "select distinct ea.scheme, ea.value from staging.entity_aliases ea "
        "join related r on r.eid = ea.entity_id "
        "where ea.transaction_time <= %s and ea.valid_from <= %s::date "
        "  and coalesce(staging.entity_alias_valid_to(ea.alias_id, %s), 'infinity'::date) > %s::date "
        "  and ea.scheme not in ('legacy-id', 'name')",
        (
            target,
            as_of,
            as_of,
            as_of,
            valid_date,
            as_of,
            valid_date,
            as_of,
            valid_date,
            as_of,
            valid_date,
            as_of,
            as_of,
            valid_date,
            as_of,
            valid_date,
            valid_date,
            as_of,
            valid_date,
            as_of,
            valid_date,
            as_of,
            valid_date,
        ),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for scheme, val in rows:
        key = _REVERSE_SCHEMES.get(scheme, scheme)
        out.setdefault(key, []).append(val)
    return {k: sorted(vals) for k, vals in out.items()}


def identifiers(conn, identifier_type: str, *, as_of: datetime) -> list[tuple[str, str]]:
    scheme = _map_scheme(identifier_type)
    valid_date = as_of.date() if isinstance(as_of, datetime) else as_of
    rows = conn.execute(
        "select distinct on (ea.value) ea.value, staging.entity_survivor(ea.entity_id, %s)::text "
        "from staging.entity_aliases ea "
        "where ea.scheme = %s and ea.transaction_time <= %s "
        "  and ea.valid_from <= %s::date "
        "  and coalesce(staging.entity_alias_valid_to(ea.alias_id, %s), 'infinity'::date) > %s::date "
        "order by ea.value, ea.transaction_time desc, ea.confidence desc, ea.alias_id desc",
        (as_of, scheme, as_of, valid_date, as_of, valid_date),
    ).fetchall()
    return sorted(rows)
