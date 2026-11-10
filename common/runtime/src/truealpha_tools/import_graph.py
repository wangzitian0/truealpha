# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import ast; from collections.abc import Collection, Iterable; from dataclasses import dataclass; from pathlib import Path; DYNAMIC_LOADERS: dict[str, str] = {"load_tool": "truealpha_tools.{}"}
@dataclass(frozen=True)
class ImportFacts:
    imports: tuple[str, ...]; from_imports: tuple[tuple[str, tuple[str, ...]], ...]; strings: tuple[str, ...]; loaded: tuple[str, ...] = ()
    def to_json(self) -> dict[str, object]: return { "imports": list(self.imports), "from": [[m, list(n)] for m, n in self.from_imports], "strings": list(self.strings), "loaded": list(self.loaded), }
    @classmethod
    def from_json(cls, data: dict[str, object]) -> ImportFacts: imports = data["imports"]; from_imports = data["from"]; strings = data["strings"]; loaded = data["loaded"]; assert isinstance(imports, list) and isinstance(from_imports, list); assert isinstance(strings, list) and isinstance(loaded, list); return cls( imports=tuple(str(i) for i in imports), from_imports=tuple((str(m), tuple(str(n) for n in names)) for m, names in from_imports), strings=tuple(str(s) for s in strings), loaded=tuple(str(m) for m in loaded), )
def scan(tree: ast.AST) -> ImportFacts:
    docstrings: set[int] = set(); imports: list[str] = []; from_imports: list[tuple[str, tuple[str, ...]]] = []; strings: list[str] = []; loaded: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func; called = function.id if isinstance(function, ast.Name) else getattr(function, "attr", None); template = DYNAMIC_LOADERS.get(called) if isinstance(called, str) else None
            if ( template and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) ): loaded.append(template.format(node.args[0].value))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings: strings.append(node.value)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module: from_imports.append((node.module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Import): imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant): docstrings.add(id(body[0].value))
    return ImportFacts( imports=tuple(imports), from_imports=tuple(from_imports), strings=tuple(strings), loaded=tuple(loaded) )
def module_index(src_roots: Iterable[Path]) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for src in src_roots:
        for path in sorted(src.rglob("*.py")):
            name = module_name(path, src)
            if name: modules[name] = path
    return modules
def module_name(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts and parts[-1] == "__init__": parts = parts[:-1]
    return ".".join(parts)
def ancestors(name: str) -> list[str]: parts = name.split("."); return [".".join(parts[: i + 1]) for i in range(len(parts))]
def edges( facts: ImportFacts, prefixes: tuple[str, ...], modules: Collection[str], *, submodule_only: bool = False, ) -> set[str]:
    found: set[str] = set()
    for module, names in facts.from_imports:
        if not module.startswith(prefixes): continue
        package_name_read = not submodule_only
        for name in names:
            candidate = f"{module}.{name}"
            if candidate in modules: found.add(candidate)
            else: package_name_read = True
        if package_name_read: found.add(module)
    found.update(name for name in facts.imports if name.startswith(prefixes)); found.update(name for name in facts.loaded if name in modules)
    for value in facts.strings:
        name, sep, attribute = value.partition(":")
        if sep and name in modules and attribute.isidentifier(): found.add(name)
        elif not sep and value in modules: found.add(value)
    return found
def import_edges( tree: ast.AST, prefixes: tuple[str, ...], modules: Collection[str], *, submodule_only: bool = False, ) -> set[str]: return edges(scan(tree), prefixes, modules, submodule_only=submodule_only)
def absolute_import_names(facts: ImportFacts) -> set[str]:
    names = set(facts.imports)
    for module, aliases in facts.from_imports: names.add(module); names.update(f"{module}.{alias}" for alias in aliases)
    return names
