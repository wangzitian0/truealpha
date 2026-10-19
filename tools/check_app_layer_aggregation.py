#!/usr/bin/env python3
"""init.md §1 rule 2, armed: the App layer must not compute a metric across rows (#727, #1272).

Rule 2 confines `apps/app-web` to deterministic reformatting — "simple within-row
arithmetic that doesn't span tables"; anything that "jointly computes a new metric across
factors or time points must go back into `common/factors` and be materialized into mart".
init.md §10 recorded that no code-level check enforced this, and #723 is what that costs:
`fund-valuation.ts` grew a window-function weighted mean that contradicted its own file
header, and nothing went red. This is that check.

Scope: `apps/app-web/src/server/mart`, `apps/app-web/src/app/research`, and
`apps/app-web/src/components`. An aggregate in a *database view* is fine —
that is the database's own read model, not the App layer.

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
RESEARCH_ROUTES = REPO_ROOT / "apps" / "app-web" / "src" / "app" / "research"
COMPONENTS = REPO_ROOT / "apps" / "app-web" / "src" / "components"
SCAN_ROOTS = (MART_READERS, RESEARCH_ROUTES, COMPONENTS)

#: Aggregation the App may not perform. `count(` is deliberately absent: counting rows a
#: reader already holds is presentation, not a new metric.
FORBIDDEN = (
    re.compile(r"\bsum\s*\(", re.IGNORECASE),
    re.compile(r"\bavg\s*\(", re.IGNORECASE),
    re.compile(r"\bstddev\s*\(", re.IGNORECASE),
    re.compile(r"\bvariance\s*\(", re.IGNORECASE),
    re.compile(r"\bover\s*\(", re.IGNORECASE),
)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_INLINE_COMMENT_RE = re.compile(r"(?<!:)//.*$")

#: file name or repo-relative path -> the issue that authorized the aggregate.
#: Empty on purpose: every fund-level aggregate moved to `factors.base.etf_virtual_company` in #727.
ALLOWED: dict[str, str] = {}


def _strip_comments(source: str) -> str:
    without_blocks = _BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), source)
    return "\n".join(_INLINE_COMMENT_RE.sub("", line) for line in without_blocks.splitlines())


def _is_allowed(path: Path) -> bool:
    if path.name in ALLOWED:
        return True
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return False
    return rel in ALLOWED


def _scan_file(path: Path) -> list[tuple[Path, int, str]]:
    raw = path.read_text(encoding="utf-8")
    raw_lines = raw.splitlines()
    cleaned = _strip_comments(raw)
    hit_lines = {
        cleaned.count("\n", 0, match.start()) + 1 for pattern in FORBIDDEN for match in pattern.finditer(cleaned)
    }
    return [(path, line_no, raw_lines[line_no - 1].strip()) for line_no in sorted(hit_lines)]


def offenders(roots: tuple[Path, ...] = SCAN_ROOTS) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for ext in ("*.ts", "*.tsx"):
            for path in sorted(root.rglob(ext)):
                if not _is_allowed(path):
                    found.extend(_scan_file(path))
    return found


def main() -> int:
    for root in SCAN_ROOTS:
        if not root.is_dir():
            print(f"no directory at {root}", file=sys.stderr)
            return 1
    found = offenders()
    if not found:
        print("app-layer aggregation check: web layer computes no cross-row metric")
        return 0
    for path, number, line in found:
        location = f"{path.relative_to(REPO_ROOT)}:{number}"
        print(f"::error file={path.relative_to(REPO_ROOT)},line={number}::{location}: {line}", file=sys.stderr)
    print(
        f"\n{len(found)} aggregate(s) in the App's mart readers (init.md §1 rule 2, #727).\n"
        "A metric computed across rows belongs in common/factors and is materialized into mart;\n"
        "the reader then reads a column. If this aggregate is genuinely presentation, add the\n"
        "file to ALLOWED in this script with the issue that decided it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
