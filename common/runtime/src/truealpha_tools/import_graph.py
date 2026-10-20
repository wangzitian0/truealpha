"""The first-party Python import graph (#1412 T1).

This module holds one rule for "module A depends on module B". The diff scope
(`diff_scope.py`) walks the edges backwards from a change to the tests that can see
it. The reachability ratchet walks the same kind of edge forwards from the deployed
roots. The ratchet still holds a private copy of this rule until its owner-held
switch lands (#1412).

An edge is one of three things:

- an `import x.y` or `from x.y import z` statement that names a first-party package;
- a registry string "x.y:function", which a deployed loop resolves at run time (#72, #800);
- a bare string that equals a module name, such as a lane named in `LANE_MODULES` (#731);
- a call of a registered loader with a string argument: `load_tool("walk_evidence")`
  imports `truealpha_tools.walk_evidence` (`truealpha_runtime.testing.load_tool`).
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

#: Loader function name -> module name template. A test loads a tool module through
#: `load_tool("<name>")`, never through an import statement (#583).
DYNAMIC_LOADERS: dict[str, str] = {"load_tool": "truealpha_tools.{}"}


@dataclass(frozen=True)
class ImportFacts:
    """What one parsed file imports and which string constants it holds.

    One walk of the syntax tree produces all of it. The walk is the cost: three walks of
    every file in the repository took 3 s of the 4.7 s that a selection took.
    """

    imports: tuple[str, ...]
    from_imports: tuple[tuple[str, tuple[str, ...]], ...]
    strings: tuple[str, ...]
    loaded: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "imports": list(self.imports),
            "from": [[m, list(n)] for m, n in self.from_imports],
            "strings": list(self.strings),
            "loaded": list(self.loaded),
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> ImportFacts:
        imports = data["imports"]
        from_imports = data["from"]
        strings = data["strings"]
        loaded = data["loaded"]
        assert isinstance(imports, list) and isinstance(from_imports, list)
        assert isinstance(strings, list) and isinstance(loaded, list)
        return cls(
            imports=tuple(str(i) for i in imports),
            from_imports=tuple((str(m), tuple(str(n) for n in names)) for m, names in from_imports),
            strings=tuple(str(s) for s in strings),
            loaded=tuple(str(m) for m in loaded),
        )


def scan(tree: ast.AST) -> ImportFacts:
    """Collect the import statements and the non-docstring string constants of one file."""
    # `ast.walk` is breadth first, so a definition is visited before its docstring node.
    docstrings: set[int] = set()
    imports: list[str] = []
    from_imports: list[tuple[str, tuple[str, ...]]] = []
    strings: list[str] = []
    loaded: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            called = function.id if isinstance(function, ast.Name) else getattr(function, "attr", None)
            template = DYNAMIC_LOADERS.get(called) if isinstance(called, str) else None
            if (
                template
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                loaded.append(template.format(node.args[0].value))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings:
                strings.append(node.value)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                from_imports.append((node.module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return ImportFacts(
        imports=tuple(imports), from_imports=tuple(from_imports), strings=tuple(strings), loaded=tuple(loaded)
    )


def module_index(src_roots: Iterable[Path]) -> dict[str, Path]:
    """Map each dotted module name under the source roots to its file."""
    modules: dict[str, Path] = {}
    for src in src_roots:
        for path in sorted(src.rglob("*.py")):
            name = module_name(path, src)
            if name:
                modules[name] = path
    return modules


def module_name(path: Path, src_root: Path) -> str:
    """The dotted name of one file under a source root. A package `__init__` gets the package name."""
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def ancestors(name: str) -> list[str]:
    """`a.b.c` -> `a`, `a.b`, `a.b.c`. Import of a.b.c runs the `__init__` of a and of a.b."""
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def edges(
    facts: ImportFacts,
    prefixes: tuple[str, ...],
    modules: Collection[str],
    *,
    submodule_only: bool = False,
) -> set[str]:
    """The first-party module names that one file depends on.

    `from a import b`: when `a.b` is a module, the edge is `a.b`. When `submodule_only` is
    false, the edge set also holds `a`, as the reachability walk wants. When it is true, `a`
    is an edge only for a name that is not a submodule, because only then does the file
    read a name that `a/__init__.py` defines. The diff scope uses this form: it must not
    select every importer of a package when one sibling submodule changes.
    """
    found: set[str] = set()
    for module, names in facts.from_imports:
        if not module.startswith(prefixes):
            continue
        package_name_read = not submodule_only
        for name in names:
            candidate = f"{module}.{name}"
            if candidate in modules:
                found.add(candidate)
            else:
                package_name_read = True
        if package_name_read:
            found.add(module)
    found.update(name for name in facts.imports if name.startswith(prefixes))
    found.update(name for name in facts.loaded if name in modules)
    for value in facts.strings:
        name, sep, attribute = value.partition(":")
        if sep and name in modules and attribute.isidentifier():
            found.add(name)
        elif not sep and value in modules:
            found.add(value)
    return found


def import_edges(
    tree: ast.AST,
    prefixes: tuple[str, ...],
    modules: Collection[str],
    *,
    submodule_only: bool = False,
) -> set[str]:
    """`edges` of a parsed file."""
    return edges(scan(tree), prefixes, modules, submodule_only=submodule_only)


def absolute_import_names(facts: ImportFacts) -> set[str]:
    """Every absolute import target in a file, first-party or not.

    A test can put its own directory on `sys.path` and import a sibling helper by a bare
    name (`from qqq_head_seed import seed`). The caller resolves these names against the
    helpers of the same test tree.
    """
    names = set(facts.imports)
    for module, aliases in facts.from_imports:
        names.add(module)
        names.update(f"{module}.{alias}" for alias in aliases)
    return names
