#!/usr/bin/env python3
"""Thin CLI shim for env_manifest; implementation lives in truealpha_tools.env_manifest."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "common" / "runtime" / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from truealpha_tools import env_manifest as _impl  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_impl.main())
sys.modules[__name__] = _impl
