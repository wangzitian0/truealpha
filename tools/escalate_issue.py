#!/usr/bin/env python3
# ruff: noqa: I001, E402, E701
# fmt: off
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common/runtime/src"))
from truealpha_tools.escalate_issue import main
if __name__ == "__main__": raise SystemExit(main())
