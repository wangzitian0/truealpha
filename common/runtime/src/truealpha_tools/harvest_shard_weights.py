"""Turn a CI run's pytest durations into shard weights (#673)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

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


def write_weights(output: Path, run_id: str, root_desc: list[str] | str, seconds: dict[str, float]) -> None:
    output.write_text(
        json.dumps(
            {
                "harvested_from_run": run_id,
                "root": root_desc,
                "files": len(seconds),
                "total_seconds": round(sum(seconds.values()), 2),
                "seconds_by_file": {key: round(value, 3) for key, value in sorted(seconds.items())},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--root", nargs="*", default=None)
    parser.add_argument("--out", default="tools/pytest_shard_weights.json")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    roots = tuple(arguments.root) if arguments.root else None
    seconds = {
        path: value
        for path, value in durations_from_log(fetch_log(arguments.run_id)).items()
        if roots is None or any(path.startswith(r) for r in roots)
    }
    if not seconds:
        prefix_desc = f"under {roots}" if roots else "across any test path"
        raise SystemExit(
            f"harvest: run {arguments.run_id} yielded no durations {prefix_desc} — was it "
            f"run with --durations=0? Refusing to write an empty harvest."
        )

    output = Path(arguments.out)
    write_weights(output, str(arguments.run_id), list(roots) if roots else "all", seconds)
    print(f"harvest: {len(seconds)} files, {sum(seconds.values()):.1f}s total -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
