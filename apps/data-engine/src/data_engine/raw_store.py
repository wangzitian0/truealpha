"""Append-only access to raw.fetches — the landing zone every ingestion script
writes through (db/migrations/0004). Rows are immutable vintages: a re-fetch is a
new row, never an update. `already_fetched` is the sweep resume check — "a
successful row exists" means "don't spend the call again"; a deliberate re-pull
(new vintage) just skips the check."""

import json


def insert_fetch(
    conn,
    *,
    source: str,
    endpoint: str,
    entity_key: str,
    payload: dict | list | None = None,
    content: str | None = None,
    params: dict | None = None,
) -> int:
    """Store one fetched payload; returns the row id for raw_ref pointers."""
    row = conn.execute(
        """
        insert into raw.fetches (source, endpoint, entity_key, params, payload, content)
        values (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
        returning id
        """,
        (
            source,
            endpoint,
            entity_key,
            json.dumps(params) if params is not None else None,
            json.dumps(payload) if payload is not None else None,
            content,
        ),
    ).fetchone()
    return row[0]


def already_fetched(conn, *, source: str, endpoint: str, entity_key: str) -> bool:
    return (
        conn.execute(
            "select 1 from raw.fetches where source = %s and endpoint = %s and entity_key = %s limit 1",
            (source, endpoint, entity_key),
        ).fetchone()
        is not None
    )


def raw_ref(fetch_id: int) -> str:
    """The pointer format staging rows use to trace back here (init.md Section 6)."""
    return f"raw.fetches:{fetch_id}"
