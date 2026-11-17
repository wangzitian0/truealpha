"""The six init.md §0 questions bound to the factor columns that answer them (#748, rule 24)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from types import MappingProxyType

from truealpha_contracts.common import canonical_sha256
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


def _qr(
    question: Question,
    col: FactorColumn,
    tracking_issue: str,
    standards: tuple[str, ...] = (),
    scope: QuestionScope = QuestionScope.ISSUER,
) -> QuestionRequirement:
    return QuestionRequirement(
        question=question, columns=(col,), standards=standards, tracking_issue=tracking_issue, scope=scope
    )


QUESTION_REQUIREMENTS: tuple[QuestionRequirement, ...] = (
    _qr(
        Question.Q1_MODEL_LEVERAGE,
        FactorColumn("mart.topt_gppe_results", "gppe", "gross_profit_per_employee", 2),
        "#528",
        ("employees_total",),
    ),
    _qr(Question.Q2_VALUATION_VS_GROWTH, FactorColumn("mart.strategy_decisions", "peg", "peg", 1, "strategy"), "#284"),
    _qr(
        Question.Q3_SUPPLY_CHAIN_EXPOSURE,
        FactorColumn("mart.issuer_supply_chain_exposure", "exposure_score", "supply_chain_exposure", 3),
        "#772",
    ),
    _qr(
        Question.Q4_ANALYST_TRACK_RECORD,
        FactorColumn("mart.issuer_analyst_ratings", "consensus_rating", "analyst_track_record", 4),
        "#771",
    ),
    _qr(
        Question.Q5_ETF_VIRTUAL_COMPANY,
        FactorColumn(
            "mart.fund_virtual_company", "weighted_valuation_gap", "etf_virtual_company", 5, "fund_consolidation"
        ),
        "#36",
        scope=QuestionScope.FUND,
    ),
    _qr(
        Question.Q6_THEME_PURITY,
        FactorColumn("mart.issuer_theme_purity", "theme_share", "theme_purity", 6),
        "#772",
        ("segment_revenue",),
    ),
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
