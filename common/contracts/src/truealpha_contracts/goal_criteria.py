"""The goal-criteria catalog: one source for the acceptance criteria of the five roots (#28 D1).

`goal_criteria.v1.json` holds every criterion of the root issues #1417 to #1421 with its root,
statement, tier (M0 "runs through" or M1 "goal reached"), threshold, window, backing checks and
oracle. Root issue tables and child issues refer to a criterion by its id. They do not restate
its threshold.

A backing check is a check that runs again:

- a verdict key: an entry of `tools/nightly_verdicts.json` (a runtime check inside an environment);
- a test marker: ``@pytest.mark.criterion("<id>")`` on a test.

`backing_findings` holds the catalog and the checks equal in both directions. A backed
criterion names at least one existing check. A pending criterion names none and names the
issue that builds its check. Every verdict key and every marker names a catalog id, so a check
that backs no criterion is visible.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping, Set
from importlib import resources
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

GOAL_CRITERIA_RESOURCE = "goal_criteria.v1.json"

#: Root number -> its root issue and the letter its acceptance ids use (#28 "Chain").
ROOTS: Mapping[int, tuple[str, str]] = {
    1: ("#1417", "A"),
    2: ("#1418", "B"),
    3: ("#1419", "C"),
    4: ("#1420", "D"),
    5: ("#1421", "E"),
}

_ID = re.compile(r"^R(?P<root>[1-5])\.(?:(?P<letter>[A-E])(?P<number>[1-9][0-9]?)|M0)$")

Tier = Literal["M0", "M1"]
#: Who wrote the oracle. #28 M1 condition 2 counts only verdicts whose oracle the repository
#: did not write: `filing`, `second_vendor`, `label`, `platform` (GitHub or host facts) and `owner`.
Oracle = Literal["repository", "filing", "second_vendor", "label", "platform", "owner"]
Status = Literal["backed", "partial", "pending"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GoalCriterion(_Frozen):
    id: str
    root: str
    tier: Tier
    statement: str = Field(min_length=1)
    threshold: str | None = Field(default=None, min_length=1)
    window: str | None = Field(default=None, min_length=1)
    oracle: Oracle
    status: Status
    verdict_keys: tuple[str, ...] = ()
    #: The issue that builds the missing check. Required when the status is not `backed`.
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


@functools.cache
def load_goal_criteria() -> GoalCriteriaCatalog:
    payload = json.loads(resources.files("truealpha_contracts.data").joinpath(GOAL_CRITERIA_RESOURCE).read_bytes())
    return GoalCriteriaCatalog.model_validate(payload)


def backing_findings(
    catalog: GoalCriteriaCatalog,
    *,
    verdict_keys: Set[str],
    markers: Mapping[str, Set[str]],
) -> list[str]:
    """Every disagreement between the catalog and the checks; an empty list means they agree.

    ``verdict_keys`` are the entries of `tools/nightly_verdicts.json`. ``markers`` maps each
    criterion id named by a ``criterion`` test marker to the test files that carry it.
    """
    findings: list[str] = []
    criteria = catalog.by_id()
    named_keys: set[str] = set()
    for criterion in catalog.criteria:
        named_keys.update(criterion.verdict_keys)
        for key in criterion.verdict_keys:
            if key not in verdict_keys:
                findings.append(f"{criterion.id}: verdict key {key!r} is not in nightly_verdicts.json")
        backed = bool(criterion.verdict_keys) or criterion.id in markers
        if criterion.status == "pending" and backed:
            findings.append(f"{criterion.id}: pending, but a check backs it; set the status to backed or partial")
        if criterion.status != "pending" and not backed:
            findings.append(f"{criterion.id}: {criterion.status}, but no verdict key or test marker backs it")
    for key in sorted(verdict_keys - named_keys):
        findings.append(f"verdict key {key!r} backs no catalog criterion")
    for criterion_id in sorted(set(markers) - set(criteria)):
        files = ", ".join(sorted(markers[criterion_id]))
        findings.append(f"test marker criterion({criterion_id!r}) names no catalog criterion ({files})")
    return findings
