"""The six init.md §0 questions, each bound to the factor columns that answer it (#748, rule 24).

Rule 24: strategy quality review starts from EXPECTED data, not successful outputs. This
registry is the expected side — per question, which wide-row columns answer it and which
`MetricStandard`s those columns consume — so the weekly coverage report can left-join the
row's §8 status dimensions and say `answered` / `unavailable:<reason>` / `missing` for every
(question, issuer). A question with no column is `missing` by construction, tracked by the
issue named here; it can never be quietly counted as unavailable.

Content-addressed: `QUESTION_REQUIREMENTS_SHA256` changes whenever a mapping changes, and the
report carries it, so two reports are comparable only under the same expectation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from types import MappingProxyType

from truealpha_contracts.common import canonical_sha256


class QuestionScope(StrEnum):
    """Whose question it is — which decides the report's denominator.

    Every question was implicitly `ISSUER` until #748's classifier met one that is not.
    q5 asks whether an ETF looks like a healthy company; its subject is the FUND. Counting
    it per issuer would either grade every issuer `unavailable:no_row` (a red describing the
    registry, not the data) or broadcast one fund row to twenty `answered` — the inflated
    numerator rule 24 exists to prevent.

    `ISSUER` stays the default, so every existing entry and every existing count is
    unchanged by this type existing. The same choice is coming for q4 (per analyst) and
    q3 (per supply-chain edge).
    """

    ISSUER = "issuer"
    FUND = "fund"


class Question(StrEnum):
    Q1_MODEL_LEVERAGE = "q1"
    Q2_VALUATION_VS_GROWTH = "q2"
    Q3_SUPPLY_CHAIN_EXPOSURE = "q3"
    Q4_ANALYST_TRACK_RECORD = "q4"
    Q5_ETF_VIRTUAL_COMPANY = "q5"
    Q6_THEME_PURITY = "q6"


QUESTION_TEXT: MappingProxyType[Question, str] = MappingProxyType(
    {
        Question.Q1_MODEL_LEVERAGE: "Is this company actually leveraged by large models?",
        Question.Q2_VALUATION_VS_GROWTH: "Is the current valuation reasonable relative to growth?",
        Question.Q3_SUPPLY_CHAIN_EXPOSURE: "What is this company exposed to, up and down its supply chain?",
        Question.Q4_ANALYST_TRACK_RECORD: "Is a given analyst's track record worth trusting?",
        Question.Q5_ETF_VIRTUAL_COMPANY: 'Does an ETF/portfolio "look like" a healthy company when treated as one?',
        Question.Q6_THEME_PURITY: 'Who\'s the "purest" name under a given theme?',
    }
)


@dataclass(frozen=True)
class FactorColumn:
    """One wide-row column that answers a question: where it lives, which registered factor
    writes it, and which universes it exists for (empty = every governed universe)."""

    table: str
    column: str
    factor: str
    module: int
    universe_prefixes: tuple[str, ...] = ()

    def applies_to(self, universe_id: str) -> bool:
        return not self.universe_prefixes or any(universe_id.startswith(p) for p in self.universe_prefixes)


@dataclass(frozen=True)
class QuestionRequirement:
    question: Question
    columns: tuple[FactorColumn, ...]
    standards: tuple[str, ...]
    tracking_issue: str
    #: The subject the report counts. Defaults to ISSUER, which is what every question
    #: assumed before #748 met one whose subject is a fund.
    scope: QuestionScope = QuestionScope.ISSUER

    @property
    def has_column(self) -> bool:
        return bool(self.columns)


QUESTION_REQUIREMENTS: tuple[QuestionRequirement, ...] = (
    QuestionRequirement(
        question=Question.Q1_MODEL_LEVERAGE,
        columns=(FactorColumn("mart.topt_gppe_results", "gppe", "gross_profit_per_employee", 2),),
        standards=("employees_total",),
        tracking_issue="#528",
    ),
    QuestionRequirement(
        question=Question.Q2_VALUATION_VS_GROWTH,
        # PEG is materialized by the strategy run, which exists for the curated TOPT universe only.
        columns=(FactorColumn("mart.strategy_decisions", "peg", "peg", 1, ("universe:topt-",)),),
        standards=(),
        tracking_issue="#284",
    ),
    QuestionRequirement(Question.Q3_SUPPLY_CHAIN_EXPOSURE, (), (), "#772"),
    QuestionRequirement(Question.Q4_ANALYST_TRACK_RECORD, (), (), "#771"),
    QuestionRequirement(
        question=Question.Q5_ETF_VIRTUAL_COMPANY,
        # Module 5 writes one row per (run, fund); the QQQ tick is the only one that
        # consolidates, because a fund can only be valued by the universe that IS its
        # holdings (`UniverseTick.consolidate_funds`).
        columns=(
            FactorColumn(
                "mart.fund_virtual_company",
                "weighted_valuation_gap",
                "etf_virtual_company",
                5,
                ("universe:qqq-",),
            ),
        ),
        standards=(),
        scope=QuestionScope.FUND,
        tracking_issue="#36",
    ),
    QuestionRequirement(Question.Q6_THEME_PURITY, (), (), "#772"),
)


def requirements_payload(requirements: tuple[QuestionRequirement, ...] = QUESTION_REQUIREMENTS) -> list[dict]:
    return [
        {
            "question": item.question.value,
            "columns": [asdict(column) for column in item.columns],
            "standards": list(item.standards),
            "scope": item.scope.value,
            "tracking_issue": item.tracking_issue,
        }
        for item in requirements
    ]


def requirements_sha256(requirements: tuple[QuestionRequirement, ...] = QUESTION_REQUIREMENTS) -> str:
    return canonical_sha256({"question_requirements": requirements_payload(requirements)})


QUESTION_REQUIREMENTS_SHA256 = requirements_sha256()
