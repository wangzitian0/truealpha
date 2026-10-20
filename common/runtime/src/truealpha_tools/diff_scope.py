"""Run only the checks that a diff reaches (#1412 T1).

Every local entry point (`make lint`, `make typecheck`, `make test`, `make smoke`,
`make check`, `tools/prepush.sh`) calls this module. The default scope is the diff
against a base ref (default `origin/main`, override with BASE=<ref>): committed,
staged, unstaged and untracked changes. `FULL=1` runs the whole repository, as CI does.

Root cause that this removes: the default local command was whole-repo work (all
pytest in one process, mypy over every package, every bun test), and the scoped path
was opt-in. Local commits are frequent, so the default decides the laptop load.

Selection rules:

1. Python imports: a test is selected when it imports a changed module, directly or
   through the first-party import graph (`import_graph.py`). A changed `__init__.py`
   also reaches every importer of a module under it, because Python runs it on import.
   A test that puts its directory on `sys.path` reaches its helpers by bare name. A
   changed `conftest.py` selects every test under its directory.
2. TypeScript imports: a bun test (`apps/app-web/tests/*.test.ts`) is selected when it
   imports a changed file, directly or through relative and `@/` imports.
3. Mentions: a file that names a changed file reaches it. Examples: a test that runs
   `tools/cut_release.sh`, a module that loads a JSON contract, a TypeScript test that
   reads `strategy.py`. A basename that more than one tracked file has also needs its
   parent directory name in the same file. Only code counts: the string constants of a
   Python file and the TypeScript text without comments. A change to a source module or
   to a document reaches only tests this way.
4. Directories: a test that names the directory of a change (two segments or more, not
   a package root, for example `db/migrations`) reaches it. A guard test that walks a
   whole tree (`rglob("*.py")`) is not selected by suffix: the 21 such tests cost 41 CPU
   seconds, more than the pre-push budget, so they stay in CI (A4: cross-package checks).
5. Explicit rules: workflows select the workflow gate test; files under `db/` select
   the migration tests; the root `pyproject.toml`, `uv.lock` and root `conftest.py`
   select every Python test; the web lockfile, `package.json` and TypeScript
   configuration select every bun test.
6. Fallback: a changed file inside a workspace member that no rule maps selects the
   tests of that member (package scope). The run record names each fallback.

mypy checks the changed modules and every module that reaches them. tsc has no file
scope, so it runs once when a web code file changes.

Each run appends one JSON line to `<git common dir>/truealpha-diff-scope/runs.jsonl`:
the selected and the skipped tests, the fallbacks, wall time and child CPU time. The
escape rate (#1412 S6) compares these records with the first CI failure of each PR.

Usage:
  python3 tools/diff_scope.py select [--base REF] [--root DIR]
  python3 tools/diff_scope.py run lint typecheck test conformance [--full] [--base REF] [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from truealpha_tools.import_graph import ImportFacts, absolute_import_names, ancestors, edges, module_name, scan

DEFAULT_BASE = "origin/main"
WEB_ROOT = "apps/app-web"
WEB_TEST_DIR = f"{WEB_ROOT}/tests"
WEB_CODE_SUFFIXES = (".ts", ".tsx", ".mjs", ".js")
WEB_ALL_TESTS_FILES = frozenset({"package.json", "bun.lock", "tsconfig.json", "next.config.mjs", "next-env.d.ts"})
PY_ALL_TESTS_FILES = frozenset({"pyproject.toml", "uv.lock", "conftest.py"})
WORKFLOW_GATE_TEST = "common/runtime/tests/test_ci_workflows.py"
MIGRATION_TEST_GLOB = "common/runtime/tests/test_migration*.py"
CONTRACT_DIRS = ("common/contracts/", f"{WEB_ROOT}/src/contracts/")
CONFORMANCE_CHECK = "common/contracts/conformance/export_issue58.py"
#: Files with no unit-test consumer. A test that names one still reaches it (rule 3).
NO_TEST_PREFIXES = ("docs/", "governance/", "skills/", ".claude/", ".agents/")
NO_TEST_SUFFIXES = (".md", ".png", ".svg", ".css", ".txt")
NO_TEST_NAMES = frozenset({"LICENSE", "Dockerfile", ".gitignore", ".dockerignore"})
MENTION_SUFFIXES = (".py", ".ts", ".tsx", ".mjs", ".js", ".sh")
SKIP_PARTS = frozenset({"node_modules", ".venv", ".next", "__pycache__"})
FULL_LINT_PATHS = ("apps", "common", "tools")
STATE_DIR = "truealpha-diff-scope"
RECORD_SCHEMA = "diff-scope-run.v1"
CACHE_SCHEMA = "import-facts.v2"


# --------------------------------------------------------------------------- layout


@dataclass(frozen=True)
class Layout:
    """The repository facts that selection needs, read from the root `pyproject.toml`."""

    root: Path
    members: tuple[str, ...]
    src_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    mypy_roots: tuple[str, ...]
    prefixes: tuple[str, ...]


def load_layout(root: Path) -> Layout:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    tool = config.get("tool", {})
    members = tuple(tool.get("uv", {}).get("workspace", {}).get("members", ()))
    src_roots = tuple(f"{m}/src" for m in members if (root / m / "src").is_dir())
    test_roots = tuple(tool.get("pytest", {}).get("ini_options", {}).get("testpaths", ()))
    mypy_roots = tuple(tool.get("mypy", {}).get("files", ()))
    prefixes = tuple(
        sorted(p.name for src in src_roots for p in (root / src).iterdir() if (p / "__init__.py").is_file())
    )
    return Layout(root, members, src_roots, test_roots, mypy_roots, prefixes)


# --------------------------------------------------------------------------- git


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def changed_files(root: Path, base: str) -> tuple[str, list[str]]:
    """The merge-base with `base` and every path changed since then, deleted paths included."""
    merge_base = git(root, "merge-base", base, "HEAD").strip()
    names = set(git(root, "diff", "--name-only", "--no-renames", merge_base).splitlines())
    names |= set(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return merge_base, sorted(n for n in names if n)


def tracked_files(root: Path) -> list[str]:
    files = set(git(root, "ls-files").splitlines())
    files |= set(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(
        f for f in files if f and not SKIP_PARTS.intersection(PurePosixPath(f).parts) and (root / f).is_file()
    )


def state_dir(root: Path) -> Path | None:
    """`<git common dir>/truealpha-diff-scope`: one place for every worktree of a clone."""
    try:
        common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(common) / STATE_DIR


# --------------------------------------------------------------------------- import facts cache


class FactsCache:
    """Import facts keyed by the SHA-256 of the file text.

    The key is the content, not the path, so every worktree of a clone shares the entries.
    Another process can write the file at the same time: the write goes to a temporary
    file and `os.replace` swaps it in, so a reader sees an old or a new file, never a
    part of one. A file that does not load is ignored and rebuilt.
    """

    def __init__(self, directory: Path | None) -> None:
        self.path = directory / "import-facts.json" if directory else None
        self.entries: dict[str, ImportFacts] = {}
        self.dirty = False
        if self.path and self.path.is_file():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                if stored.get("schema") == CACHE_SCHEMA:
                    self.entries = {k: ImportFacts.from_json(v) for k, v in stored["entries"].items()}
            except (OSError, ValueError, KeyError, AssertionError, TypeError):
                self.entries = {}

    def facts(self, text: str) -> ImportFacts:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cached = self.entries.get(key)
        if cached is not None:
            return cached
        try:
            facts = scan(ast.parse(text))
        except (SyntaxError, ValueError):
            # A file that does not parse reaches nothing through imports. Its own lint
            # and test step reports the syntax error.
            facts = ImportFacts((), (), (), ())
        self.entries[key] = facts
        self.dirty = True
        return facts

    def save(self, live: set[str]) -> None:
        if not (self.path and self.dirty):
            return
        keep = {k: v.to_json() for k, v in self.entries.items() if k in live}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".import-facts.")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"schema": CACHE_SCHEMA, "entries": keep}, stream)
            os.replace(temporary, self.path)
        except OSError as error:
            print(f"diff-scope: could not save the import cache: {error}", file=sys.stderr)


# --------------------------------------------------------------------------- selection


@dataclass
class Selection:
    base: str
    merge_base: str
    changed: list[str]
    pytest: list[str] = field(default_factory=list)
    pytest_all: list[str] = field(default_factory=list)
    bun: list[str] = field(default_factory=list)
    bun_all: list[str] = field(default_factory=list)
    mypy: list[str] = field(default_factory=list)
    tsc: bool = False
    conformance: bool = False
    lint: list[str] = field(default_factory=list)
    fallbacks: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_record(self) -> dict[str, object]:
        return {
            "base": self.base,
            "merge_base": self.merge_base,
            "changed": self.changed,
            "pytest": {"selected": self.pytest, "skipped": sorted(set(self.pytest_all) - set(self.pytest))},
            "bun": {"selected": self.bun, "skipped": sorted(set(self.bun_all) - set(self.bun))},
            "mypy": self.mypy,
            "tsc": self.tsc,
            "conformance": self.conformance,
            "lint": self.lint,
            "fallbacks": self.fallbacks,
            "notes": self.notes,
        }


def _is_python_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _is_web_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return str(pure.parent) == WEB_TEST_DIR and pure.name.endswith(".test.ts")


def _under(path: str, roots: Iterable[str]) -> str | None:
    for root in roots:
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def _no_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return path.startswith(NO_TEST_PREFIXES) or pure.suffix in NO_TEST_SUFFIXES or pure.name in NO_TEST_NAMES


def _suffix_names(relative: PurePosixPath) -> list[str]:
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


@dataclass
class _PyGraph:
    provides: dict[str, set[str]]
    direct: dict[str, set[str]]
    implied: dict[str, set[str]]
    strings: dict[str, str]


def _python_graph(layout: Layout, py_files: Sequence[str], texts: dict[str, str], cache: FactsCache) -> _PyGraph:
    """Names that each Python file provides and consumes.

    A source module provides its dotted name. A file under a test root provides
    `<test root>::<suffix>` for each dotted suffix of its path inside that root, so a
    bare-name import of a helper resolves only inside its own test tree.
    """
    modules = {module_name(Path(p), Path(src)) for p in py_files if (src := _under(p, layout.src_roots))}
    graph = _PyGraph({}, {}, {}, {})
    for path in py_files:
        facts = cache.facts(texts[path])
        found = edges(facts, layout.prefixes, modules, submodule_only=True)
        consumed = set(found)
        src = _under(path, layout.src_roots)
        test_root = _under(path, layout.test_roots)
        graph.strings[path] = "\n".join(facts.strings)
        if src:
            graph.provides[path] = {module_name(Path(path), Path(src))}
        elif test_root:
            relative = PurePosixPath(path).relative_to(test_root)
            graph.provides[path] = {f"{test_root}::{name}" for name in _suffix_names(relative)}
            consumed |= {f"{test_root}::{name}" for name in absolute_import_names(facts)}
        else:
            graph.provides[path] = set()
        graph.direct[path] = consumed
        graph.implied[path] = {a for name in found for a in ancestors(name)[:-1]}
    return graph


def _reverse_closure(graph: _PyGraph, seeds: set[str], changed_names: set[str]) -> set[str]:
    """Files that reach a seed file or a changed name through the import graph.

    An affected name reaches every direct consumer. Only a name that the diff itself
    changes reaches the implied consumers (importers of a module below it). A package
    `__init__` that imports a changed sibling therefore does not pull in every importer of
    the package: only the files that read a name from the package itself.
    """
    by_direct: dict[str, set[str]] = defaultdict(set)
    by_implied: dict[str, set[str]] = defaultdict(set)
    for path, names in graph.direct.items():
        for name in names:
            by_direct[name].add(path)
    for path, names in graph.implied.items():
        for name in names:
            by_implied[name].add(path)
    affected = set(seeds)
    queue = list(changed_names)
    for path in seeds:
        queue.extend(graph.provides.get(path, ()))
    seen: set[str] = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        consumers = set(by_direct.get(name, ()))
        if name in changed_names:
            consumers |= by_implied.get(name, set())
        for consumer in consumers - affected:
            affected.add(consumer)
            queue.extend(graph.provides.get(consumer, ()))
    return affected


_WEB_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s+)["']([^"']+)["']""",
    re.MULTILINE,
)


