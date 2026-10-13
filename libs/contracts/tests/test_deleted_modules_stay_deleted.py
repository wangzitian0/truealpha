"""#1238: modules deleted from `truealpha_contracts` must not come back.

Each module below had no importer outside its own tests and the deleted cluster.
Nothing deployed used it, so the code only added review and merge cost.
`capture_contracts` and `readiness` left last: only the Issue 58 conformance export imported them.
This test fails when any listed module name is importable again.
Add a name to the list in the same change that deletes the module.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "truealpha_contracts"

DELETED_MODULES = (
    "gates",
    "policy_bundle",
    "market",
    "documents",
    "conversations",
    "capture_contracts",
    "readiness",
)


@pytest.mark.parametrize("name", DELETED_MODULES)
def test_deleted_module_is_not_importable(name: str) -> None:
    spec = importlib.util.find_spec(f"truealpha_contracts.{name}")
    assert spec is None, f"truealpha_contracts.{name} was deleted in #1238 and is importable again: {spec.origin}"


@pytest.mark.parametrize("name", DELETED_MODULES)
def test_deleted_module_has_no_source_file(name: str) -> None:
    assert not (PACKAGE_DIR / f"{name}.py").exists(), f"{name}.py was deleted in #1238 and exists again"
    assert not (PACKAGE_DIR / name).exists(), f"package {name}/ was deleted in #1238 and exists again"


def test_the_import_probe_finds_a_module_that_exists() -> None:
    """Positive control. A probe that finds nothing for every name would pass the tests above for any input."""
    assert importlib.util.find_spec("truealpha_contracts.access") is not None
    assert (PACKAGE_DIR / "access.py").exists()
