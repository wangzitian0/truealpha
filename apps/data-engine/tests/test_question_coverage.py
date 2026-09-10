"""#748: expected cells (question registry) left-joined with observed status rows."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.question_coverage import (
    NO_ROW,
    UNRECORDED_REASON,
    Cell,
    classify_question,
    gppe_cells,
    peg_cells,
    persist,
    summary_line,
)
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    QUESTION_REQUIREMENTS_SHA256,
    Question,
)

TOPT = "universe:topt-us-2026-03-31"
QQQ = "universe:qqq-us-2026-06-30"
REQ = {item.question: item for item in QUESTION_REQUIREMENTS}


def test_a_question_with_a_column_counts_answered_unavailable_by_reason_and_no_row() -> None:
    cells = (
        Cell("issuer:a", True),
        Cell("issuer:b", False, "missing_headcount"),
        Cell("issuer:c", False, "missing_headcount"),
        Cell("issuer:d", False, "missing_gross_profit"),
    )
    entry = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=QQQ,
        issuers=["issuer:a", "issuer:b", "issuer:c", "issuer:d", "issuer:e"],
        cells_by_column={"mart.topt_gppe_results.gppe": cells},
    )
    assert entry["column"] == "mart.topt_gppe_results.gppe"
    assert entry["answered"] == 1 and entry["missing"] == 0 and entry["denominator"] == 5
    # sorted by count desc then reason; the issuer with no row is the left-join's honest gap
    assert entry["unavailable"] == {"missing_headcount": 2, "missing_gross_profit": 1, NO_ROW: 1}


def test_removing_the_left_join_is_red() -> None:
    """Red-proof (#748 acceptance 2): an expected issuer the join cannot find is never counted
    as answered, and a question the registry does not bind is `missing`, not `unavailable`."""
    entry = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=QQQ,
        issuers=["issuer:a"],
        cells_by_column={},
    )
    assert entry["answered"] == 0 and entry["unavailable"] == {NO_ROW: 1}
    unbound = classify_question(
        REQ[Question.Q3_SUPPLY_CHAIN_EXPOSURE], universe_id=QQQ, issuers=["issuer:a", "issuer:b"], cells_by_column={}
    )
    assert unbound["missing"] == 2 and unbound["column"] is None and unbound["tracking_issue"] == "#772"


def test_peg_applies_to_topt_only() -> None:
    peg = REQ[Question.Q2_VALUATION_VS_GROWTH]
    cells = {"mart.strategy_decisions.peg": (Cell("issuer:a", True), Cell("issuer:b", False, "excluded:financial"))}
    topt = classify_question(peg, universe_id=TOPT, issuers=["issuer:a", "issuer:b"], cells_by_column=cells)
    qqq = classify_question(peg, universe_id=QQQ, issuers=["issuer:a", "issuer:b"], cells_by_column=cells)
    assert topt["answered"] == 1 and topt["unavailable"] == {"excluded:financial": 1}
    assert qqq["missing"] == 2 and qqq["column"] is None


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self.rows


def test_gppe_cells_fall_back_to_availability_for_rows_written_before_the_status_columns() -> None:
    rows = [
        ("issuer:a", "available", "available", []),
        ("issuer:b", None, "available", []),
        ("issuer:c", None, "unavailable", ["missing_headcount", "missing_total_assets"]),
        ("issuer:d", "stale", "available", ["stale_input"]),
    ]
    cells = gppe_cells(_Rows(rows), "run")
    assert [c.answered for c in cells] == [True, True, False, False]
    assert cells[2].reason == "missing_headcount" and cells[3].reason == "stale_input"


def test_peg_cells_name_the_exclusion_or_admit_the_reason_is_unrecorded() -> None:
    rows = [
        ("issuer:a", "1.2", "available", None),
        ("issuer:b", None, "excluded", "financial_branch"),
        ("issuer:c", None, "available", None),
    ]
    cells = peg_cells(_Rows(rows), cutoff=datetime(2026, 9, 8, tzinfo=UTC))
    assert cells[0].answered and cells[1].reason == "excluded:financial_branch" and cells[2].reason == UNRECORDED_REASON


def _connection():
    try:
        return psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            raise
        pytest.skip("no local Postgres")


def test_the_report_persists_append_only_and_reads_back() -> None:
    connection = _connection()
    try:
        report = {
            "universe": "topt",
            "universe_id": TOPT,
            "run_id": "capture-run:" + "0" * 64,
            "cutoff": datetime(2026, 9, 8, tzinfo=UTC).isoformat(),
            "environment": "test",
            "requirements_sha256": QUESTION_REQUIREMENTS_SHA256,
            "generated_at": datetime(2026, 9, 8, 9, 7, tzinfo=UTC).isoformat(),
            "denominator": 1,
            "questions": {
                q.value: classify_question(REQ[q], universe_id=TOPT, issuers=["issuer:a"], cells_by_column={})
                for q in Question
            },
        }
        report_id = persist(connection, report)
        assert persist(connection, report) == report_id  # idempotent under the same content
        row = connection.execute(
            "select universe_id, requirements_sha256, payload->'questions'->'q3'->>'missing' from mart.question_coverage_report where report_id = %s",
            (report_id,),
        ).fetchone()
        assert row == (TOPT, QUESTION_REQUIREMENTS_SHA256, "1")
        assert "q1: 0/1 answered" in summary_line(report)
    finally:
        connection.rollback()
        connection.close()


def test_a_question_bound_to_two_columns_is_answered_by_either() -> None:
    """Copilot on #779: with more than one answering column an issuer counts once, as
    answered when any column answers, with the first row's reason otherwise."""
    from truealpha_contracts.question_requirements import FactorColumn, QuestionRequirement

    two = QuestionRequirement(
        Question.Q1_MODEL_LEVERAGE,
        (FactorColumn("mart.a", "x", "gross_profit_per_employee", 2), FactorColumn("mart.b", "y", "peg", 1)),
        (),
        "#0",
    )
    entry = classify_question(
        two,
        universe_id=QQQ,
        issuers=["issuer:a", "issuer:b", "issuer:c"],
        cells_by_column={
            "mart.a.x": (Cell("issuer:a", False, "missing_headcount"), Cell("issuer:b", False, "missing_headcount")),
            "mart.b.y": (Cell("issuer:a", True),),
        },
    )
    assert entry["columns"] == ["mart.a.x", "mart.b.y"] and entry["column"] == "mart.a.x"
    assert entry["answered"] == 1
    assert entry["unavailable"] == {"missing_headcount": 1, NO_ROW: 1}


def test_a_fund_scoped_question_counts_funds_not_issuers() -> None:
    """q5 asks whether an ETF looks like a healthy company; its subject is the FUND (#36).

    The registry could not hold it at all before the scope existed: one fund row looked up
    under twenty issuer ids would grade twenty `unavailable:no_row` — a red describing the
    registry rather than the data — and broadcasting the row to twenty `answered` is the
    inflated numerator rule 24 exists to prevent. Both wrong answers are asserted against
    here by the denominator.
    """
    entry = classify_question(
        REQ[Question.Q5_ETF_VIRTUAL_COMPANY],
        universe_id=QQQ,
        issuers=[f"issuer:cik:{n}" for n in range(20)],
        funds=["etf:series:S000101292"],
        cells_by_column={"mart.fund_virtual_company.weighted_valuation_gap": (Cell("etf:series:S000101292", True),)},
    )
    assert entry["scope"] == "fund"
    assert entry["denominator"] == 1, "twenty issuers must not become q5's denominator"
    assert entry["answered"] == 1 and entry["missing"] == 0 and entry["unavailable"] == {}


def test_a_refused_consolidation_is_unavailable_with_the_refusing_floor() -> None:
    """A fund whose coverage fell below the definition's floors has no aggregate. It must
    read as unavailable-with-a-reason, never as answered and never as missing — missing is
    reserved for "no column exists yet", which is a different problem with a different owner."""
    entry = classify_question(
        REQ[Question.Q5_ETF_VIRTUAL_COMPANY],
        universe_id=QQQ,
        issuers=[],
        funds=["etf:series:thin"],
        cells_by_column={
            "mart.fund_virtual_company.weighted_valuation_gap": (
                Cell("etf:series:thin", False, "valued_weight_below_minimum"),
            )
        },
    )
    assert entry["answered"] == 0 and entry["missing"] == 0
    assert entry["unavailable"] == {"valued_weight_below_minimum": 1}


def test_a_universe_whose_tick_does_not_consolidate_has_no_fund_subjects() -> None:
    """Only the universe that IS a fund's holdings consolidates it (`consolidate_funds` on
    the QQQ tick alone). On TOPT q5 is NOT APPLICABLE — no fund subject exists, so the
    denominator is 0 and nothing is answered, unavailable OR missing. `missing` would claim
    "a subject exists and no column covers it", which is a different problem with a different
    owner (review on #797). The assertions below say exactly that, and that TOPT must not
    borrow QQQ's fund to look answered."""
    entry = classify_question(
        REQ[Question.Q5_ETF_VIRTUAL_COMPANY],
        universe_id=TOPT,
        issuers=[f"issuer:lei:{n}" for n in range(20)],
        funds=[],
        cells_by_column={},
    )
    assert entry["columns"] == [], "the q5 column is scoped to universe:qqq-"
    assert entry["denominator"] == 0 and entry["answered"] == 0
    assert entry["missing"] == 0, "not applicable is not missing: there is no subject to be missing a column"
    assert entry["unavailable"] == {}, "and nothing is unavailable either — there is nothing to grade"


def test_issuer_scoped_questions_ignore_the_fund_subjects() -> None:
    """Red-proof for the scope switch itself: handing funds to an issuer-scoped question
    must not change its denominator. If the switch inverted, q1 would count 1 instead of 3."""
    entry = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=QQQ,
        issuers=["issuer:a", "issuer:b", "issuer:c"],
        funds=["etf:series:S000101292"],
        cells_by_column={"mart.topt_gppe_results.gppe": (Cell("issuer:a", True),)},
    )
    assert entry["scope"] == "issuer"
    assert entry["denominator"] == 3 and entry["answered"] == 1
