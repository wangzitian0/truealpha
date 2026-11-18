"""#1664 Slice 4 guard: legacy `factors.production_topt` package remains retired."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_LEGACY_ENGINE_PATTERN = re.compile(r"\bproduction_topt\b")


def test_legacy_production_topt_package_remains_deleted() -> None:
    assert not (REPO_ROOT / "common" / "factors" / "src" / "factors" / "production_topt").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("factors.production_topt")


def test_runtime_sources_exclude_legacy_production_topt_engine() -> None:
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common"):
        for py in root.rglob("*.py"):
            rel = py.relative_to(REPO_ROOT).as_posix()
            if "/src/" not in rel:
                continue
            matches = _LEGACY_ENGINE_PATTERN.findall(py.read_text(encoding="utf-8"))
            assert matches == [], f"{rel} still references retired production_topt: {matches}"