def _web_edges(files: Sequence[str], texts: dict[str, str]) -> dict[str, set[str]]:
    """Relative and `@/` imports of each web file, resolved to repository paths."""
    web_files = {f for f in files if f.startswith(WEB_ROOT + "/") and f.endswith(WEB_CODE_SUFFIXES)}
    result: dict[str, set[str]] = {}
    for path in web_files:
        targets: set[str] = set()
        for spec in _WEB_IMPORT.findall(texts.get(path, "")):
            if spec.startswith("@/"):
                base = PurePosixPath(WEB_ROOT, "src", spec[2:])
            elif spec.startswith("."):
                base = PurePosixPath(os.path.normpath(PurePosixPath(path).parent / spec))
            else:
                continue
            candidates = (
                str(base),
                *(f"{base}{ext}" for ext in WEB_CODE_SUFFIXES),
                *(f"{base}/index{ext}" for ext in WEB_CODE_SUFFIXES),
            )
            # An import of a file that the diff deleted still names it: keep the bare path.
            targets.add(next((c for c in candidates if c in web_files), str(base)))
        result[path] = targets
    return result


def _web_reverse_closure(web_edges: dict[str, set[str]], seeds: set[str]) -> set[str]:
    importers: dict[str, set[str]] = defaultdict(set)
    for path, targets in web_edges.items():
        for target in targets:
            importers[target].add(path)
            importers[str(PurePosixPath(target).with_suffix(""))].add(path)
    affected = set(seeds)
    queue = list(seeds) + [str(PurePosixPath(s).with_suffix("")) for s in seeds]
    while queue:
        for importer in importers.get(queue.pop(), ()):
            if importer not in affected:
                affected.add(importer)
                queue.append(importer)
    return affected


