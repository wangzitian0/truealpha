#!/usr/bin/env python3
"""Thin CLI shim delegating to ``truealpha_tools.harvest_shard_weights`` (#1340)."""

from __future__ import annotations

import sys
from pathlib import Path

_RUNTIME_SRC = Path(__file__).resolve().parents[1] / "common" / "runtime" / "src"
if str(_RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_SRC))

from truealpha_tools.harvest_shard_weights import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
