"""The six init.md §0 questions bound to the factor columns that answer them."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from types import MappingProxyType

from truealpha_contracts.common import canonical_sha256, load_data_json
from truealpha_contracts.environment_contract import UniverseRole
from truealpha_contracts.universes import record, universe_of


class QuestionScope(StrEnum):
    ISSUER = "issuer"
    FUND = "fund"


class Question(StrEnum):
    Q1_MODEL_LEVERAGE = "q1"
    Q2_VALUATION_VS_GROWTH = "q2"
    Q3_SUPPLY_CHAIN_EXPOSURE = "q3"
    Q4_ANALYST_TRACK_RECORD = "q4"
    Q5_ETF_VIRTUAL_COMPANY = "q5"
    Q6_THEME_PURITY = "q6"


@dataclass(frozen=True)
class FactorColumn:
    table: str
    column: str
    factor: str
    module: int
    universe_role: UniverseRole | None = None

    def applies_to(self, universe_id: str) -> bool:
        return self.universe_role is None or self.universe_role in record(universe_of(universe_id)).roles


@dataclass(frozen=True)
class QuestionRequirement:
    question: Question
    columns: tuple[FactorColumn, ...]
    standards: tuple[str, ...]
    tracking_issue: str
    scope: QuestionScope = QuestionScope.ISSUER

    @property
    def has_column(self) -> bool:
        return bool(self.columns)


_REG = load_data_json("registries.v1.json")
QUESTION_TEXT: MappingProxyType[Question, str] = MappingProxyType(
    {Question(k): v for k, v in _REG["question_text"].items()}
)
QUESTION_REQUIREMENTS: tuple[QuestionRequirement, ...] = tuple(
    QuestionRequirement(
        Question(i["question"]),
        tuple(FactorColumn(**c) for c in i["columns"]),
        tuple(i["standards"]),
        i["tracking_issue"],
        QuestionScope(i["scope"]),
    )
    for i in _REG["question_requirements"]
)


def requirements_payload(requirements: tuple[QuestionRequirement, ...] = QUESTION_REQUIREMENTS) -> list[dict]:
    cols = lambda r: [asdict(c) for c in r.columns]  # noqa: E731
    return [
        {
            "question": r.question.value,
            "columns": cols(r),
            "standards": list(r.standards),
            "scope": r.scope.value,
            "tracking_issue": r.tracking_issue,
        }
        for r in requirements
    ]


def requirements_sha256(requirements: tuple[QuestionRequirement, ...] = QUESTION_REQUIREMENTS) -> str:
    return canonical_sha256({"question_requirements": requirements_payload(requirements)})


QUESTION_REQUIREMENTS_SHA256 = requirements_sha256()
