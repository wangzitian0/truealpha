"""Thin wrapper: the recorder and the validator live in the package so they ship inside the image.

Usage (in a checkout; inside the staging code server use `python -m data_engine.datahub.sentinel_corpus`):
    uv run --package truealpha-data-engine python apps/data-engine/scripts/record_sentinel_corpus.py check <dir>
"""

from data_engine.datahub.sentinel_corpus import main

if __name__ == "__main__":
    raise SystemExit(main())