def _dir_pattern(path: str, package_roots: set[str]) -> re.Pattern[str] | None:
    """A pattern for the directory of `path`, or None when that directory is too broad.

    `"db/migrations"`, `"db", "migrations"` and `Path("db") / "migrations"` all name the same
    directory, so the separator between segments may be a slash, a comma or a path join.
    """
    parent = PurePosixPath(path).parent
    if len(parent.parts) < 2 or str(parent) in package_roots:
        return None
    joined = r"""["')\s]*[/,][\s"'(]*""".join(re.escape(s) for s in parent.parts)
    return re.compile(rf"(?<![\w.-]){joined}(?![\w.-])")


@dataclass
class _Corpus:
    """The text in which rule 3 and rule 4 look for a name."""

    texts: dict[str, str]
    raw: dict[str, str]
    test_side: set[str]

    def names(self, changed: str, basename_count: dict[str, int], tests_only: bool) -> set[str]:
        pure = PurePosixPath(changed)
        needles = [pure.name]
        if basename_count.get(pure.name, 0) > 1:
            # A shared basename (`page.tsx`, `__init__.py`) needs its parent directory too.
            if not pure.parent.name:
                return set()
            needles.append(pure.parent.name)
        pool = self.test_side if tests_only else self.texts.keys()
        return {p for p in pool if p != changed and all(n in self.texts[p] for n in needles)}

    def directory(self, pattern: re.Pattern[str] | None) -> set[str]:
        if pattern is None:
            return set()
        # Raw text: `ROOT / "db" / "migrations"` keeps its separators only there.
        return {p for p in self.test_side if pattern.search(self.raw[p])}


