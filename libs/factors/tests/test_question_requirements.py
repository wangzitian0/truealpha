"""#748 acceptance (1): every question maps to registered factor columns (or is explicitly
missing with an owning issue), every column's factor carries the module it claims, and the
registry sha moves when a mapping moves."""

from __future__ import annotations

# Importing a factor module registers it; the registry is populated by import, not discovery.
import factors.base.etf_virtual_company  # noqa: F401
import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
from factors.registry import FACTOR_REGISTRY
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    QUESTION_REQUIREMENTS_SHA256,
    FactorColumn,
    Question,
    QuestionRequirement,
    QuestionScope,
    requirements_sha256,
)


def test_every_question_is_registered_once() -> None:
    assert [item.question for item in QUESTION_REQUIREMENTS] == list(Question)


def test_every_column_names_a_registered_factor_with_its_module() -> None:
    for item in QUESTION_REQUIREMENTS:
        for column in item.columns:
            spec = FACTOR_REGISTRY.get(column.factor)
            assert spec is not None, f"{item.question}: {column.factor} is not a registered factor"
            assert spec.module == column.module, f"{column.factor}: registry module {spec.module} != {column.module}"


def test_a_question_without_a_column_names_the_issue_that_owns_it() -> None:
    for item in QUESTION_REQUIREMENTS:
        if not item.has_column:
            assert item.tracking_issue.startswith("#")


def test_the_registry_sha_moves_with_the_mapping() -> None:
    assert requirements_sha256() == QUESTION_REQUIREMENTS_SHA256
    changed = tuple(
        QuestionRequirement(
            item.question,
            item.columns + (FactorColumn("mart.x", "y", "peg", 1),),
            item.standards,
            item.tracking_issue,
        )
        if item.question is Question.Q6_THEME_PURITY
        else item
        for item in QUESTION_REQUIREMENTS
    )
    assert requirements_sha256(changed) != QUESTION_REQUIREMENTS_SHA256


def test_universe_scoping_is_explicit() -> None:
    peg = next(c for item in QUESTION_REQUIREMENTS for c in item.columns if c.factor == "peg")
    assert peg.applies_to("universe:topt-us-2026-03-31") and not peg.applies_to("universe:qqq-us-2026-06-30")
    gppe = next(c for item in QUESTION_REQUIREMENTS for c in item.columns if c.factor == "gross_profit_per_employee")
    assert gppe.applies_to("universe:qqq-us-2026-06-30")


def test_a_questions_scope_names_whose_question_it_is() -> None:
    """#748's classifier counts one subject per question; the scope says which. q5 asks
    about a FUND, and counting it per issuer would either grade twenty issuers
    `unavailable:no_row` (a red about the registry, not the data) or broadcast one fund row
    to twenty `answered`."""
    by_question = {item.question: item for item in QUESTION_REQUIREMENTS}
    assert by_question[Question.Q5_ETF_VIRTUAL_COMPANY].scope is QuestionScope.FUND
    for question in Question:
        if question is not Question.Q5_ETF_VIRTUAL_COMPANY:
            assert by_question[question].scope is QuestionScope.ISSUER, (
                f"{question} changed subject without this test being told"
            )


def test_scope_defaults_to_issuer_so_existing_entries_are_unchanged() -> None:
    """The type existing must not restate every registered question. A default of anything
    else would silently re-scope q1-q4 and q6 the day it landed."""
    assert QuestionRequirement(Question.Q1_MODEL_LEVERAGE, (), (), "#0").scope is QuestionScope.ISSUER


def test_the_registry_sha_moves_when_a_scope_moves() -> None:
    """A report is comparable to another only under the same expectation, and the subject a
    question is counted over is part of that expectation."""
    rescoped = tuple(
        QuestionRequirement(item.question, item.columns, item.standards, item.tracking_issue, QuestionScope.FUND)
        if item.question is Question.Q1_MODEL_LEVERAGE
        else item
        for item in QUESTION_REQUIREMENTS
    )
    assert requirements_sha256(rescoped) != QUESTION_REQUIREMENTS_SHA256
