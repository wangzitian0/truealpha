"""Sentinel known answers contract for DataHub oracle evaluation (#1520)."""

from __future__ import annotations

import functools
from decimal import Decimal
from importlib import resources
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator
from truealpha_contracts.common import StrictFrozenModel

SENTINEL_KNOWN_ANSWERS_RESOURCE = "sentinel_known_answers.v1.json"
_NE = Annotated[str, Field(min_length=1)]
_DT = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
_CIK = Annotated[str, Field(pattern=r"^issuer:cik:\d{10}$")]


class _AttrDict(dict[str, Any]):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _box(o: Any) -> Any:
    return (
        _AttrDict({k: _box(v) for k, v in o.items()})
        if type(o) is dict
        else (tuple(_box(x) for x in o) if isinstance(o, (list, tuple)) else o)
    )


class SentinelKnownAnswer(StrictFrozenModel):
    answer_id: _NE
    ticker: _NE
    issuer_id: _CIK
    cik: str = Field(pattern=r"^\d{10}$")
    metric: Literal["operating_branch", "gppe", "headcount", "revenue"]
    period_end: _DT
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    kind: Literal["exact", "band", "numeric"]
    expected_value: str | None = None
    lower_bound: str | None = None
    upper_bound: str | None = None
    relative_tolerance: str | None = None
    source_citation: _NE

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if (
            (self.kind == "exact" and not self.expected_value)
            or (
                self.kind == "band"
                and (
                    self.lower_bound is None
                    or self.upper_bound is None
                    or Decimal(self.lower_bound) > Decimal(self.upper_bound)
                )
            )
            or (
                self.kind == "numeric"
                and (
                    self.expected_value is None
                    or self.relative_tolerance is None
                    or Decimal(self.relative_tolerance) < Decimal(0)
                )
            )
        ):
            raise ValueError(f"{self.answer_id}: invalid bounds")
        return self


class SentinelKnownAnswersCatalog(StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: _NE
    description: _NE
    answers: tuple[SentinelKnownAnswer, ...] = Field(min_length=9)
    negative_theme_classifications: tuple[Any, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _wrap_neg(self) -> Self:
        object.__setattr__(
            self, "negative_theme_classifications", tuple(_box(dict(x)) for x in self.negative_theme_classifications)
        )
        return self


@functools.cache
def load_sentinel_known_answers() -> SentinelKnownAnswersCatalog:
    return SentinelKnownAnswersCatalog.model_validate_json(
        resources.files("truealpha_contracts.data")
        .joinpath(SENTINEL_KNOWN_ANSWERS_RESOURCE)
        .read_text(encoding="utf-8")
    )
