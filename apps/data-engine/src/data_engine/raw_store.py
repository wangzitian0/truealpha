"""Object landing and verification for `raw.fetches`."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from truealpha_contracts.models import DataSource, RawCapture, RawObjectRef
from truealpha_contracts.ports import RawObjectStore
from truealpha_runtime import S3RawObjectStore

_store: S3RawObjectStore | None = None


def object_store() -> S3RawObjectStore:
    global _store
    if _store is None:
        _store = S3RawObjectStore()
    return _store


def parse_s3_uri(object_uri: str) -> tuple[str, str]:
    bucket, _, key = object_uri.removeprefix("s3://").partition("/")
    return bucket, key


def get_uri_payload(
    object_uri: str,
    sha256: str,
    byte_length: int | None,
    content_type: str | None,
    *,
    store: RawObjectStore | None = None,
) -> bytes:
    bucket, key = parse_s3_uri(object_uri)
    ref = RawObjectRef(
        bucket=bucket,
        key=key,
        sha256=sha256,
        byte_length=int(byte_length or 0),
        content_type=content_type or "application/octet-stream",
    )
    return (store or object_store()).get(ref)


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
    store: RawObjectStore | None = None,
    recorded_at: datetime | None = None,
) -> int:
    capture = RawCapture(
        source=source,
        source_record_id=source_record_id,
        body=body,
        content_type=content_type,
        fetched_at=fetched_at,
        source_published_at=source_published_at,
        metadata=metadata or {},
    )
    landing_time = recorded_at or datetime.now(UTC)
    if landing_time.tzinfo is None or landing_time.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    if landing_time < fetched_at:
        raise ValueError("recorded_at must not precede fetched_at")
    envelope = (store or object_store()).store(capture)
    row = conn.execute(
        "insert into raw.fetches (source, source_record_id, payload_sha256, object_uri, content_type, "
        "byte_length, source_published_at, fetched_at, recorded_at, metadata) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "on conflict (source, source_record_id, payload_sha256) do nothing returning id",
        (
            source.value,
            source_record_id,
            envelope.object.sha256,
            envelope.object.uri,
            content_type,
            envelope.object.byte_length,
            source_published_at,
            fetched_at,
            landing_time,
            json.dumps(metadata or {}),
        ),
    ).fetchone()
    if row is not None:
        return row[0]
    return conn.execute(
        "select id from raw.fetches where source = %s and source_record_id = %s and payload_sha256 = %s",
        (source.value, source_record_id, envelope.object.sha256),
    ).fetchone()[0]


def insert_json_fetch(
    conn,
    *,
    source: DataSource,
    source_record_id: str,
    payload,
    fetched_at: datetime,
    source_published_at: datetime | None = None,
    metadata: dict | None = None,
    store: RawObjectStore | None = None,
    recorded_at: datetime | None = None,
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
        store=store,
        recorded_at=recorded_at,
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
    """Format a `raw.fetches:<id>` lineage reference."""
    return f"raw.fetches:{fetch_id}"


def get_payload(conn, fetch_id: int, *, store: RawObjectStore | None = None) -> bytes:
    """Load checksum-verified bytes for `fetch_id` from object storage."""
    row = conn.execute(
        "select object_uri, payload_sha256, byte_length, content_type from raw.fetches where id = %s", (fetch_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"raw.fetches:{fetch_id} does not exist")
    return get_uri_payload(*row, store=store)
