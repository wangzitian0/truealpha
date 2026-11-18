"""#1664 Slice 4 guard: legacy `factors.production_topt` and `mart.topt_*` relations remain retired."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LEGACY_RELATIONS = (
    "topt_gppe_invocations",
    "topt_gppe_results",
    "topt_core_invocations",
    "topt_core_results",
    "topt_core_meta_info",
    "topt_core_result_read",
)
_LEGACY_PATTERN = re.compile(r"\b(?:" + "|".join(LEGACY_RELATIONS) + r"|production_topt)\b")


def test_legacy_production_topt_package_remains_deleted() -> None:
    assert not (REPO_ROOT / "common" / "factors" / "src" / "factors" / "production_topt").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("factors.production_topt")


def test_baseline_schema_and_runtime_sources_exclude_retired_topt_relations() -> None:
    baseline = (REPO_ROOT / "db" / "migrations" / "0003_mart.sql").read_text(encoding="utf-8")
    assert _LEGACY_PATTERN.findall(baseline) == []

    for root in (REPO_ROOT / "apps", REPO_ROOT / "common"):
        for py in root.rglob("*.py"):
            rel = py.relative_to(REPO_ROOT).as_posix()
            if "/src/" not in rel:
                continue
            matches = _LEGACY_PATTERN.findall(py.read_text(encoding="utf-8"))
            assert matches == [], f"{rel} still references retired TOPT artifacts: {matches}"
