"""The persisted index of landed fetch keys (#1411 K3).

``staging.landed_fetch_keys`` holds one row per `FetchKey` that landed. The bytes sit in
``raw.fetches`` and object storage, so a key that landed is served with no external call, in
the same process, in a later process and in a later run. `sources.gateway` reads this index
before every keyed vendor or SEC call (`gateway.fetch_once`).

Two implementations share one shape (``lookup``, ``land``):

- `PostgresLandedKeys` is the deployed index. It writes on the gateway's autocommit connection,
  so a rolled-back tick keeps the keys its calls landed (the ledger rows survive the same way).
- `MemoryLandedKeys` is the test double. It keeps the same contract in a dict.

An index never decides whether a caller may fetch. A lookup that fails reads as a miss, and the
failure is logged by the gateway. A miss costs one external call, which the ledger records.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from data_engine.datahub.fetch_key import FetchKey

log = logging.getLogger(__name__)


class LandedKeys(Protocol):
    def lookup(self, key: FetchKey) -> bytes | None:
        """The bytes landed under ``key``, or None. A hit counts as one reuse."""
        ...

    def land(self, key: FetchKey, body: bytes, *, content_type: str, fetched_at: datetime, run_key: str | None) -> None:
        """Record ``body`` as the landing of ``key``."""
        ...


class MemoryLandedKeys:
    """The in-memory index of the tests. It counts reuses per key like the table does."""

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
    """The deployed index over ``staging.landed_fetch_keys`` and ``raw.fetches``.

    ``connect`` returns an autocommit connection; ``reset`` drops it after a database error so
    the next call opens a fresh one. The gateway passes its ledger connection, so one process
    holds one warehouse connection for both."""

    def __init__(self, connect: Callable[[], Any], reset: Callable[[], None], *, store: Any | None = None) -> None:
        self._connect = connect
        self._reset = reset
        self._store = store

    def _attempt(self, work: Callable[[Any], Any]) -> Any:
        import psycopg

        try:
            return work(self._connect())
        except psycopg.Error:
            # A connection that dropped silently is marked closed only after a statement fails on it.
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
            except Exception:  # noqa: BLE001 - lost or corrupt bytes read as a miss; the refetch lands them again
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
