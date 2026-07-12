"""Entity resolution over the knowledge graph (staging.kg_entities /
staging.kg_identifiers / staging.kg_edges).

All cross-source ID crosswalk (CIK <-> ticker <-> moomoo_code <-> CUSIP/ISIN)
goes through `staging.kg_identifiers` (db/migrations/0004_runtime_contracts.sql)
— typed identifier rows pointing at canonical entities — no module keeps its own
mapping table. Canonical entities use the most durable id available at creation:
'company:cik:<n>' for SEC filers, 'company:isin:<ISIN>' otherwise,
'etf:series:<S000...>' for funds (fund series ids are durable; trust CIKs cover
many series). `same_as` edges in kg_edges link canonical->canonical when two
entities are later found to be the same real-world thing — resolve() follows one
such merge hop; relationship edges ('holds', 'supplies_to', ...) also live in
kg_edges, with holdings WEIGHTS in staging.fund_holding_facts, not on the edge.

Functions take an open DB connection (psycopg-style: conn.execute(sql, params)
returns a cursor) rather than owning connection config, so this library stays
free of any driver dependency; data-engine / llm-service pass their own
connection in and control transaction boundaries (nothing here commits).

Point-in-time: reads filter transaction_time <= as_of; writes are append-only
vintages with an EXPLICIT transaction_time (the runtime contract dropped the
column defaults: when a fact became knowable is the source's property, never the
insert clock). "Newest wins" tiebreaks on the strictly-increasing identity id,
since vintages written in one transaction can share a timestamp.
"""

from datetime import datetime


def ensure_entity(conn, entity_id: str, entity_type: str, display_name: str) -> None:
    """Register an entity if absent. kg_entities is a registry, not a point-in-time
    table — an existing row is left untouched (first writer wins on display_name)."""
    conn.execute(
        "insert into staging.kg_entities (id, entity_type, display_name) values (%s, %s, %s) on conflict (id) do nothing",
        (entity_id, entity_type, display_name),
    )


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
    """Assert 'this source-local identifier refers to this entity'; returns False
    when the assertion is already current. Identifier rows are pointer-like: the
    newest vintage per (identifier_type, identifier_value) wins in resolve(), so
    a previously superseded mapping can always be re-asserted as a new vintage —
    only re-asserting what the latest vintage already says is skipped (comparing
    against all history would make any correction permanent)."""
    latest = conn.execute(
        """
        select entity_id, confidence from staging.kg_identifiers
        where identifier_type = %s and identifier_value = %s
        order by transaction_time desc, id desc
        limit 1
        """,
        (identifier_type, identifier_value),
    ).fetchone()
    if latest is not None and latest[0] == entity_id and float(latest[1]) == confidence:
        return False
    inserted = conn.execute(
        """
        insert into staging.kg_identifiers
            (entity_id, source, identifier_type, identifier_value, valid_time,
             transaction_time, confidence, raw_ref)
        values (%s, %s, %s, %s, daterange(%s::date, null, '[)'), %s, %s, %s)
        on conflict (source, identifier_type, identifier_value, transaction_time) do nothing
        returning id
        """,
        (entity_id, source, identifier_type, identifier_value, valid_from, transaction_time, confidence, raw_ref),
    ).fetchone()
    return inserted is not None


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
    """Append a relationship-edge vintage; returns False when the latest vintage
    for (from, to, relation) already asserts the same (source, confidence) — a
    re-run of the same ingestion must not spray duplicate vintages, while a new
    filing period (new transaction_time) or a revised assertion appends. The
    uq_kg_edges_vintage index additionally rejects exact duplicates racing within
    one transaction (on conflict do nothing)."""
    latest = conn.execute(
        """
        select source, confidence, lower(valid_time), raw_ref from staging.kg_edges
        where from_id = %s and to_id = %s and relation_type = %s
        order by transaction_time desc, id desc
        limit 1
        """,
        (from_id, to_id, relation_type),
    ).fetchone()
    if (
        latest is not None
        and latest[0] == source
        and float(latest[1]) == confidence
        and latest[2].isoformat() == valid_from
        and latest[3] == raw_ref
    ):
        return False
    inserted = conn.execute(
        """
        insert into staging.kg_edges
            (from_id, to_id, relation_type, valid_time, transaction_time, confidence, source, raw_ref)
        values (%s, %s, %s, daterange(%s::date, null, '[)'), %s, %s, %s, %s)
        on conflict do nothing
        returning id
        """,
        (from_id, to_id, relation_type, valid_from, transaction_time, confidence, source, raw_ref),
    ).fetchone()
    return inserted is not None


def _merge_target(conn, entity_id: str, as_of: datetime):
    return conn.execute(
        """
        select to_id from staging.kg_edges
        where from_id = %s and relation_type = 'same_as' and transaction_time <= %s
        order by transaction_time desc, id desc
        limit 1
        """,
        (entity_id, as_of),
    ).fetchone()


def resolve(conn, identifier_type: str, value: str, *, as_of: datetime) -> str | None:
    """Return the unified entity id for a source-local identifier as visible at
    as_of, or None if unknown. Reads the newest kg_identifiers vintage (any
    asserting source; highest confidence breaks same-instant ties), then follows
    at most one canonical->canonical same_as merge hop.

    as_of is deliberately required, not defaulted to now(): a backtest that
    forgets to pass its historical viewpoint would silently use today's mapping
    (lookahead bias) — the repo's point-in-time hard constraint says that must
    be impossible by construction, not a convention."""
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
    merged = _merge_target(conn, row[0], as_of)
    return merged[0] if merged else row[0]


def crosswalk(conn, entity_id: str, *, as_of: datetime) -> dict[str, list[str]]:
    """All source-local identifiers pointing at an entity, grouped by type:
    {'ticker': ['GOOGL', 'GOOG'], 'cik': ['1652044'], ...}. One hop only — does
    not chase canonical merges. as_of required for the same reason as resolve()."""
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
    """Every (value, entity_id) pair of one identifier type — e.g. all moomoo
    codes in the KG, for a sweep to iterate. Newest vintage per value.
    as_of required for the same reason as resolve()."""
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
