"""Canonical FetchKey and LandedKeyIndex cache-aside (#1411 K3).

One physical external resource is identified by a canonical ``FetchKey``:
``(seat, endpoint, canonical_entity_id, as_of)``. Before issuing a vendor HTTP/SDK
call, a caller or adapter checks the ``LandedKeyIndex`` (or process/session cache).
When the key has already landed for the current ``as_of`` axis, the landed payload is
returned with zero external calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class FetchKey:
    """Canonical identity of one external vendor fetch (#1411 K3)."""

    seat: str
    endpoint: str
    canonical_entity_id: str
    as_of: str

    def __post_init__(self) -> None:
        for field_name in ("seat", "endpoint", "canonical_entity_id", "as_of"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"FetchKey.{field_name} must be a non-empty string, got {value!r}")

    @property
    def source_record_id(self) -> str:
        return f"{self.endpoint}:{self.canonical_entity_id}:{self.as_of}"


@dataclass
class LandedKeyIndex:
    """Cache-aside index for canonical ``FetchKey`` lookups within a run or across
    ``raw.fetches`` (#1411 K3).
    """

    connection: Any = None
    _entries: dict[FetchKey, Any] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get_or_fetch(self, key: FetchKey, fetcher: Callable[[], T]) -> T:
        """Return the landed value for ``key`` if present, otherwise invoke ``fetcher()`` once."""
        if key in self._entries:
            self.hits += 1
            return self._entries[key]
        if self.connection is not None:
            db_hit = self._lookup_raw_fetch(key)
            if db_hit is not None:
                self.hits += 1
                self._entries[key] = db_hit
                return db_hit  # type: ignore[return-value]
        self.misses += 1
        result = fetcher()
        self._entries[key] = result
        return result

    def put(self, key: FetchKey, value: Any) -> None:
        """Record a landed value under ``key``."""
        self._entries[key] = value

    def contains(self, key: FetchKey) -> bool:
        if key in self._entries:
            return True
        if self.connection is not None:
            return self._lookup_raw_fetch(key) is not None
        return False

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def _lookup_raw_fetch(self, key: FetchKey) -> bytes | None:
        try:
            from data_engine import raw_store

            row = self.connection.execute(
                """
                select id
                from raw.fetches
                where source = %s and source_record_id in (%s, %s)
                order by fetched_at desc
                limit 1
                """,
                (key.seat, key.source_record_id, f"{key.endpoint}:{key.canonical_entity_id}"),
            ).fetchone()
            if row is None:
                return None
            return raw_store.get_payload(self.connection, int(row[0]))
        except Exception:  # noqa: BLE001 - fall back to in-memory cache when raw.fetches or S3 is unavailable
            return None


DEFAULT_LANDED_KEY_INDEX = LandedKeyIndex()
