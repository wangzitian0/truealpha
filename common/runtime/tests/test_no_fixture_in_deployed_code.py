"""#434 exit criterion 3, as a check that runs again: no deployed consumption code
instantiates a fixture repository or reads a fixture module.

CLAUDE.md: fixture data lives in tests only and is never reachable from a deployed
route. llm-service carried a `strategy_run_backend` flag that could select
`FixtureStrategyRunRepository` in a deployed process until 2026-09-07; app-web keeps
its fixture repository class as a tests-only injection point. This scan is what turns
red if either comes back. It reads source text, not imports, so it cannot be fooled by
a lazy import inside a function.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONSUMPTION_SOURCES = (REPO / "apps" / "llm-service" / "src", REPO / "apps" / "app-web" / "src")
# An instantiation or call of a Fixture* class; a `class FixtureX(...)` definition is not
# a reach, and neither is a mention inside a comment or docstring.
INSTANTIATION = re.compile(r"(?<![\w.])(?<!class )Fixture[A-Za-z]+\s*\(")
FIXTURE_MODULE_IMPORT = re.compile(r"^\s*(from|import)\s+truealpha_contracts\.[a-z_]+_fixture\b", re.M)


def _strip_comments(text: str, suffix: str) -> str:
    if suffix == ".py":
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        return re.sub(r"#[^\n]*", "", text)
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    return re.sub(r"//[^\n]*", "", text)


def _sources() -> list[Path]:
    out: list[Path] = []
    for root in CONSUMPTION_SOURCES:
        out.extend(p for p in root.rglob("*") if p.suffix in (".py", ".ts", ".tsx") and "node_modules" not in p.parts)
    return out


def test_no_deployed_consumption_module_instantiates_a_fixture() -> None:
    offenders = []
    for path in _sources():
        body = _strip_comments(path.read_text(encoding="utf-8"), path.suffix)
        for match in INSTANTIATION.finditer(body):
            offenders.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert not offenders, "fixture reached from deployed consumption code:\n" + "\n".join(offenders)


def test_llm_service_imports_no_fixture_module() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in (REPO / "apps" / "llm-service" / "src").rglob("*.py")
        if FIXTURE_MODULE_IMPORT.search(_strip_comments(path.read_text(encoding="utf-8"), ".py"))
    ]
    assert not offenders, "llm-service imports a fixture module in deployed code:\n" + "\n".join(offenders)


def test_the_scan_is_not_vacuous() -> None:
    files = _sources()
    assert len(files) > 50, "the consumption source trees are not where this test thinks they are"
    assert INSTANTIATION.search("return FixtureStrategyRunRepository()"), "the pattern must catch the 2026-09-07 shape"
    assert INSTANTIATION.search("return FixtureStrategyRunRepository ()"), (
        "whitespace before the parenthesis is still a call"
    )
    assert not INSTANTIATION.search("class FixtureStrategyRunRepository(Base):"), "a definition is not a reach"


# --- #42: the import closure of every deployed Python entry point --------------------------
#
# The scans above read two source trees. They missed the reach that #42 found: llm-service
# imported `MartResearchReadRepository` from common/contracts, and that class subclassed
# `FixtureResearchReadRepository`, so three report sections were fixture constants in
# production. A deployed path is what an entry point imports (AGENTS.md rule 6), whatever
# the directory. So this walks the import graph from each entry point through every
# first-party package, including imports inside function bodies.

import ast  # noqa: E402

#: The modules that deployed processes load: uvicorn (llm-service) and Dagster's code location.
DEPLOYED_ENTRY_POINTS = ("llm_service.main", "data_engine.dagster_defs")
#: Each first-party top-level package and the directory that holds it.
FIRST_PARTY_ROOTS = {
    "llm_service": REPO / "apps" / "llm-service" / "src",
    "data_engine": REPO / "apps" / "data-engine" / "src",
    "truealpha_contracts": REPO / "common" / "contracts" / "src",
    "truealpha_runtime": REPO / "common" / "runtime" / "src",
    "truealpha_tools": REPO / "common" / "runtime" / "src",
    "factors": REPO / "common" / "factors" / "src",
}
FIXTURE_NAME = re.compile(r"^Fixture[A-Z]\w*$")


def _module_file(module: str) -> Path | None:
    root = FIRST_PARTY_ROOTS.get(module.split(".", 1)[0])
    if root is None:
        return None
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported(module: str, path: Path, tree: ast.AST) -> list[tuple[str, tuple[str, ...]]]:
    """Every (module, names) an import statement anywhere in `tree` names."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.append((target, tuple(alias.name for alias in node.names)))
    return found


