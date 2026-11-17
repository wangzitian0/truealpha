"""Boot-time environment validation against required-env manifest (#759)."""

from __future__ import annotations

from infra2_sdk.runtime.config_schema import assert_environment

__all__ = ["assert_environment"]
