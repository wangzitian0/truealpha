"""Verify deployed Python packages carry no fixture modules."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gate

REPO = Path(__file__).resolve().parents[3]
DEPLOYED_SRC_ROOTS = (
    REPO / "apps" / "llm-service" / "src",
    REPO / "apps" / "data-engine" / "src",
    REPO / "apps" / "app-web" / "src",
    REPO / "common" / "contracts" / "src",
    REPO / "common" / "factors" / "src",
    REPO / "common" / "runtime" / "src",
)


@pytest.mark.criterion("R5.E2")
def test_no_deployed_source_tree_contains_a_fixture_module() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for root in DEPLOYED_SRC_ROOTS
        for path in root.rglob("*")
        if path.is_file() and "fixture" in path.name.lower()
    ]
    assert not offenders, f"fixture modules shipped under deployed source roots: {offenders}"
