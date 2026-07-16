"""Run a frozen D5 replay without network or credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from data_engine.datahub.medium_replay import run_topt_medium_replay
from data_engine.datahub.tiny_replay import run_tiny_replay


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--replay", choices=("tiny", "medium"), default="tiny")
    args = parser.parse_args()

    corpus_bytes = args.corpus.read_bytes()
    actual_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    if actual_sha256 != args.expected_sha256:
        raise SystemExit("frozen corpus SHA-256 mismatch")
    corpus = json.loads(corpus_bytes)
    report = run_tiny_replay(corpus) if args.replay == "tiny" else run_topt_medium_replay(corpus)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
