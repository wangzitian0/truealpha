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
INSTANTIATION = re.compile(r"(?<![\w.])(?<!class )Fixture[A-Za-z]+\(")
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
        body = _strip_comments(path.read_text(), path.suffix)
        for match in INSTANTIATION.finditer(body):
            offenders.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert not offenders, "fixture reached from deployed consumption code:\n" + "\n".join(offenders)


def test_llm_service_imports_no_fixture_module() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in (REPO / "apps" / "llm-service" / "src").rglob("*.py")
        if FIXTURE_MODULE_IMPORT.search(_strip_comments(path.read_text(), ".py"))
    ]
    assert not offenders, "llm-service imports a fixture module in deployed code:\n" + "\n".join(offenders)


def test_the_scan_is_not_vacuous() -> None:
    files = _sources()
    assert len(files) > 50, "the consumption source trees are not where this test thinks they are"
    assert INSTANTIATION.search("return FixtureStrategyRunRepository()"), "the pattern must catch the 2026-09-07 shape"
    assert not INSTANTIATION.search("class FixtureStrategyRunRepository(Base):"), "a definition is not a reach"