_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.DOTALL)


def _code_text(path: str, text: str, strings: dict[str, str]) -> str:
    """The part of a file that can read another file: no comments, no docstrings."""
    if path.endswith(".py"):
        return strings.get(path, "")
    if path.endswith(WEB_CODE_SUFFIXES):
        return _JS_COMMENT.sub("", text)
    return text


class _Selector:
    """Applies the selection rules to one change set. Each rule is one method."""

    def __init__(self, root: Path, selection: Selection) -> None:
        self.layout = load_layout(root)
        self.selection = selection
        files = tracked_files(root)
        self.existing = set(files)
        self.texts: dict[str, str] = {}
        for path in files:
            if path.endswith(MENTION_SUFFIXES):
                try:
                    self.texts[path] = (root / path).read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        py_files = [f for f in files if f.endswith(".py") and f in self.texts]
        selection.pytest_all = sorted(f for f in py_files if _under(f, self.layout.test_roots) and _is_python_test(f))
        selection.bun_all = sorted(f for f in files if _is_web_test(f))
        self.basename_count: dict[str, int] = defaultdict(int)
        for path in files:
            self.basename_count[PurePosixPath(path).name] += 1
        cache = FactsCache(state_dir(root))
        self.graph = _python_graph(self.layout, py_files, self.texts, cache)
        cache.save({hashlib.sha256(self.texts[p].encode("utf-8")).hexdigest() for p in py_files})
        self.web_edges = _web_edges(files, self.texts)
        test_side = {p for p in self.texts if _under(p, self.layout.test_roots) or p.startswith(WEB_TEST_DIR + "/")}
        code = {p: _code_text(p, t, self.graph.strings) for p, t in self.texts.items()}
        self.corpus = _Corpus(code, self.texts, test_side)
        layout = self.layout
        self.package_roots = set(layout.members) | set(layout.src_roots) | set(layout.test_roots) | {WEB_ROOT}
        self.py_seeds: set[str] = set()
        self.py_changed_names: set[str] = set()
        self.web_seeds: set[str] = set()
        self.py_tests: set[str] = set()
        self.all_py = False
        self.all_web = False

    def tests_under(self, directory: str) -> set[str]:
        return {t for t in self.selection.pytest_all if t.startswith(directory.rstrip("/") + "/")}

    def python_rule(self, path: str) -> bool:
        """Rule 1. A changed module seeds the graph; a deleted one still names its importers."""
        if not path.endswith(".py"):
            return False
        pure = PurePosixPath(path)
        src = _under(path, self.layout.src_roots)
        test_root = _under(path, self.layout.test_roots)
        if path in self.existing:
            self.selection.lint.append(path)
            self.py_seeds.add(path)
        if src:
            # A changed or deleted module is a changed name: a changed package `__init__`
            # reaches importers below it, and a deleted module still has importers that fail.
            self.py_changed_names.add(module_name(Path(path), Path(src)))
        elif test_root and path not in self.existing:
            relative = pure.relative_to(test_root)
            self.py_changed_names |= {f"{test_root}::{n}" for n in _suffix_names(relative)}
        if pure.name == "conftest.py" and path != "conftest.py":
            self.py_tests |= self.tests_under(str(pure.parent))
        return bool(src or test_root)

    def web_rule(self, path: str) -> bool:
        """Rule 2 seeds, and the web half of rule 5."""
        pure = PurePosixPath(path)
        if str(pure.parent) == WEB_ROOT and pure.name in WEB_ALL_TESTS_FILES:
            self.all_web = self.selection.tsc = True
            return True
        if path.startswith(WEB_ROOT + "/") and path.endswith(WEB_CODE_SUFFIXES):
            self.web_seeds.add(path)
            self.selection.tsc = True
            return True
        return False

    def explicit_rules(self, path: str) -> bool:
        """Rule 5: files whose consumers no import or mention shows."""
        pure = PurePosixPath(path)
        reached = False
        if path in PY_ALL_TESTS_FILES:
            self.all_py = reached = True
        if path.startswith(".github/workflows/"):
            self.py_tests.add(WORKFLOW_GATE_TEST)
            reached = True
        if path.startswith("db/"):
            self.py_tests |= {t for t in self.selection.pytest_all if PurePosixPath(t).match(MIGRATION_TEST_GLOB)}
            reached = True
        member = _under(path, self.layout.members)
        if member and pure.name == "pyproject.toml" and str(pure.parent) == member:
            self.py_tests |= self.tests_under(member)
            reached = True
        if path.startswith(CONTRACT_DIRS) or path == CONFORMANCE_CHECK:
            self.selection.conformance = True
        return reached

    def mention_rules(self, path: str) -> bool:
        """Rules 3 and 4: files that name the change or its directory."""
        tests_only = _no_test(path) or bool(path.endswith(".py") and _under(path, self.layout.src_roots))
        named = self.corpus.names(path, self.basename_count, tests_only)
        named |= self.corpus.directory(_dir_pattern(path, self.package_roots))
        for other in named:
            (self.py_seeds if other.endswith(".py") else self.web_seeds).add(other)
        return bool(named)

    def fallback(self, path: str) -> None:
        """Rule 6: package scope for a file that no rule maps, and the record says so."""
        if _no_test(path):
            return
        member = _under(path, self.layout.members)
        if path.startswith(WEB_ROOT + "/"):
            self.all_web = True
            self.selection.fallbacks.append({"path": path, "scope": WEB_ROOT, "reason": "no rule maps this file"})
        elif member:
            self.py_tests |= self.tests_under(member)
            self.selection.fallbacks.append({"path": path, "scope": member, "reason": "no rule maps this file"})
        else:
            self.selection.notes.append(f"{path}: no rule maps this file and it is outside every package")

    def finish(self) -> Selection:
        selection = self.selection
        py_tests_set = set(selection.pytest_all)
        affected_py = _reverse_closure(self.graph, self.py_seeds, self.py_changed_names)
        self.py_tests |= affected_py & py_tests_set
        affected_web = _web_reverse_closure(self.web_edges, self.web_seeds)
        selection.pytest = selection.pytest_all if self.all_py else sorted(self.py_tests)
        selection.bun = selection.bun_all if self.all_web else sorted(f for f in affected_web if _is_web_test(f))
        selection.mypy = sorted(f for f in affected_py if f in self.existing and _under(f, self.layout.mypy_roots))
        selection.lint = sorted(set(selection.lint))
        for path in selection.changed:
            if path in self.existing and path.endswith(".py") and _under(path, self.layout.src_roots):
                if not _reverse_closure(self.graph, {path}, set()) & py_tests_set:
                    selection.notes.append(f"{path}: no test imports this module")
        return selection


