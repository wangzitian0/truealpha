"""Turn a CI run's pytest durations into shard weights — A4 B1 (#673).

Two weight proxies were refuted by measurement before this existed: packing by
file position gave three lanes of 57 / 107 / 137 s, and packing by test count
gave 74 / 155 / 84 s while the counts were 177 / 177 / 176. A Dagster
materialisation test costs 9 s and a pure-function test costs a millisecond,
so nothing derivable from the source predicts the time.

This reads the `--durations=0` output of a completed ci-required run, sums
every phase per FILE (setup and teardown belong to the file's cost as much as
call does), and writes `tools/pytest_shard_weights.json`.

The harvest records the run it came from — which dates it, since a run id is
resolvable to its timestamp. That is not decoration:
weights go stale silently — the lanes stay green and merely drift back out of
balance — so `libs/runtime/tests/test_pytest_shard.py` asserts the harvest
still covers most of the tree, which is the observable symptom of a harvest
nobody refreshed.

Separate from `pytest_shard.py` on purpose, not by oversight: that tool runs in
every shard lane and is stdlib-only by design, while this one shells out to
`gh` and needs the network. Folding an occasional maintenance command into the
hot path would put a network dependency inside the job that decides which
tests run.

Usage:
    python tools/harvest_shard_weights.py <run-id> [--root apps/data-engine/tests]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

# `0.42s call     apps/data-engine/tests/test_x.py::test_y` — the phase is kept
# out of the key deliberately: a file's cost is all of its phases, and pytest
# reports setup/call/teardown separately.
# No `^` anchor: `gh run view --log` prefixes every line with the job name, the
# step name and a timestamp, so an anchored pattern matches nothing and the
# harvest comes back empty. That is not hypothetical — it is what the first
# version did, and the fail-closed check below is what caught it instead of
# writing an empty file over a good one.
#
# The phase is matched but not kept: a file's cost is setup + call + teardown,
# and pytest reports them separately.
DURATION = re.compile(r"\b([0-9]+\.[0-9]+)s\s+(?:call|setup|teardown)\s+(\S+?\.py)::")


def durations_from_log(text: str) -> dict[str, float]:
    seconds: dict[str, float] = defaultdict(float)
    for value, path in DURATION.findall(text):
        seconds[path] += float(value)
    return dict(seconds)


def fetch_log(run_id: str) -> str:
    result = subprocess.run(["gh", "run", "view", str(run_id), "--log"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"harvest: gh run view {run_id} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--root", default="apps/data-engine/tests")
    parser.add_argument("--out", default="tools/pytest_shard_weights.json")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    seconds = {
        path: value
        for path, value in durations_from_log(fetch_log(arguments.run_id)).items()
        if path.startswith(arguments.root)
    }
    if not seconds:
        # Fail closed: an empty harvest written over a good one silently
        # reverts every lane to the count-based estimate, and nothing would
        # say so (#527's green-while-empty).
        raise SystemExit(
            f"harvest: run {arguments.run_id} yielded no durations under {arguments.root} — was it "
            f"run with --durations=0? Refusing to write an empty harvest."
        )

    output = Path(arguments.out)
    output.write_text(
        json.dumps(
            {
                "harvested_from_run": str(arguments.run_id),
                "root": arguments.root,
                "files": len(seconds),
                "total_seconds": round(sum(seconds.values()), 2),
                "seconds_by_file": {key: round(value, 3) for key, value in sorted(seconds.items())},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"harvest: {len(seconds)} files, {sum(seconds.values()):.1f}s total -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
