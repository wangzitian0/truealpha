"""Dependency-neutral primitives shared by immutable contract modules."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

STABLE_ID_BODY = r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*"
STABLE_ID_PATTERN = rf"^{STABLE_ID_BODY}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
DEFAULT_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def reject_binary_float(value: Any, label: str = "monetary or ratio value") -> Any:
    if isinstance(value, float):
        raise ValueError(f"{label} cannot be a binary float")
    return value


def finite_decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    if value is not None:
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
        if not parsed.is_finite():
            raise ValueError("non-finite Decimal values are forbidden")
    return value


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def require_stable_and_immutable(
    value: str,
    field_name: str,
    *,
    mutable_tokens: frozenset[str] = DEFAULT_MUTABLE_TOKENS,
) -> str:
    if re.fullmatch(STABLE_ID_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be a stable coordinate")
    tokens = {token for token in re.split(r"[._:/@+\-]", value.lower()) if token}
    if tokens & mutable_tokens:
        raise ValueError(f"{field_name} must name an immutable version")
    return value


def sorted_unique_coordinates(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = True,
    immutable: bool = False,
    mutable_tokens: frozenset[str] = DEFAULT_MUTABLE_TOKENS,
) -> tuple[str, ...]:
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    for val in values:
        if immutable:
            require_stable_and_immutable(val, field_name, mutable_tokens=mutable_tokens)
        elif re.fullmatch(STABLE_ID_PATTERN, val) is None:
            raise ValueError(f"{field_name} must contain stable coordinates")
    return tuple(sorted(values))


def drop_none_fields(data: dict[str, Any], *fields: str) -> dict[str, Any]:
    for name in fields:
        if data.get(name) is None:
            data.pop(name, None)
    return data


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def identify(model: BaseModel, *, id_field: str, prefix: str, hash_field: str = "content_sha256") -> None:
    payload = model.model_dump(mode="json", exclude={id_field, hash_field})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    if getattr(model, hash_field) not in ("", expected_hash):
        raise ValueError(f"{hash_field} does not match canonical content")
    if getattr(model, id_field) not in ("", expected_id):
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, expected_hash)
    object.__setattr__(model, id_field, expected_id)


class CaptureEnvironment(StrEnum):
    LOCAL = "local"
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


def identify_by_grain(model: BaseModel, *, id_field: str, prefix: str, identity_fields: tuple[str, ...]) -> None:
    identity = model.model_dump(mode="json", include=set(identity_fields))
    expected_id = f"{prefix}:{canonical_sha256({'kind': prefix, 'identity': identity})}"
    expected_hash = canonical_sha256(model.model_dump(mode="json", exclude={id_field, "content_sha256"}))
    if getattr(model, id_field) not in ("", expected_id):
        raise ValueError(f"{id_field} does not match its declared identity grain")
    if getattr(model, "content_sha256") not in ("", expected_hash):
        raise ValueError("content_sha256 does not match the canonical record")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", expected_hash)