def select(root: Path, base: str = DEFAULT_BASE, changed: Sequence[str] | None = None) -> Selection:
    """Compute the checks that the diff against `base` reaches."""
    if changed is None:
        merge_base, changed_list = changed_files(root, base)
    else:
        merge_base, changed_list = "", sorted(set(changed))
    selector = _Selector(root, Selection(base=base, merge_base=merge_base, changed=changed_list))
    for path in changed_list:
        reached = selector.python_rule(path)
        reached = selector.web_rule(path) or reached
        reached = selector.explicit_rules(path) or reached
        reached = selector.mention_rules(path) or reached
        if not reached:
            selector.fallback(path)
    return selector.finish()


# --------------------------------------------------------------------------- run


@dataclass(frozen=True)
class Command:
    step: str
    argv: tuple[str, ...]
    cwd: str = "."


def plan(selection: Selection | None, steps: Sequence[str], *, web_tests_all: Sequence[str] = ()) -> list[Command]:
    """The commands for each step. A `selection` of None means FULL=1: the whole repository."""
    full = selection is None
    commands: list[Command] = []
    for step in steps:
        if step == "lint":
            paths: Sequence[str] = FULL_LINT_PATHS if selection is None else selection.lint
            if paths:
                commands.append(Command("lint", ("uv", "run", "ruff", "check", *paths)))
                commands.append(Command("lint", ("uv", "run", "ruff", "format", "--check", *paths)))
        elif step == "typecheck":
            if selection is None:
                commands.append(Command("typecheck", ("uv", "run", "mypy")))
            elif selection.mypy:
                commands.append(Command("typecheck", ("uv", "run", "mypy", *selection.mypy)))
            if full or (selection is not None and selection.tsc):
                commands.append(Command("typecheck", ("bun", "run", "typecheck"), WEB_ROOT))
        elif step == "test":
            if selection is None:
                commands.append(Command("test", ("uv", "run", "pytest")))
            elif selection.pytest:
                commands.append(Command("test", ("uv", "run", "pytest", *selection.pytest)))
            for test in web_tests_all if selection is None else selection.bun:
                relative = str(PurePosixPath(test).relative_to(WEB_ROOT))
                commands.append(Command("test", ("bun", "run", relative), WEB_ROOT))
        elif step == "conformance":
            if full or (selection is not None and selection.conformance):
                commands.append(Command("conformance", ("uv", "run", "python", CONFORMANCE_CHECK, "--check")))
        else:
            raise ValueError(f"unknown step {step!r}: expected lint, typecheck, test or conformance")
    return commands


