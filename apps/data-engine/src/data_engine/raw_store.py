"""Raw landing per the runtime contract (CLAUDE.md raw-storage hard constraint,
db/migrations/0004_runtime_contracts.sql): immutable source-response bytes go to
S3-compatible object storage, and `raw.fetches` keeps the checksum, object
pointer, timestamps, and lineage. Rows are append-only vintages — a re-fetch
with identical bytes collapses onto the existing row (content-addressed), a
re-fetch with different bytes is a new vintage.

`already_fetched` is the sweep resume check: "a row exists for this
(source, source_record_id)" means "don't spend the call again"; a deliberate
re-pull just skips the check and lands a new vintage.
"""

import json
from datetime import UTC, datetime

from truealpha_contracts import DataSource, RawCapture
from truealpha_runtime import S3RawObjectStore

_store: S3RawObjectStore | None = None


def object_store() -> S3RawObjectStore:
    global _store
    if _store is None:
        _store = S3RawObjectStore()
    return _store


def insert_fetch(
    conn,
    *,
    source: DataSource,
    source_record_id: str,
    body: bytes,
    content_type: str,
    fetched_at: datetime,
    source_published_at: datetime | None = None,
    metadata: dict | None = None,
) -> int:
    """Store the bytes in object storage, record the pointer row, return its id
    (the existing row's id when these exact bytes were already recorded)."""
    envelope = object_store().store(
        RawCapture(
            source=source,
            source_record_id=source_record_id,
            body=body,
            content_type=content_type,
            fetched_at=fetched_at,
            source_published_at=source_published_at,
            metadata=metadata or {},
        )
    )
    # recorded_at is passed explicitly rather than left to its now() default:
    # Postgres's now() is the TRANSACTION start, so in a long-running sweep a
    # fetch made mid-transaction would violate check (recorded_at >= fetched_at).
    # The wall clock at insert time is also simply the truer ingestion stamp.
    row = conn.execute(
        """
        insert into raw.fetches
            (source, source_record_id, payload_sha256, object_uri, content_type,
             byte_length, source_published_at, fetched_at, recorded_at, metadata)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        on conflict (source, source_record_id, payload_sha256) do nothing
        returning id
        """,
        (
            source.value,
            source_record_id,
            envelope.object.sha256,
            envelope.object.uri,
            content_type,
            envelope.object.byte_length,
            source_published_at,
            fetched_at,
            datetime.now(UTC),
            json.dumps(metadata or {}),
        ),
    ).fetchone()
    if row is not None:
        return row[0]
    existing = conn.execute(
        "select id from raw.fetches where source = %s and source_record_id = %s and payload_sha256 = %s",
        (source.value, source_record_id, envelope.object.sha256),
    ).fetchone()
    return existing[0]


def insert_json_fetch(
    conn,
    *,
    source: DataSource,
    source_record_id: str,
    payload,
    fetched_at: datetime,
    source_published_at: datetime | None = None,
    metadata: dict | None = None,
) -> int:
    return insert_fetch(
        conn,
        source=source,
        source_record_id=source_record_id,
        body=json.dumps(payload).encode(),
        content_type="application/json",
        fetched_at=fetched_at,
        source_published_at=source_published_at,
        metadata=metadata,
    )


def already_fetched(conn, *, source: DataSource, source_record_id: str) -> bool:
    return (
        conn.execute(
            "select 1 from raw.fetches where source = %s and source_record_id = %s limit 1",
            (source.value, source_record_id),
        ).fetchone()
        is not None
    )


def raw_ref(fetch_id: int) -> str:
    """The pointer format staging rows use to trace back here (init.md Section 6)."""
    return f"raw.fetches:{fetch_id}"


def get_payload(conn, fetch_id: int) -> bytes:
    """Read a fetch's bytes back through its pointer row — checksum-verified by
    the object store. This is how offline rebuilds (--figi-from-raw) reuse
    landed responses instead of re-spending API calls."""
    from truealpha_contracts import RawObjectRef

    row = conn.execute(
        "select object_uri, payload_sha256, byte_length, content_type from raw.fetches where id = %s",
        (fetch_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"raw.fetches:{fetch_id} does not exist")
    object_uri, sha256, byte_length, content_type = row
    bucket, _, key = object_uri.removeprefix("s3://").partition("/")
    ref = RawObjectRef(bucket=bucket, key=key, sha256=sha256, byte_length=byte_length, content_type=content_type)
    return object_store().get(ref)
