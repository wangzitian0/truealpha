"""Validate GoalCriteriaCatalog JSON Schema structure and model invariants (#28 D1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from truealpha_contracts.goal_criteria import (
    GoalCriteriaCatalog,
    load_goal_criteria,
)

pytestmark = pytest.mark.contract


def _payload() -> dict:
    return load_goal_criteria().model_dump(mode="json")


def _with(criteria: list[dict]) -> GoalCriteriaCatalog:
    return GoalCriteriaCatalog.model_validate({**_payload(), "criteria": criteria})


def test_goal_criteria_catalog_validates() -> None:
    catalog = load_goal_criteria()
    assert len(catalog.criteria) > 0
    assert "R2.M0" in catalog.by_id()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "R3.A1"}, "uses the letter C"),
        ({"root": "#1417"}, "is not #1419"),
        ({"tier": "M0"}, "tier M0 is not M1"),
        ({"status": "backed"}, "must not name pending_on"),
        ({"pending_on": None}, "must name pending_on"),
        ({"oracle": "self"}, "oracle"),
        ({"oracle": "owner"}, "oracle"),
    ],
)
def test_a_malformed_criterion_is_refused(change: dict, message: str) -> None:
    criteria = _payload()["criteria"]
    target = next(c for c in criteria if c["id"] == "R3.C6")
    target.update(change)
    with pytest.raises(ValidationError, match=message):
        _with(criteria)


def test_a_duplicate_id_and_a_missing_m0_are_refused() -> None:
    criteria = _payload()["criteria"]
    with pytest.raises(ValidationError, match="named twice"):
        _with([*criteria, criteria[0]])
    with pytest.raises(ValidationError, match="R2 has no M0"):
        _with([c for c in criteria if c["id"] != "R2.M0"])
