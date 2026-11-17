"""Canonical FetchKey for cached external fetches."""

from __future__ import annotations

import hashlib
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

KEY_SEPARATOR = "|"


@dataclass(frozen=True)
class FetchKey:
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
        return f"{self.endpoint}:{self.canonical_entity_id}:{self.as_of}"

    @property
    def ledger_key(self) -> str:
        return KEY_SEPARATOR.join((self.seat, self.endpoint, self.canonical_entity_id, self.as_of))

    @classmethod
    def from_ledger_key(cls, ledger_key: str) -> FetchKey:
        seat, endpoint, canonical_entity_id, as_of = ledger_key.split(KEY_SEPARATOR)
        return cls(seat, endpoint, canonical_entity_id, as_of)

    @classmethod
    def for_batch(cls, seat: str, endpoint: str, members: Iterable[str], as_of: str) -> FetchKey:
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
        remaining = {
            name: value
            for name, value in params.items()
            if name != entity_param and name not in exclude and name not in as_of_params
        }
        as_of = next((str(params[name]) for name in as_of_params if name in params), default_as_of)
        label = endpoint if not remaining else f"{endpoint}?{urllib.parse.urlencode(sorted(remaining.items()))}"
        return cls(seat, label, f"symbol:{params[entity_param]}", as_of)
