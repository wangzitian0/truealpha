"""Do init.md, vision.md and README.md cite only what exists, and hold no status stamp? (#1410 W1)

The three documents state rules and point to their sources. They drift when code moves and the
text stays: on 2026-10-20 init.md cited 17 `libs/` paths after #1373 moved `libs/` to `common/`,
named two `mart` objects that no migration creates, and held five dated status stamps. This
check reads the documents at one git revision (default `HEAD`) and reports three kinds of
finding:

  * path: a code span or a Markdown link target that has the shape of a repository path
    (at least one `/`, no space, no `*`, no URL scheme, no leading `/`, and a first segment
    with no `.` unless it is a top-level entry) resolves to no tracked file or directory. A
    citation resolves when it is a tracked path, a tracked directory, or the tail of one at a
    segment boundary (`datahub/share_basis.py` names a module inside its package). Write a
    slash token that is not a repository path (an image name, an MCP method) outside a code
    span.
  * mart: a `mart.<name>` reference names an object that the migration chain does not create,
    or drops or renames later. The chain is `db/migrations/*.sql` in filename order, which is
    the order `db/apply_migrations.sh` applies. `mart.<prefix>_*` is a wildcard and is skipped.
  * stamp: a `Status 20xx` or `as of 20xx` stamp, in any letter case. Delivery state lives on
    the issues and the dashboards (ADR A8), not in these documents.

It runs nightly in `.github/workflows/doc-drift.yml` and on `workflow_dispatch` there. A red
run is the verdict. It gates no merge and opens no issue (ADR A8). Manual replay:

    python3 common/runtime/src/truealpha_tools/doc_drift.py [--ref <revision>]

Standard library only, so the workflow installs nothing.

Exit codes:
  0 - every citation resolves and no stamp is present
  1 - at least one finding, or the documents cite nothing (a parser that finds no citation
      proves nothing)
  2 - a document is missing at the revision, or git cannot read the revision
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The documents this check reads. They are the entry points that an agent or a person reads
#: first (AGENTS.md "Architecture authority").
DOCUMENTS = ("init.md", "vision.md", "README.md")
MIGRATIONS_DIR = "db/migrations"

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
_PATH_SHAPE = re.compile(r"^[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?$")
_MART_REFERENCE = re.compile(r"\bmart\.([a-z_][a-z0-9_]*)(\*?)")
_STAMP = re.compile(r"\b(?:status|as of) 20\d\d", re.IGNORECASE)

_NAME = r"mart\.([a-z_][a-z0-9_]*)"
_CREATE = re.compile(
    rf"\bcreate\s+(?:or\s+replace\s+)?(?:materialized\s+)?(?:table|view|function|type|sequence)\s+"
    rf"(?:if\s+not\s+exists\s+)?{_NAME}",
    re.IGNORECASE,
)
_DROP = re.compile(
    rf"\bdrop\s+(?:materialized\s+)?(?:table|view|function|type|sequence)\s+(?:if\s+exists\s+)?{_NAME}",
    re.IGNORECASE,
)
_RENAME = re.compile(
    rf"\balter\s+(?:materialized\s+)?(?:table|view)\s+(?:if\s+exists\s+)?{_NAME}\s+rename\s+to\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    document: str
    line: int
    kind: str
    subject: str

    def __str__(self) -> str:
        reason = {
            "path": "cites a path that resolves to no tracked file or directory",
            "mart": "names a mart object that the migration chain does not create",
            "stamp": "holds a status stamp",
        }[self.kind]
        return f"{self.document}:{self.line}: {self.kind}: {self.subject!r} {reason}"


@dataclass(frozen=True)
class Report:
    findings: tuple[Finding, ...]
    paths_checked: int
    mart_checked: int


def mart_objects(migrations: Sequence[tuple[str, str]]) -> frozenset[str]:
    """The mart objects that exist after the chain applies. `migrations` is (filename, SQL)."""
    alive: set[str] = set()
    for _, sql in sorted(migrations):
        events = [(match.start(), "create", match.group(1), "") for match in _CREATE.finditer(sql)]
        events += [(match.start(), "drop", match.group(1), "") for match in _DROP.finditer(sql)]
        events += [(match.start(), "rename", match.group(1), match.group(2)) for match in _RENAME.finditer(sql)]
        for _, kind, name, new_name in sorted(events):
            if kind == "create":
                alive.add(name.lower())
            elif kind == "drop":
                alive.discard(name.lower())
            elif name.lower() in alive:
                alive.discard(name.lower())
                alive.add(new_name.lower())
    return frozenset(alive)


class _Tree:
    """The tracked files of one revision, and every directory and tail they imply."""

    def __init__(self, files: Iterable[str]) -> None:
        self._entries: set[str] = set()
        self._tails: set[str] = set()
        self.top_level: set[str] = set()
        for path in files:
            parts = path.split("/")
            self.top_level.add(parts[0])
            for end in range(1, len(parts) + 1):
                self._entries.add("/".join(parts[:end]))
            for start in range(1, len(parts)):
                for end in range(start + 1, len(parts) + 1):
                    self._tails.add("/".join(parts[start:end]))

    def resolves(self, citation: str) -> bool:
        citation = citation.rstrip("/")
        return citation in self._entries or citation in self._tails


def _citations(line: str) -> list[str]:
    tokens = [match.group(1).strip() for match in _CODE_SPAN.finditer(line)]
    tokens += [match.group(1).strip() for match in _LINK_TARGET.finditer(line)]
    return tokens


def _is_path_citation(token: str, tree: _Tree) -> bool:
    if "://" in token or token.startswith("/") or "*" in token or not _PATH_SHAPE.match(token):
        return False
    first = token.split("/", 1)[0]
    # A host name (`ghcr.io/...`, `data.sec.gov/...`) is not a repository path.
    return "." not in first or first in tree.top_level


def check(documents: Mapping[str, str], files: Iterable[str], migrations: Sequence[tuple[str, str]]) -> Report:
    """Every finding in `documents` ({name: text}) against one tree and one migration chain."""
    tree = _Tree(files)
    alive = mart_objects(migrations)
    findings: list[Finding] = []
    paths_checked = 0
    mart_checked = 0
    for name, text in documents.items():
        for number, line in enumerate(text.splitlines(), 1):
            for token in _citations(line):
                if not _is_path_citation(token, tree):
                    continue
                paths_checked += 1
                if not tree.resolves(token):
                    findings.append(Finding(name, number, "path", token))
            for match in _MART_REFERENCE.finditer(line):
                if match.group(2):
                    continue
                mart_checked += 1
                if match.group(1) not in alive:
                    findings.append(Finding(name, number, "mart", f"mart.{match.group(1)}"))
            for match in _STAMP.finditer(line):
                findings.append(Finding(name, number, "stamp", match.group(0)))
    return Report(tuple(findings), paths_checked, mart_checked)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def read_revision(root: Path, ref: str) -> tuple[dict[str, str], list[str], list[tuple[str, str]]]:
    """The documents, the tracked files and the migration chain of `ref`, read from git."""
    files = _git(root, "ls-tree", "-r", "--name-only", ref).splitlines()
    tracked = set(files)
    missing = [name for name in DOCUMENTS if name not in tracked]
    if missing:
        raise FileNotFoundError(f"{ref} does not track {', '.join(missing)}")
    documents = {name: _git(root, "show", f"{ref}:{name}") for name in DOCUMENTS}
    migrations = [
        (path.rsplit("/", 1)[1], _git(root, "show", f"{ref}:{path}"))
        for path in files
        if path.startswith(f"{MIGRATIONS_DIR}/") and path.endswith(".sql") and path.count("/") == 2
    ]
    return documents, files, migrations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ref", default="HEAD", help="the git revision to read (default: HEAD)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="a path inside the repository")
    args = parser.parse_args(argv)
    try:
        documents, files, migrations = read_revision(args.root, args.ref)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"doc-drift: cannot read {args.ref}: {detail}", file=sys.stderr)
        return 2
    report = check(documents, files, migrations)
    for finding in report.findings:
        print(finding)
    kinds = {kind: sum(1 for finding in report.findings if finding.kind == kind) for kind in ("path", "mart", "stamp")}
    print(
        f"doc-drift at {args.ref}: {report.paths_checked} path citations and {report.mart_checked} mart references "
        f"checked in {', '.join(DOCUMENTS)}; findings: {kinds['path']} path, {kinds['mart']} mart, "
        f"{kinds['stamp']} stamp"
    )
    if report.paths_checked == 0 and report.mart_checked == 0:
        print("doc-drift: the documents cite no path and no mart object, so this run proves nothing", file=sys.stderr)
        return 1
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
