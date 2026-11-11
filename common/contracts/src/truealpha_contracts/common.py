"""Dependency-neutral primitives shared by immutable contract modules."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

STABLE_ID_BODY = r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*"
STABLE_ID_PATTERN = rf"^{STABLE_ID_BODY}$"


def require_stable_and_immutable(value: str, field_name: str, *, mutable_tokens: frozenset[str]) -> str:
    if re.fullmatch(STABLE_ID_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be a stable coordinate")
    tokens = {token for token in re.split(r"[._:/@+\-]", value.lower()) if token}
    if tokens & mutable_tokens:
        raise ValueError(f"{field_name} must name an immutable version")
    return value


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
