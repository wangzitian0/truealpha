"""Canonical FetchKey (#1411 K3).

One physical external resource has one canonical ``FetchKey``:
``(seat, endpoint, canonical_entity_id, as_of)``. The source gateway reads the persisted
index of landed keys (`sources.landed_keys`) before it makes a vendor or SEC call. A key
that landed is served from stored bytes with no external call (`gateway.fetch_once`).

Each ``as_of`` names the axis on which the resource can change: a settled session for a
price, an accession for a filing, a UTC day for a vendor document without a version.
One fetch of a key makes exactly one external request. A caller that needs two requests
uses two keys, because the ledger counts requests per key.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Separator of the ledger form. A key part holds none (``__post_init__`` refuses it).
KEY_SEPARATOR = "|"


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
            if KEY_SEPARATOR in value:
                raise ValueError(f"FetchKey.{field_name} must not contain {KEY_SEPARATOR!r}, got {value!r}")

    @property
    def source_record_id(self) -> str:
        """The record id under which the landed bytes sit in ``raw.fetches``."""
        return f"{self.endpoint}:{self.canonical_entity_id}:{self.as_of}"

    @property
    def ledger_key(self) -> str:
        """The form written to ``staging.api_call_ledger.fetch_key`` and named by the verdict."""
        return KEY_SEPARATOR.join((self.seat, self.endpoint, self.canonical_entity_id, self.as_of))