def all_web_tests(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in (root / WEB_TEST_DIR).glob("*.test.ts"))


def _write_record(root: Path, record: dict[str, object]) -> None:
    directory = state_dir(root)
    if directory is None:
        print("diff-scope: no git common dir; this run is not recorded", file=sys.stderr)
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # One `write` of one line in append mode: concurrent runs do not interleave lines.
        with (directory / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as error:
        print(f"diff-scope: could not write the run record in {directory}: {error}", file=sys.stderr)


def _print_selection(selection: Selection) -> None:
    print(f"diff-scope: {len(selection.changed)} changed file(s) against {selection.base}")
    print(
        f"diff-scope: pytest {len(selection.pytest)}/{len(selection.pytest_all)} files, "
        f"bun {len(selection.bun)}/{len(selection.bun_all)} tests, mypy {len(selection.mypy)} modules, "
        f"tsc {'yes' if selection.tsc else 'no'}, lint {len(selection.lint)} files"
    )
    for fallback in selection.fallbacks:
        print(f"diff-scope: package scope {fallback['scope']} for {fallback['path']} ({fallback['reason']})")
    for note in selection.notes:
        print(f"diff-scope: note: {note}")


def run(root: Path, steps: Sequence[str], *, full: bool, base: str, dry_run: bool, entry: str) -> int:
    started = time.monotonic()
    selection: Selection | None = None
    if full:
        print("diff-scope: FULL=1, whole repository")
    else:
        selection = select(root, base)
        _print_selection(selection)
        if not selection.changed:
            print(f"diff-scope: nothing changed against {base}; no check selected. FULL=1 runs everything.")
    commands = plan(selection, steps, web_tests_all=all_web_tests(root))
    exit_code = 0
    for command in commands:
        shown = " ".join(command.argv[:10]) + (f" ... (+{len(command.argv) - 10})" if len(command.argv) > 10 else "")
        print(f"== [{command.step}] {shown}" + (f"  (in {command.cwd})" if command.cwd != "." else ""), flush=True)
        if dry_run:
            continue
        result = subprocess.run(command.argv, cwd=root / command.cwd, check=False)
        if result.returncode != 0:
            exit_code = result.returncode
            print(f"diff-scope: [{command.step}] failed with exit {result.returncode}", file=sys.stderr)
            break
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    record: dict[str, object] = {
        "schema": RECORD_SCHEMA,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry": entry,
        "steps": list(steps),
        "full": full,
        "dry_run": dry_run,
        "head": git(root, "rev-parse", "HEAD").strip(),
        "commands": len(commands),
        "exit": exit_code,
        "wall_seconds": round(time.monotonic() - started, 3),
        "child_cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
    }
    if selection is not None:
        record.update(selection.as_record())
    _write_record(root, record)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run only the checks that a diff reaches (#1412 T1).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("select", "run"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=None)
        command.add_argument("--base", default=os.environ.get("BASE") or DEFAULT_BASE)
        if name == "run":
            command.add_argument("steps", nargs="+", choices=("lint", "typecheck", "test", "conformance"))
            command.add_argument("--full", action="store_true", default=os.environ.get("FULL", "") not in ("", "0"))
            command.add_argument(
                "--dry-run", action="store_true", default=os.environ.get("DIFF_SCOPE_DRY_RUN", "") not in ("", "0")
            )
            command.add_argument("--entry", default=os.environ.get("DIFF_SCOPE_ENTRY", "direct"))
    args = parser.parse_args(argv)
    root = (args.root or Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())).resolve()
    if args.command == "select":
        print(json.dumps(select(root, args.base).as_record(), indent=2, sort_keys=True))
        return 0
    return run(root, args.steps, full=args.full, base=args.base, dry_run=args.dry_run, entry=args.entry)


if __name__ == "__main__":
    raise SystemExit(main())