def _fixture_reaches(module: str, path: Path, tree: ast.AST) -> list[str]:
    reaches = []
    for target, names in _imported(module, path, tree):
        if target.rsplit(".", 1)[-1].endswith("_fixture") or any(n.endswith("_fixture") for n in names):
            reaches.append(f"{module} imports the fixture module {target}")
        reaches.extend(f"{module} imports {name} from {target}" for name in names if FIXTURE_NAME.match(name))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if FIXTURE_NAME.match(name):
                    reaches.append(f"{module}: class {node.name} subclasses {name}")
    return reaches


def deployed_closure(entry_points: tuple[str, ...] = DEPLOYED_ENTRY_POINTS) -> dict[str, Path]:
    """Every first-party module that importing the entry points can load, with its file.
    Importing `a.b.c` runs `a` and `a.b` first, so each parent package is in the closure too."""
    closure: dict[str, Path] = {}
    pending = list(entry_points)
    while pending:
        module = pending.pop()
        if module in closure:
            continue
        path = _module_file(module)
        if path is None:
            continue
        closure[module] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for target, names in _imported(module, path, tree):
            parts = target.split(".")
            pending.extend(".".join(parts[:i]) for i in range(1, len(parts) + 1))
            pending.extend(f"{target}.{name}" for name in names)
    return closure


def test_no_deployed_python_module_imports_or_subclasses_a_fixture() -> None:
    offenders = []
    for module, path in sorted(deployed_closure().items()):
        offenders.extend(_fixture_reaches(module, path, ast.parse(path.read_text(encoding="utf-8"))))
    assert not offenders, "fixture code is reachable from a deployed entry point (AGENTS.md rule 6):\n" + "\n".join(
        offenders
    )


def test_the_closure_walk_is_not_vacuous() -> None:
    closure = deployed_closure()
    for module in (
        *DEPLOYED_ENTRY_POINTS,
        "llm_service.mcp_server",
        "truealpha_contracts",
        "truealpha_contracts.research_report_mart",
        "truealpha_contracts.strategy_run_postgres",
    ):
        assert module in closure, f"{module} is deployed but the walk did not reach it"
    assert len(closure) > 100, f"the walk reached only {len(closure)} modules"


def test_the_reach_check_catches_the_2026_10_20_shape() -> None:
    """The exact source of #42, and the shapes next to it, each named."""
    path = REPO / "common/contracts/src/truealpha_contracts/research_report_mart.py"
    shapes = {
        "from truealpha_contracts.research_report_fixture import FixtureResearchReadRepository\n"
        "class MartResearchReadRepository(FixtureResearchReadRepository):\n    pass\n": 3,
        "from . import strategy_run_fixture\n": 1,
        "def f():\n    from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository\n": 2,
        "import truealpha_contracts.strategy_run_fixture as source\n": 1,
        "class Reader(source.FixtureStrategyRunRepository):\n    pass\n": 1,
    }
    for source, expected in shapes.items():
        found = _fixture_reaches("truealpha_contracts.research_report_mart", path, ast.parse(source))
        assert len(found) == expected, (source, found)
    clean = "from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository\n"
    assert _fixture_reaches("truealpha_contracts.x", path, ast.parse(clean)) == []
