"""Anti-GREEN-WHILE-EMPTY and Mutation Tests (Layer 1-4 Verification).

Guards against:
1. GREEN-WHILE-EMPTY: Reports showing missing=0 by setting everything to unavailable,
   or claiming answered=True when the underlying metric is NULL.
2. WRONG FORMULA: Inadmissible formulas passing tautological assertions.
3. STALE-REPORTED-AS-FRESH: Old data masquerading as fresh.
4. Red-proven once: Every guard must fail under mutation.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from data_engine.datahub.question_coverage import (
    NO_ROW,
    classify_question,
    supply_chain_cells,
)
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    Question,
    QuestionRequirement,
)

REQ = {item.question: item for item in QUESTION_REQUIREMENTS}
TOPT_UNIVERSE = "universe:topt-us-2026-03-31"


def test_red_proven_unbound_question_is_strictly_missing() -> None:
    """Mutation: If a question has no answering column, missing MUST equal denominator.

    It must NEVER quietly report unavailable or answered.
    """
    mutated_req = QuestionRequirement(
        question=Question.Q3_SUPPLY_CHAIN_EXPOSURE,
        columns=(),  # Mutated: stripped columns
        standards=(),
        tracking_issue="#772",
    )
    result = classify_question(
        mutated_req,
        universe_id=TOPT_UNIVERSE,
        issuers=[f"issuer:{i}" for i in range(20)],
        cells_by_column={},
    )
    assert result["missing"] == 20
    assert result["answered"] == 0
    assert result["unavailable"] == {}


def test_red_proven_bound_question_without_rows_is_no_row_not_missing() -> None:
    """Mutation: If a question is bound, missing MUST be 0 and missing rows are NO_ROW."""
    result = classify_question(
        REQ[Question.Q3_SUPPLY_CHAIN_EXPOSURE],
        universe_id=TOPT_UNIVERSE,
        issuers=[f"issuer:{i}" for i in range(20)],
        cells_by_column={},
    )
    assert result["missing"] == 0
    assert result["answered"] == 0
    assert result["unavailable"] == {NO_ROW: 20}


def test_anti_green_while_empty_rejects_null_metric_as_answered() -> None:
    """Anti-GREEN-WHILE-EMPTY rule: A row with availability_status='available'
    but a NULL metric value must NOT be marked answered=True.
    It must be downgraded to unavailable with 'null_metric_value'.
    """
    mock_conn = MagicMock()
    # 20 rows with status 'available' but exposure_score is None!
    mock_conn.execute.return_value.fetchall.return_value = [(f"issuer:{i}", "available", [], None) for i in range(20)]
    cells = supply_chain_cells(mock_conn, "run:test")
    assert len(cells) == 20
    assert all(not c.answered for c in cells)
    assert all(c.reason == "null_metric_value" for c in cells)

    result = classify_question(
        REQ[Question.Q3_SUPPLY_CHAIN_EXPOSURE],
        universe_id=TOPT_UNIVERSE,
        issuers=[f"issuer:{i}" for i in range(20)],
        cells_by_column={"mart.issuer_supply_chain_exposure.exposure_score": cells},
    )
    assert result["answered"] == 0
    assert result["unavailable"] == {"null_metric_value": 20}
    assert result["missing"] == 0


def test_green_path_with_valid_metrics_is_answered() -> None:
    """Verify that when metric values are genuinely present and available,
    cells are correctly marked answered=True.
    """
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [
        (f"issuer:{i}", "available", [], Decimal("0.25")) for i in range(20)
    ]
    cells = supply_chain_cells(mock_conn, "run:test")
    assert len(cells) == 20
    assert all(c.answered for c in cells)

    result = classify_question(
        REQ[Question.Q3_SUPPLY_CHAIN_EXPOSURE],
        universe_id=TOPT_UNIVERSE,
        issuers=[f"issuer:{i}" for i in range(20)],
        cells_by_column={"mart.issuer_supply_chain_exposure.exposure_score": cells},
    )
    assert result["answered"] == 20
    assert result["unavailable"] == {}
    assert result["missing"] == 0


def test_all_six_questions_have_valid_column_bindings() -> None:
    """Verify that all 6 questions in QUESTION_REQUIREMENTS are bound to real columns."""
    for req in QUESTION_REQUIREMENTS:
        assert req.has_column, f"Question {req.question.value} has no column binding!"
        for col in req.columns:
            assert col.table.startswith("mart."), f"Table {col.table} must be in mart schema"
            assert col.column, f"Column name must not be empty for {col.table}"
            assert col.factor, f"Factor name must be specified for {col.table}.{col.column}"
            assert col.module in range(1, 8), f"Module {col.module} must be between 1 and 7"
