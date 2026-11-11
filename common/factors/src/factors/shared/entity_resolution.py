"""Point-in-time entity resolution over staging.kg_entities, kg_identifiers, and kg_edges."""

from datetime import datetime


def ensure_entity(conn, entity_id: str, entity_type: str, display_name: str) -> None:
    conn.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, %s, %s) on conflict (id) do nothing",
        (entity_id, entity_type, display_name),
    )


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


def assert_identifier(
    conn,
    *,
    entity_id: str,
    source: str,
    identifier_type: str,
    identifier_value: str,
    confidence: float,
    transaction_time: datetime,
    valid_from: str,
    raw_ref: str,
) -> bool:
    return _append_if_changed(
        conn,
        latest_sql="""
            select entity_id, confidence from staging.kg_identifiers
            where identifier_type = %s and identifier_value = %s
            order by transaction_time desc, id desc
            limit 1
        """,
        latest_params=(identifier_type, identifier_value),
        expected_head=entity_id,
        confidence=confidence,
        insert_sql="""
            insert into staging.kg_identifiers
                (entity_id, source, identifier_type, identifier_value, valid_time,
                 transaction_time, confidence, raw_ref)
            values (%s, %s, %s, %s, daterange(%s::date, null, '[)'), %s, %s, %s)
            on conflict (source, identifier_type, identifier_value, transaction_time) do nothing
            returning id
        """,
        insert_params=(
            entity_id,
            source,
            identifier_type,
            identifier_value,
            valid_from,
            transaction_time,
            confidence,
            raw_ref,
        ),
    )


def add_edge(
    conn,
    *,
    from_id: str,
    to_id: str,
    relation_type: str,
    confidence: float,
    source: str,
    transaction_time: datetime,
    valid_from: str,
    raw_ref: str,
) -> bool:
    return _append_if_changed(
        conn,
        latest_sql="""
            select source, confidence from staging.kg_edges
            where from_id = %s and to_id = %s and relation_type = %s
            order by transaction_time desc, id desc
            limit 1
        """,
        latest_params=(from_id, to_id, relation_type),
        expected_head=source,
        confidence=confidence,
        insert_sql="""
            insert into staging.kg_edges
                (from_id, to_id, relation_type, valid_time, transaction_time, confidence, source, raw_ref)
            values (%s, %s, %s, daterange(%s::date, null, '[)'), %s, %s, %s, %s)
            on conflict do nothing
            returning id
        """,
        insert_params=(from_id, to_id, relation_type, valid_from, transaction_time, confidence, source, raw_ref),
    )


def resolve(conn, identifier_type: str, value: str, *, as_of: datetime) -> str | None:
    row = conn.execute(
        """
        select entity_id from staging.kg_identifiers
        where identifier_type = %s and identifier_value = %s and transaction_time <= %s
        order by transaction_time desc, confidence desc, id desc
        limit 1
        """,
        (identifier_type, value, as_of),
    ).fetchone()
    if row is None:
        return None
    merged = conn.execute(
        """
        select to_id from staging.kg_edges
        where from_id = %s and relation_type = 'same_as' and transaction_time <= %s
        order by transaction_time desc, id desc
        limit 1
        """,
        (row[0], as_of),
    ).fetchone()
    return merged[0] if merged else row[0]


def crosswalk(conn, entity_id: str, *, as_of: datetime) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        select distinct identifier_type, identifier_value from staging.kg_identifiers
        where entity_id = %s and transaction_time <= %s
        """,
        (entity_id, as_of),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for identifier_type, value in rows:
        out.setdefault(identifier_type, []).append(value)
    return {t: sorted(vals) for t, vals in out.items()}


def identifiers(conn, identifier_type: str, *, as_of: datetime) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        select distinct on (identifier_value) identifier_value, entity_id
        from staging.kg_identifiers
        where identifier_type = %s and transaction_time <= %s
        order by identifier_value, transaction_time desc, confidence desc, id desc
        """,
        (identifier_type, as_of),
    ).fetchall()
    return sorted(rows)
