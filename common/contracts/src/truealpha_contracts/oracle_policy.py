"""The versioned tolerances of the outside comparisons (#1523).

`oracle_policy.v1.json` holds one entry per comparator. The tolerance is data, not a literal in the
comparing code, and each row of `mart.oracle_outcomes` stores the tolerance it was judged by.
"""

from __future__ import annotations

import functools
import json
from decimal import Decimal
from importlib import resources
from typing import Any, Literal

from pydantic import Field, field_validator

from truealpha_contracts.common import FrozenModel

ORACLE_POLICY_RESOURCE = "oracle_policy.v1.json"


class ComparatorPolicy(FrozenModel):
    relative_tolerance: Decimal = Field(gt=0, lt=1)
    explanation: str = Field(min_length=1)

    @field_validator("relative_tolerance", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("a tolerance is a base-10 string, never a binary float")
        return value


class OraclePolicy(FrozenModel):
    schema_version: Literal[1]
    policy_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1)
    comparators: dict[str, ComparatorPolicy] = Field(min_length=1)

    def relative_tolerance(self, comparator: str) -> Decimal:
        """The relative tolerance of `comparator`; an unregistered comparator is a code defect."""
        return self.comparators[comparator].relative_tolerance


@functools.cache
def load_oracle_policy() -> OraclePolicy:
    payload = json.loads(resources.files("truealpha_contracts.data").joinpath(ORACLE_POLICY_RESOURCE).read_bytes())
    return OraclePolicy.model_validate(payload)
