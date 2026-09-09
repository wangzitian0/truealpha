#!/usr/bin/env python3
"""init.md §1 rule 2, armed: the App layer must not compute a metric across rows (#727).

Rule 2 confines `apps/app-web` to deterministic reformatting — "simple within-row
arithmetic that doesn't span tables"; anything that "jointly computes a new metric across
factors or time points must go back into `libs/factors` and be materialized into mart".
init.md §10 recorded that no code-level check enforced this, and #723 is what that costs:
`fund-valuation.ts` grew a window-function weighted mean that contradicted its own file
header, and nothing went red. This is that check.

Scope: the SQL the App's mart readers send. An aggregate in a *database view* is fine —
that is the database's own read model, not the App layer — so this only reads
`apps/app-web/src/server/mart/*.ts`.

Allow-list: a reader that genuinely needs an aggregate names itself in ALLOWED with the
issue that decided it. Adding a name is the reviewable act; the default is refusal.

Stdlib-only, like tools/check_route_manifest.py, so it runs before any toolchain setup.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MART_READERS = REPO_ROOT / "apps" / "app-web" / "src" / "server" / "mart"

#: Aggregation the App may not perform. `count(` is deliberately absent: counting rows a
#: reader already holds is presentation, not a new metric.
FORBIDDEN = (
    re.compile(r"\bsum\s*\(", re.IGNORECASE),
    re.compile(r"\bavg\s*\(", re.IGNORECASE),
    re.compile(r"\bover\s*\(\s*partition\b", re.IGNORECASE),
)

#: file name -> the issue that authorized the aggregate. Empty on purpose: every
#: fund-level aggregate moved to `factors.base.etf_virtual_company` in #727.
ALLOWED: dict[str, str] = {}


def offenders(root: Path = MART_READERS) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.glob("*.ts")):
        if path.name in ALLOWED:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            # A comment explaining the rule is not a violation of it.
            stripped = line.strip()
            if stripped.startswith(("*", "//", "/*")):
                continue
            if any(pattern.search(line) for pattern in FORBIDDEN):
                found.append((path, number, stripped))
    return found


def main() -> int:
    if not MART_READERS.is_dir():
        print(f"no mart reader directory at {MART_READERS}", file=sys.stderr)
        return 1
    found = offenders()
    if not found:
        print(f"app-layer aggregation check: {MART_READERS.relative_to(REPO_ROOT)}/*.ts compute no cross-row metric")
        return 0
    for path, number, line in found:
        location = f"{path.relative_to(REPO_ROOT)}:{number}"
        print(f"::error file={path.relative_to(REPO_ROOT)},line={number}::{location}: {line}", file=sys.stderr)
    print(
        f"\n{len(found)} aggregate(s) in the App's mart readers (init.md §1 rule 2, #727).\n"
        "A metric computed across rows belongs in libs/factors and is materialized into mart;\n"
        "the reader then reads a column. If this aggregate is genuinely presentation, add the\n"
        "file to ALLOWED in this script with the issue that decided it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
