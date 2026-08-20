"""Thin wrapper: the oracles live in the package so they ship inside the image.

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/canary_assert.py [--run-id ...]
"""

from data_engine.datahub.canary_oracles import main

if __name__ == "__main__":
    raise SystemExit(main())
