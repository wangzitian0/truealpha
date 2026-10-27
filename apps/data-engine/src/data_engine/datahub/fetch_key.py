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

import hashlib
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
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

    @classmethod
    def from_ledger_key(cls, ledger_key: str) -> FetchKey:
        """The key that `ledger_key` renders. A part holds no separator, so the split is exact."""
        seat, endpoint, canonical_entity_id, as_of = ledger_key.split(KEY_SEPARATOR)
        return cls(seat, endpoint, canonical_entity_id, as_of)

    @classmethod
    def for_batch(cls, seat: str, endpoint: str, members: Iterable[str], as_of: str) -> FetchKey:
        """The key of one request that names a set of members (ISINs, tickers) in its body.

        The entity is ``batch:<sha256 of the sorted members>``: the same set is the same resource,
        whatever order it was asked in."""
        digest = hashlib.sha256("\n".join(sorted(members)).encode()).hexdigest()
        return cls(seat, endpoint, f"batch:{digest}", as_of)

    @classmethod
    def for_request(
        cls,
        seat: str,
        endpoint: str,
        params: Mapping[str, str],
        *,
        entity_param: str,
        as_of_params: Collection[str],
        default_as_of: str,
        exclude: Collection[str] = (),
    ) -> FetchKey:
        """The key of a vendor request that its query parameters describe in full.

        The entity is ``symbol:<value>`` of ``entity_param``. The as-of is the first of
        ``as_of_params`` that the request carries, else ``default_as_of``. Every other parameter
        stays in the endpoint label, in sorted order, so two requests that differ in window,
        interval or adjustment never share a key. ``exclude`` names the credentials."""
        remaining = {
            name: value
            for name, value in params.items()
            if name != entity_param and name not in exclude and name not in as_of_params
        }
        as_of = next((str(params[name]) for name in as_of_params if name in params), default_as_of)
        label = endpoint if not remaining else f"{endpoint}?{urllib.parse.urlencode(sorted(remaining.items()))}"
        return cls(seat, label, f"symbol:{params[entity_param]}", as_of)
