"""SSOT loader for the goal-criteria catalog (#28 D1, #1747)."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

GOAL_CRITERIA_RESOURCE = "goal_criteria.v1.json"

#: Root number -> (root issue reference, acceptance letter suffix).
ROOTS: Mapping[int, tuple[str, str]] = {
    1: ("#1417", "A"),
    2: ("#1418", "B"),
    3: ("#1419", "C"),
    4: ("#1420", "D"),
    5: ("#1421", "E"),
}

_ID = re.compile(r"^R(?P<root>[1-5])\.(?:(?P<letter>[A-E])(?P<number>[1-9][0-9]?)|M0)$")

Tier = Literal["M0", "M1"]
Oracle = Literal["repository", "filing", "second_vendor", "label", "platform", "owner"]
Status = Literal["backed", "partial", "pending"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GoalCriterion(_Frozen):
    """Single goal acceptance criterion and its verification binding."""

    id: str
    root: str
    tier: Tier
    statement: str = Field(min_length=1)
    threshold: str | None = Field(default=None, min_length=1)
    window: str | None = Field(default=None, min_length=1)
    oracle: Oracle
    status: Status
    verdict_keys: tuple[str, ...] = ()
    pending_on: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _id_names_its_root_and_tier(self) -> Self:
        match = _ID.match(self.id)
        if match is None:
            raise ValueError(f"{self.id!r} is not R<root>.<letter><number> or R<root>.M0")
        issue, letter = ROOTS[int(match["root"])]
        if self.root != issue:
            raise ValueError(f"{self.id}: root {self.root} is not {issue}")
        if match["letter"] is not None and match["letter"] != letter:
            raise ValueError(f"{self.id}: root {issue} uses the letter {letter}")
        expected_tier = "M0" if match["letter"] is None else "M1"
        if self.tier != expected_tier:
            raise ValueError(f"{self.id}: tier {self.tier} is not {expected_tier}")
        if (self.status == "backed") == (self.pending_on is not None):
            raise ValueError(
                f"{self.id}: a {self.status} criterion {'must' if self.pending_on is None else 'must not'} name pending_on"
            )
        if len(set(self.verdict_keys)) != len(self.verdict_keys):
            raise ValueError(f"{self.id}: a verdict key is named twice")
        return self


class GoalCriteriaCatalog(_Frozen):
    """Validated root acceptance catalog loaded from package data."""

    schema_version: Literal[1]
    catalog_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    criteria: tuple[GoalCriterion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique_and_every_root_has_one_m0(self) -> Self:
        ids = [criterion.id for criterion in self.criteria]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"criterion ids named twice: {duplicates}")
        for number in ROOTS:
            if f"R{number}.M0" not in ids:
                raise ValueError(f"root R{number} has no M0 criterion")
        return self

    def by_id(self) -> dict[str, GoalCriterion]:
        return {criterion.id: criterion for criterion in self.criteria}


def goal_criteria_path() -> Path:
    """Return the filesystem path of the bundled goal-criteria contract."""
    return Path(str(resources.files("truealpha_contracts.data") / GOAL_CRITERIA_RESOURCE))


@functools.cache
def load_goal_criteria() -> GoalCriteriaCatalog:
    """Load and validate the bundled goal-criteria catalog."""
    raw = resources.files("truealpha_contracts.data").joinpath(GOAL_CRITERIA_RESOURCE).read_bytes()
    return GoalCriteriaCatalog.model_validate(json.loads(raw))


__all__ = [
    "GOAL_CRITERIA_RESOURCE",
    "GoalCriteriaCatalog",
    "GoalCriterion",
    "Oracle",
    "ROOTS",
    "Status",
    "Tier",
    "goal_criteria_path",
    "load_goal_criteria",
]
