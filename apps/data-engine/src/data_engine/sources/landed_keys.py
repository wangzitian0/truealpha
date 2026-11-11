"""Persisted and in-memory landed fetch-key index (`staging.landed_fetch_keys`, #1411 K3)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from data_engine.datahub.fetch_key import FetchKey

log = logging.getLogger(__name__)


class LandedKeys(Protocol):
    def lookup(self, key: FetchKey) -> bytes | None: ...
    def land(
        self, key: FetchKey, body: bytes, *, content_type: str, fetched_at: datetime, run_key: str | None
    ) -> None: ...


class MemoryLandedKeys:
    def __init__(self) -> None:
        self.bodies: dict[FetchKey, bytes] = {}
        self.reuses: dict[FetchKey, int] = {}

    def lookup(self, key: FetchKey) -> bytes | None:
        body = self.bodies.get(key)
        if body is not None:
            self.reuses[key] = self.reuses.get(key, 0) + 1
        return body

    def land(self, key: FetchKey, body: bytes, *, content_type: str, fetched_at: datetime, run_key: str | None) -> None:
        self.bodies[key] = body


_LOOKUP_SQL = """
select raw_fetch_id
from staging.landed_fetch_keys
where seat = %s and endpoint = %s and canonical_entity_id = %s and as_of = %s
"""

_REUSE_SQL = """
update staging.landed_fetch_keys
set reuse_count = reuse_count + 1, last_reused_at = clock_timestamp()
where seat = %s and endpoint = %s and canonical_entity_id = %s and as_of = %s
"""

_LAND_SQL = """
insert into staging.landed_fetch_keys
    (seat, endpoint, canonical_entity_id, as_of, raw_fetch_id, payload_sha256, landed_run_key)
select %s, %s, %s, %s, f.id, f.payload_sha256, %s
from raw.fetches as f
where f.id = %s
on conflict (seat, endpoint, canonical_entity_id, as_of) do update
set raw_fetch_id = excluded.raw_fetch_id,
    payload_sha256 = excluded.payload_sha256,
    landed_at = clock_timestamp(),
    landed_run_key = excluded.landed_run_key
"""


class PostgresLandedKeys:
    def __init__(self, connect: Callable[[], Any], reset: Callable[[], None], *, store: Any | None = None) -> None:
        self._connect = connect
        self._reset = reset
        self._store = store

    def _attempt(self, work: Callable[[Any], Any]) -> Any:
        import psycopg

        try:
            return work(self._connect())
        except psycopg.Error:
            self._reset()
            return work(self._connect())

    def lookup(self, key: FetchKey) -> bytes | None:
        from data_engine import raw_store

        params = (key.seat, key.endpoint, key.canonical_entity_id, key.as_of)

        def read(connection: Any) -> bytes | None:
            row = connection.execute(_LOOKUP_SQL, params).fetchone()
            if row is None:
                return None
            try:
                body = raw_store.get_payload(connection, int(row[0]), store=self._store)
            except Exception:  # noqa: BLE001
                log.exception("landed key %s points at unreadable bytes; it will be fetched again", key.ledger_key)
                return None
            connection.execute(_REUSE_SQL, params)
            return body

        return self._attempt(read)

    def land(self, key: FetchKey, body: bytes, *, content_type: str, fetched_at: datetime, run_key: str | None) -> None:
        from truealpha_contracts.models import DataSource

        from data_engine import raw_store

        source = DataSource(key.seat)

        def write(connection: Any) -> None:
            fetch_id = raw_store.insert_fetch(
                connection,
                source=source,
                source_record_id=key.source_record_id,
                body=body,
                content_type=content_type,
                fetched_at=fetched_at,
                metadata={"fetch_key": key.ledger_key},
                store=self._store,
            )
            connection.execute(
                _LAND_SQL, (key.seat, key.endpoint, key.canonical_entity_id, key.as_of, run_key, fetch_id)
            )

        self._attempt(write)
