"""Dependency-neutral primitives shared by immutable contract modules."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class CaptureEnvironment(StrEnum):
    # `local` remains only for the pre-Gate-0 bounded adapter. Frozen contracts
    # use the explicit logical tiers below.
    LOCAL = "local"
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"
