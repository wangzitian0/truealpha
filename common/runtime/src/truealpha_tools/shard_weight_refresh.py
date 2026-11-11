"""Decide whether the shard weights need a refresh pull request (#1503)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from truealpha_tools.harvest_shard_weights import write_weights


class Decision(NamedTuple):
    open_pr: bool
    reason: str


def merge_weights(old: Mapping[str, float], harvested: Mapping[str, float], repo_root: Path) -> dict[str, float]:
    merged = {**old, **harvested}
    return {key: value for key, value in sorted(merged.items()) if (repo_root / key).is_file()}


def decide(old: Mapping[str, float], merged: Mapping[str, float], open_prs: int) -> Decision:
    if open_prs > 0:
        return Decision(False, f"an open refresh pull request exists ({open_prs}); the job opens at most one")
    gained = sorted(set(merged) - set(old))
    if not gained:
        return Decision(False, "the harvest gives a weight to no existing test file that lacks one")
    return Decision(True, f"the harvest gives weights to {len(gained)} test files that lack one")


def _read_weights(path: Path, *, must_exist: bool) -> tuple[str, dict[str, float]]:
    if not path.is_file():
        if must_exist:
            raise SystemExit(f"shard_weight_refresh: {path} does not exist")
        return "", {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    seconds = raw.get("seconds_by_file")
    if not isinstance(seconds, dict):
        raise SystemExit(f"shard_weight_refresh: {path} has no seconds_by_file mapping")
    return str(raw.get("harvested_from_run", "")), {str(key): float(value) for key, value in seconds.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", type=Path, required=True)
    parser.add_argument("--open-prs", type=int, required=True)
    parser.add_argument("--weights", type=Path, default=Path("tools/pytest_shard_weights.json"))
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    _, old = _read_weights(arguments.weights, must_exist=False)
    run_id, harvested = _read_weights(arguments.harvest, must_exist=True)
    if not harvested:
        raise SystemExit(f"shard_weight_refresh: {arguments.harvest} holds no weights; refusing to decide")

    merged = merge_weights(old, harvested, arguments.repo_root)
    decision = decide(old, merged, arguments.open_prs)
    if decision.open_pr:
        write_weights(arguments.weights, run_id, "all", merged)

    print(f"open_pr={str(decision.open_pr).lower()}")
    print(f"reason={decision.reason}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"open_pr={str(decision.open_pr).lower()}\nreason={decision.reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
