#!/usr/bin/env python3
"""CLI runner for `data_engine.core_strategy_replay.run()`."""

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
# Deterministic golden snapshot excluding generated_at (#347).
CANONICAL_FIXTURE_PATH = REPOSITORY_ROOT / "common/contracts/tests/fixtures/strategy_run_preview.v1.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    decisions, _ = run()
    corpus = _load_corpus()
    mismatches = _compare_against_golden(decisions, corpus)

    base: dict[str, object] = {
        "corpus_sha256": CORPUS_SHA256,
        "decisions": [d.to_json() for d in decisions],
    }

    def _dump(path: Path, payload: dict[str, object], suffix: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + suffix)

    output_dir = args.output_dir
    _dump(
        output_dir / OUTPUT_JSON,
        {**base, "generated_at": datetime.now(UTC).isoformat(), "golden_mismatches": mismatches},
    )
    (output_dir / OUTPUT_MARKDOWN).write_text(render_markdown(decisions))

    if mismatches:
        print(f"FAILED: {len(mismatches)} decision(s) did not match the golden fixture:")
        for mismatch in mismatches:
            print(f"  - {mismatch}")
        return 1

    _dump(
        CANONICAL_FIXTURE_PATH,
        {**base, "strategy_id": STRATEGY_ID, "source": "strategy_smoke_fixture", "golden_mismatches": []},
        "\n",
    )

    print(f"OK: all {len(decisions)} decisions reproduced the golden fixture exactly.")
    print(f"Canonical fixture refreshed at {CANONICAL_FIXTURE_PATH.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
