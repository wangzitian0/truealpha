#!/usr/bin/env python3
"""CLI wrapper: run `data_engine.core_strategy_replay.run()` and write reports.

The replay logic itself lives in `data_engine.core_strategy_replay` (a plain
importable module, no Dagster/CLI concerns) so both this script and the
`materialize_core_strategy_replay_preview` Dagster asset
(`data_engine.core_strategy_replay_assets`) call the same `run()` without one
depending on the other. Re-exports the names `apps/data-engine/tests/
test_strategy_smoke.py` already loads off this script module dynamically
(`importlib.util.spec_from_file_location`), so that test needed no changes.

Usage: uv run --package truealpha-data-engine python \
    apps/data-engine/scripts/run_strategy_smoke.py --output-dir <dir>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from data_engine.core_strategy_replay import (
    CORPUS_SHA256,
    REPOSITORY_ROOT,
    STRATEGY_ID,
    Decision,
    _compare_against_golden,
    _load_corpus,
    render_markdown,
    run,
)

__all__ = [
    "CORPUS_SHA256",
    "REPOSITORY_ROOT",
    "STRATEGY_ID",
    "Decision",
    "_compare_against_golden",
    "_load_corpus",
    "render_markdown",
    "run",
    "main",
]

OUTPUT_JSON = "strategy_smoke.json"
OUTPUT_MARKDOWN = "strategy_smoke.md"
# Checked-in, deterministic mirror of a clean run — see #347. This is the
# fixture `truealpha_contracts.strategy_run_fixture.FixtureStrategyRunRepository`
# (and its TypeScript port under apps/app-web/src/contracts) reads at runtime.
# It excludes `generated_at` so its bytes are stable across regenerations, and
# it is only refreshed when this script reproduces the golden fixture exactly.
CANONICAL_FIXTURE_PATH = REPOSITORY_ROOT / "libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    decisions, _ = run()
    corpus = _load_corpus()
    mismatches = _compare_against_golden(decisions, corpus)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / OUTPUT_JSON).write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "corpus_sha256": CORPUS_SHA256,
                "decisions": [d.to_json() for d in decisions],
                "golden_mismatches": mismatches,
            },
            indent=2,
            sort_keys=True,
        )
    )
    (output_dir / OUTPUT_MARKDOWN).write_text(render_markdown(decisions))

    if mismatches:
        print(f"FAILED: {len(mismatches)} decision(s) did not match the golden fixture:")
        for mismatch in mismatches:
            print(f"  - {mismatch}")
        return 1

    CANONICAL_FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANONICAL_FIXTURE_PATH.write_text(
        json.dumps(
            {
                "strategy_id": STRATEGY_ID,
                "source": "strategy_smoke_fixture",
                "corpus_sha256": CORPUS_SHA256,
                "decisions": [d.to_json() for d in decisions],
                "golden_mismatches": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    print(f"OK: all {len(decisions)} decisions reproduced the golden fixture exactly.")
    print(f"Canonical fixture refreshed at {CANONICAL_FIXTURE_PATH.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
