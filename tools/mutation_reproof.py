"""Break each declared property and require its guard to notice.

#582. `AGENTS.md` rule 7 asks for an acceptance criterion to be shown red once.
That proves the assertion worked on the day it was written and says nothing
about six months later — `assertFunnelSaysSomething` was red-proven and still
used `node.parentElement`, which a later `<header>` wrapper would have made pass
vacuously. Six defects of that shape shipped or nearly shipped in the 2026-08
work, each an assertion that looked correct and checked nothing, and every
file-reading test passed on all six.

So: apply a declared edit, run the guard, require a NON-ZERO exit, restore. A
guard whose mutation stops failing has gone inert, and naming it is the only
output this exists to produce.

The manifest is data, deliberately: adding a guard without adding its mutation
shows up as a missing entry in review, which a convention would not.

Usage:
  python tools/mutation_reproof.py
  python tools/mutation_reproof.py --only source-contracts/claim-ceiling-unconditional

Exit codes:
  0 - every declared mutation was caught
  1 - a guard did not notice its own property being broken
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("mutations.json")
# Per mutation, not per job. Without it a single hanging guard exhausts the
# workflow's whole budget, nothing says which one, and every guard after it goes
# unproven — the job that reports inertness, inert (review). Generous: the
# slowest declared command is a bun test run measured in seconds.
TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Mutation:
    id: str
    guard: str
    file: str
    find: str
    replace: str
    command: tuple[str, ...]
    cwd: str


def load(path: Path = MANIFEST) -> list[Mutation]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Mutation(
            id=entry["id"],
            guard=entry["guard"],
            file=entry["file"],
            find=entry["find"],
            replace=entry["replace"],
            command=tuple(entry["command"]),
            cwd=entry["cwd"],
        )
        for entry in raw["mutations"]
    ]


def apply_and_run(mutation: Mutation) -> tuple[bool, str]:
    """Returns (the guard noticed, why not)."""
    target = REPO_ROOT / mutation.file
    original = target.read_text(encoding="utf-8")
    matches = original.count(mutation.find)
    if matches == 0:
        # The anchor is gone, so this mutation no longer describes the code. That
        # is a finding, not a skip: the manifest and the source have drifted and
        # nobody would have known.
        return False, f"anchor not found in {mutation.file} — the mutation no longer applies"
    if matches > 1:
        # First-occurrence replacement on an ambiguous anchor breaks the WRONG
        # occurrence and blames the guard: the 2026-08-24 maiden run reported the
        # walk-skip guard inert when a new step had introduced a second identical
        # `if:` line ahead of the one the mutation meant. Ambiguity is its own
        # verdict, named at the cause.
        return False, f"anchor matches {matches} times in {mutation.file} — pin it to a unique context"
    target.write_text(original.replace(mutation.find, mutation.replace, 1), encoding="utf-8")
    try:
        result = subprocess.run(
            list(mutation.command),
            cwd=REPO_ROOT / mutation.cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Not caught and not passed: it never answered. Reported as a finding
        # and the run continues, so one hanging guard cannot hide the state of
        # every guard after it.
        return False, (
            f"the guard did not finish within {TIMEOUT_SECONDS}s — it neither passed nor "
            f"failed, so the property is unproven"
        )
    finally:
        target.write_text(original, encoding="utf-8")
    if result.returncode != 0:
        return True, ""
    return False, "the guard passed with the property broken"


def run(only: str = "", mutations: Sequence[Mutation] | None = None) -> int:
    declared = list(mutations if mutations is not None else load())
    if only:
        declared = [mutation for mutation in declared if mutation.id == only]
        if not declared:
            print(f"no mutation named {only!r}", file=sys.stderr)
            return 1

    inert: list[str] = []
    for mutation in declared:
        caught, why = apply_and_run(mutation)
        if caught:
            print(f"  caught   {mutation.id}")
        else:
            print(f"  INERT    {mutation.id}: {why}")
            inert.append(f"{mutation.id} ({mutation.guard}): {why}")

    if inert:
        print("", file=sys.stderr)
        for entry in inert:
            print(f"guard is inert: {entry}", file=sys.stderr)
        print(
            f"\n{len(inert)} of {len(declared)} declared properties can be broken without any "
            f"guard noticing. Each was red once; each is now decoration.",
            file=sys.stderr,
        )
        return 1
    print(f"\nall {len(declared)} declared properties are still guarded")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(_parser().parse_args(argv).only)


if __name__ == "__main__":
    raise SystemExit(main())
