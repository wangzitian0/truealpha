"""#1229: this repository imports only public names from `infra2_sdk`.

A name that starts with `_` is private to the SDK. The SDK can rename or remove it in any
release. `checks.py` imported `_redact_error` while the public `redact_postgres_error` was
available. This guard scans the real tree and fails on each private import.

Static and stdlib-only. The scan imports no scanned module.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: Every Python tree that can import the SDK. A new `apps/<x>/src` joins automatically.
SCAN_PATTERNS = ("apps/*/src", "apps/*/tests", "libs/*/src", "libs/*/tests", "tools")


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCAN_PATTERNS:
        for root in sorted(REPO.glob(pattern)):
            files.extend(sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts))
    return files


def _is_private(name: str) -> bool:
    """A leading underscore marks a private name. A dunder such as `__version__` is public."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _dotted(node: ast.expr) -> list[str]:
    """The dotted parts of `a.b.c`, or an empty list when the chain does not start at a name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return []


def private_sdk_names(source: str, label: str) -> list[str]:
    """One message for each private `infra2_sdk` name that `source` imports or reads."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "infra2_sdk":
            module_parts = (node.module or "").split(".")
            found.extend(
                f"{label}:{node.lineno}: imports private module `{node.module}`"
                for part in module_parts
                if _is_private(part)
            )
            found.extend(
                f"{label}:{node.lineno}: imports private name `{alias.name}` from `{node.module}`"
                for alias in node.names
                if _is_private(alias.name)
            )
        elif isinstance(node, ast.Import):
            found.extend(
                f"{label}:{node.lineno}: imports private module `{alias.name}`"
                for alias in node.names
                if alias.name.split(".")[0] == "infra2_sdk" and any(_is_private(part) for part in alias.name.split("."))
            )
        elif isinstance(node, ast.Attribute):
            parts = _dotted(node)
            if parts[:1] == ["infra2_sdk"] and any(_is_private(part) for part in parts[1:]):
                found.append(f"{label}:{node.lineno}: reads private attribute `{'.'.join(parts)}`")
    return found


def test_no_file_imports_a_private_sdk_name() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        label = path.relative_to(REPO).as_posix()
        offenders.extend(private_sdk_names(path.read_text(encoding="utf-8"), label))
    assert not offenders, "use the public SDK name instead:\n" + "\n".join(offenders)


def test_the_detector_flags_each_private_shape() -> None:
    private_from_name = "from infra2_sdk.runtime.postgres import PostgresSettings, _redact_error\n"
    private_from_module = "from infra2_sdk._wire import encode\n"
    private_import = "import infra2_sdk._wire\n"
    private_attribute = "import infra2_sdk\nvalue = infra2_sdk.runtime.postgres._redact_error\n"
    assert len(private_sdk_names(private_from_name, "x.py")) == 1
    assert "`_redact_error`" in private_sdk_names(private_from_name, "x.py")[0]
    assert len(private_sdk_names(private_from_module, "x.py")) == 1
    assert len(private_sdk_names(private_import, "x.py")) == 1
    assert len(private_sdk_names(private_attribute, "x.py")) == 1


def test_the_detector_accepts_public_names() -> None:
    public = (
        "from infra2_sdk.runtime.postgres import redact_postgres_error\n"
        "from infra2_sdk import __version__\n"
        "from infra2_sdk.deploy import dispatch_and_wait as _sdk_dispatch_and_wait\n"
        "from other_package import _private_but_not_the_sdk\n"
    )
    assert private_sdk_names(public, "x.py") == []


def test_the_scan_is_not_vacuous() -> None:
    labels = {path.relative_to(REPO).as_posix() for path in _scanned_files()}
    for expected in (
        "libs/runtime/src/truealpha_runtime/checks.py",
        "libs/runtime/tests/test_public_sdk_names.py",
        "tools/health_check.py",
    ):
        assert expected in labels, f"the scan misses {expected}"
    importers = [
        path
        for path in _scanned_files()
        if any(
            isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "infra2_sdk"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert len(importers) >= 10, f"only {len(importers)} files import infra2_sdk; the scan roots drifted"
