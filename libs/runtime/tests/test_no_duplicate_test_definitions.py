"""#1089: a test module defines each top-level function and class once.

Python keeps the last definition of a repeated name. The earlier copy is dead code.
A repeated `def test_x` silently drops a test, and a green test count does not show it.
`test_repository.py` carried two copies of two helpers (#1082).

Static and stdlib-only, over the real tree. The check imports no scanned module.
Nested names are out of scope: `@property` and `@x.setter` repeat a method name by design.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: Every Python test tree, and the root conftest. A new `apps/<x>/tests` joins automatically.
TEST_ROOT_PATTERNS = ("apps/*/tests", "libs/*/tests")
ROOT_FILES = ("conftest.py",)


def _scanned_files() -> list[Path]:
    files = [REPO / name for name in ROOT_FILES]
    for pattern in TEST_ROOT_PATTERNS:
        for root in sorted(REPO.glob(pattern)):
            files.extend(sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts))
    return files


def repeated_definitions(source: str, label: str) -> list[str]:
    """Return one message for each module-level name that `source` defines more than once."""
    seen: dict[str, list[int]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            seen.setdefault(node.name, []).append(node.lineno)
    return [
        f"{label}: `{name}` is defined at lines {', '.join(map(str, lines))}; the last one wins"
        for name, lines in seen.items()
        if len(lines) > 1
    ]


def test_no_test_module_defines_a_top_level_name_twice() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        label = path.relative_to(REPO).as_posix()
        offenders.extend(repeated_definitions(path.read_text(encoding="utf-8"), label))
    assert not offenders, "a later definition shadows an earlier one:\n" + "\n".join(offenders)


def test_the_scan_is_not_vacuous() -> None:
    labels = {path.relative_to(REPO).as_posix() for path in _scanned_files()}
    for expected in (
        "conftest.py",
        "apps/data-engine/tests/datahub/test_repository.py",
        "libs/runtime/tests/test_no_duplicate_test_definitions.py",
        "libs/contracts/tests/test_content_addressing_is_shared.py",
    ):
        assert expected in labels, f"the scan misses {expected}"
    assert len(labels) > 100, f"the scan covers {len(labels)} files; the trees are not where this test thinks"


def test_the_detector_reports_file_name_and_every_line() -> None:
    source = "def helper():\n    pass\n\n\nclass Fake:\n    pass\n\n\ndef helper():\n    pass\n"
    assert repeated_definitions(source, "x/test_a.py") == [
        "x/test_a.py: `helper` is defined at lines 1, 9; the last one wins"
    ]


def test_the_detector_covers_async_functions_and_classes() -> None:
    source = "async def run():\n    pass\n\n\nasync def run():\n    pass\n\n\nclass Box:\n    pass\n\n\nclass Box:\n    pass\n"
    messages = repeated_definitions(source, "x/test_b.py")
    assert [message.split("`")[1] for message in messages] == ["run", "Box"]


def test_the_detector_ignores_nested_and_distinct_names() -> None:
    source = (
        "def one():\n    pass\n\n\n"
        "def two():\n    pass\n\n\n"
        "class First:\n    def method(self):\n        pass\n\n\n"
        "class Second:\n    def method(self):\n        pass\n"
    )
    assert repeated_definitions(source, "x/test_c.py") == []
