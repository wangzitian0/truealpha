"""Entity resolution over the knowledge graph (staging.kg_entities / staging.kg_edges).

All cross-source ID crosswalk (CIK <-> ticker <-> moomoo_code <-> CUSIP/ISIN) goes
through `same_as` edges here — no module keeps its own mapping table (init.md
Section 6).

Model: every source-local identifier is itself a node (entity_type='identifier',
id 'id:<namespace>:<value>') with a same_as edge pointing at the canonical entity
it identifies. Canonical entities use the most durable id available at creation:
'company:cik:<n>' for SEC filers, 'company:isin:<ISIN>' otherwise,
'etf:series:<S000...>' for funds (fund series ids are durable; trust CIKs cover
many series). same_as can also link canonical->canonical when two entities are
later found to be the same real-world thing — resolve() follows one such merge hop.

Functions take an open DB connection (psycopg-style: conn.execute(sql, params)
returns a cursor) rather than owning connection config, so this library stays
free of any driver dependency; data-engine / llm-service pass their own
connection in and control transaction boundaries (nothing here commits).

Point-in-time: reads filter transaction_time <= as_of; writes are append-only
vintages, never updates (CLAUDE.md hard constraints).
"""

import json
from datetime import UTC, datetime


def identifier_node_id(namespace: str, value: str) -> str:
    return f"id:{namespace}:{value}"


def ensure_entity(conn, entity_id: str, entity_type: str, display_name: str) -> None:
    """Register an entity if absent. kg_entities is a registry, not a point-in-time
    table — an existing row is left untouched (first writer wins on display_name)."""
    conn.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, %s, %s) on conflict (id) do nothing",
        (entity_id, entity_type, display_name),
    )


def add_edge(
    conn,
    *,
    from_id: str,
    to_id: str,
    relation_type: str,
    confidence: float,
    source: str,
    valid_from: str,
    raw_ref: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Append a new edge vintage; returns False if an identical assertion already
    exists. Identity is (from, to, relation, source, confidence, properties) —
    valid_from and raw_ref deliberately don't count, so re-running a bootstrap on a
    later day skips unchanged facts instead of spraying duplicate vintages, while a
    real change (a new N-PORT period in properties, a revised confidence) appends.
    Existence is checked against ALL vintages, not just the latest: parallel edges
    between the same pair that differ only in properties (e.g. one 'holds' edge per
    A-share/H-share line, discriminated by properties.isin) must coexist without
    re-inserting each other on every run."""
    props_json = json.dumps(properties, sort_keys=True) if properties is not None else None
    exists = conn.execute(
        """
        select 1 from staging.kg_edges
        where from_id = %s and to_id = %s and relation_type = %s and source = %s
          and confidence = %s and properties is not distinct from %s::jsonb
        limit 1
        """,
        (from_id, to_id, relation_type, source, confidence, props_json),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        """
        insert into staging.kg_edges
            (from_id, to_id, relation_type, valid_time, confidence, source, raw_ref, properties)
        values (%s, %s, %s, daterange(%s::date, null, '[)'), %s, %s, %s, %s::jsonb)
        """,
        (from_id, to_id, relation_type, valid_from, confidence, source, raw_ref, props_json),
    )
    return True


def add_same_as(
    conn,
    *,
    namespace: str,
    value: str,
    entity_id: str,
    confidence: float,
    source: str,
    valid_from: str,
    raw_ref: str | None = None,
) -> bool:
    """Assert 'this source-local identifier refers to this entity'. Creates the
    identifier node on first sight."""
    node = identifier_node_id(namespace, value)
    ensure_entity(conn, node, "identifier", f"{namespace}:{value}")
    return add_edge(
        conn,
        from_id=node,
        to_id=entity_id,
        relation_type="same_as",
        confidence=confidence,
        source=source,
        valid_from=valid_from,
        raw_ref=raw_ref,
    )


def _same_as_target(conn, from_id: str, as_of: datetime):
    return conn.execute(
        """
        select to_id from staging.kg_edges
        where from_id = %s and relation_type = 'same_as' and transaction_time <= %s
        order by transaction_time desc, confidence desc
        limit 1
        """,
        (from_id, as_of),
    ).fetchone()


def resolve(conn, namespace: str, value: str, *, as_of: datetime | None = None) -> str | None:
    """Return the unified entity id for a source-local identifier as visible at
    as_of, or None if unknown. Follows the identifier's same_as edge, then at most
    one canonical->canonical merge hop."""
    as_of = as_of or datetime.now(UTC)
    row = _same_as_target(conn, identifier_node_id(namespace, value), as_of)
    if row is None:
        return None
    canonical = row[0]
    merged = _same_as_target(conn, canonical, as_of)
    return merged[0] if merged else canonical


def crosswalk(conn, entity_id: str, *, as_of: datetime | None = None) -> dict[str, list[str]]:
    """All source-local identifiers pointing at an entity, grouped by namespace:
    {'ticker': ['GOOGL', 'GOOG'], 'cik': ['1652044'], ...}. One hop only — does not
    chase canonical merges."""
    as_of = as_of or datetime.now(UTC)
    rows = conn.execute(
        """
        select distinct from_id from staging.kg_edges
        where to_id = %s and relation_type = 'same_as' and transaction_time <= %s
          and from_id like 'id:%%'
        """,
        (entity_id, as_of),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for (from_id,) in rows:
        _, namespace, value = from_id.split(":", 2)
        out.setdefault(namespace, []).append(value)
    return {ns: sorted(vals) for ns, vals in out.items()}


def identifiers(conn, namespace: str, *, as_of: datetime | None = None) -> list[tuple[str, str]]:
    """Every (value, entity_id) pair in one identifier namespace — e.g. all moomoo
    codes in the KG, for a sweep to iterate. Latest vintage per identifier."""
    as_of = as_of or datetime.now(UTC)
    rows = conn.execute(
        """
        select distinct on (from_id) from_id, to_id from staging.kg_edges
        where relation_type = 'same_as' and from_id like %s and transaction_time <= %s
        order by from_id, transaction_time desc, confidence desc
        """,
        (f"id:{namespace}:%", as_of),
    ).fetchall()
    return sorted((from_id.split(":", 2)[2], to_id) for from_id, to_id in rows)
