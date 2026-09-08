"""#748 acceptance (1): every question maps to registered factor columns (or is explicitly
missing with an owning issue), every column's factor carries the module it claims, and the
registry sha moves when a mapping moves."""

from __future__ import annotations

# Importing a factor module registers it; the registry is populated by import, not discovery.
import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
from factors.registry import FACTOR_REGISTRY
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    QUESTION_REQUIREMENTS_SHA256,
    FactorColumn,
    Question,
    QuestionRequirement,
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
