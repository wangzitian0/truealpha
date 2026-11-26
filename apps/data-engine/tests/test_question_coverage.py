"""#748: expected cells (question registry) left-joined with observed status rows."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.question_coverage import (
    NO_ROW,
    REPORT_COMPILER_VERSION,
    UNRECORDED_REASON,
    Cell,
    GovernedHead,
    analyst_rating_cells,
    classify_question,
    coverage_verdict_failure,
    gppe_cells,
    peg_cells,
    persist,
    previous_head,
    reports_current_for_head,
    stored_confidence_report_run,
    stored_report_rules_current,
    stored_report_run,
    summary_line,
    supply_chain_cells,
    theme_purity_cells,
    with_change,
)
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    QUESTION_REQUIREMENTS_SHA256,
    Question,
    QuestionRequirement,
)
from truealpha_contracts.reasons import REGISTRY_SHA256

pytestmark = pytest.mark.deps

TOPT = "universe:topt-us-2026-03-31"
QQQ = "universe:qqq-us-2026-06-30"
SP500 = "universe:sp500-us-2026-09-30"
CANARY = "universe:canary-us-2026-06-30"
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
        cells_by_column={"mart.metric_node_values.value": cells},
    )
    assert entry["column"] == "mart.metric_node_values.value"
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
    synthetic_unbound = QuestionRequirement(Question.Q3_SUPPLY_CHAIN_EXPOSURE, (), (), "#772")
    unbound = classify_question(
        synthetic_unbound, universe_id=QQQ, issuers=["issuer:a", "issuer:b"], cells_by_column={}
    )
    assert unbound["missing"] == 2 and unbound["column"] is None and unbound["tracking_issue"] == "#772"


def test_peg_applies_to_strategy_universes_only() -> None:
    """PEG is scoped to the `strategy` role (`topt`, `qqq`, `sp500`). On non-strategy sentinel `canary`,
    no column governs the universe, so every subject is graded missing with full denominator."""
    peg = REQ[Question.Q2_VALUATION_VS_GROWTH]
    cells = {"mart.strategy_decisions.peg": (Cell("issuer:a", True), Cell("issuer:b", False, "excluded:financial"))}
    for strategy_universe in (TOPT, QQQ, SP500):
        res = classify_question(
            peg, universe_id=strategy_universe, issuers=["issuer:a", "issuer:b"], cells_by_column=cells
        )
        assert res["answered"] == 1 and res["unavailable"] == {"excluded:financial": 1}
        assert res["denominator"] == 2 and res["missing"] == 0
    canary_issuers = [f"issuer:{n}" for n in range(6)]
    canary = classify_question(peg, universe_id=CANARY, issuers=canary_issuers, cells_by_column=cells)
    assert canary["missing"] == 6
    assert canary["denominator"] == 6
    assert canary["answered"] == 0 and canary["unavailable"] == {}
    assert canary["column"] is None and canary["columns"] == []


def test_a_question_with_no_column_at_all_stays_missing() -> None:
    unbound = QuestionRequirement(Question.Q3_SUPPLY_CHAIN_EXPOSURE, (), (), "#772")
    entry = classify_question(unbound, universe_id=QQQ, issuers=["issuer:a", "issuer:b"], cells_by_column={})
    assert entry["missing"] == 2 and entry["denominator"] == 2


def test_a_question_with_an_applicable_column_counts_answered() -> None:
    entry = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=QQQ,
        issuers=["issuer:a"],
        cells_by_column={"mart.metric_node_values.value": (Cell("issuer:a", True),)},
    )
    assert entry["denominator"] == 1 and entry["answered"] == 1 and entry["missing"] == 0


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self.rows


def test_gppe_cells_use_persisted_availability_status() -> None:
    rows = [
        ("issuer:a", "available", [], Decimal("1")),
        ("issuer:b", "low_confidence", ["low_confidence"], Decimal("1")),
        ("issuer:c", "unavailable", ["missing_headcount", "missing_total_assets"], None),
        ("issuer:d", "stale", ["stale_input"], Decimal("1")),
    ]
    cells = gppe_cells(_Rows(rows), "run")
    assert [c.answered for c in cells] == [True, True, False, False]
    assert cells[1].reason == "low_confidence"
    assert cells[2].reason == "missing_headcount" and cells[3].reason == "stale_input"


def test_peg_cells_name_the_exclusion_or_admit_the_reason_is_unrecorded() -> None:
    rows = [
        ("issuer:a", "1.2", "available", None, []),
        ("issuer:b", None, "excluded", "financial_branch", []),
        ("issuer:c", None, "available", None, []),
        ("issuer:d", None, "available", None, ["non_positive_growth"]),
        ("issuer:e", None, "excluded", "missing_gross_profit_fact", ["insufficient_earnings_history"]),
        ("issuer:f", "0.8", "excluded", "missing_gross_profit_fact", []),
    ]
    cells = peg_cells(_Rows(rows), run_id="capture-run:" + "0" * 64)
    # #1804: PEG does not read the gross profit. The reason of the PEG factor wins over the
    # exclusion of the decision, and an excluded decision with a PEG value is answered.
    assert cells[4].reason == "insufficient_earnings_history"
    assert cells[5].answered
    assert cells[0].answered and cells[1].reason == "excluded:financial_branch" and cells[2].reason == UNRECORDED_REASON
    # #837: an evaluated issuer without a PEG says why, in the factor's own words — the three
    # `unrecorded_reason` cells on the 2026-09-15 staging topt report were exactly this row.
    assert cells[3].reason == "non_positive_growth"


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
            "select universe_id, requirements_sha256, payload->'questions'->'q3'->>'missing', payload->'questions'->'q3'->'unavailable'->>'no_row' from mart.question_coverage_report where report_id = %s",
            (report_id,),
        ).fetchone()
        assert row == (TOPT, QUESTION_REQUIREMENTS_SHA256, "0", "1")
        assert "q1: 0/1 answered" in summary_line(report)
    finally:
        connection.rollback()
        connection.close()


def test_the_previous_head_is_found_by_the_lane_and_a_report_of_old_rules_is_not_current(monkeypatch) -> None:
    """A list universe changes its id with its membership, so the lane name and the lane's id prefix find
    the previous head; an id outside the prefix is never read. A stored report without the current reason
    registry still names its head but is not current."""
    from types import SimpleNamespace

    from data_engine.datahub import question_coverage

    connection = _connection()
    lane, tag = "universe-list:t1851", os.getpid()
    # A lane of its own: a committed report of a real lane in a shared CI database cannot be newer.
    monkeypatch.setattr(question_coverage, "key_of_lane", lambda name: "t1851" if name == lane else "none")
    monkeypatch.setattr(question_coverage, "record", lambda key: SimpleNamespace(id_prefix=f"universe:{key}-us-{tag}-"))

    def report(
        universe_id: str,
        run: str,
        *,
        registry: str | None,
        compiler_version: str | None = REPORT_COMPILER_VERSION,
    ) -> dict:
        body = {
            "universe": lane,
            "universe_id": universe_id,
            "run_id": run,
            "cutoff": datetime(2026, 11, 22, tzinfo=UTC).isoformat(),
            "environment": "test",
            "requirements_sha256": QUESTION_REQUIREMENTS_SHA256,
            "generated_at": datetime(2026, 11, 22, 9, 7, tzinfo=UTC).isoformat(),
            "denominator": 1,
            "questions": {},
        }
        if registry:
            body["reason_registry_sha256"] = registry
        if compiler_version:
            body["compiler_version"] = compiler_version
        return body

    try:
        older, newer, stray = ("capture-run:" + c * 64 for c in "123")
        old_id, new_id = f"universe:t1851-us-{tag}-2099-01-01", f"universe:t1851-us-{tag}-2099-02-15"
        persist(connection, report(old_id, older, registry=None, compiler_version=None))
        assert stored_report_run(connection, old_id) == older, "a report of the old rules still names its head"
        assert stored_report_rules_current(connection, old_id) is False, "but it is not current"
        persist(connection, report(new_id, newer, registry=REGISTRY_SHA256, compiler_version=None))
        assert stored_report_rules_current(connection, new_id) is False, "missing compiler_version is not current"
        persist(connection, report(new_id, newer, registry=REGISTRY_SHA256))
        assert stored_report_rules_current(connection, new_id) is True
        persist(connection, report(f"universe:t1851-{tag}-2099", stray, registry=REGISTRY_SHA256))

        previous = previous_head(connection, lane, newer)
        assert previous is not None and (previous.universe_id, previous.run_id) == (old_id, older)
        assert previous_head(connection, "universe-list:none", newer) is None
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
        cells_by_column={"mart.metric_node_values.value": (Cell("issuer:a", True),)},
    )
    assert entry["scope"] == "issuer"
    assert entry["denominator"] == 3 and entry["answered"] == 1


def test_theme_purity_cells_answer_an_issuer_when_any_theme_produced_a_share() -> None:
    """Module 6 writes one row per (issuer, THEME), and q6 asks whether an issuer's purity is
    answerable at all. Counting an issuer unavailable because one of three themes refused
    would make ADDING a theme look like a regression in coverage."""
    rows = [
        ("issuer:a", "available", []),
        ("issuer:a", "unavailable", ["below_minimum_classified_share"]),
        ("issuer:b", "unavailable", ["below_minimum_classified_share"]),
        ("issuer:b", "unavailable", ["no_segments"]),
    ]
    cells = theme_purity_cells(_Rows(rows), "run")
    by_subject = {c.subject_id: c for c in cells}
    assert by_subject["issuer:a"].answered is True, "one theme produced a share"
    assert by_subject["issuer:b"].answered is False
    assert by_subject["issuer:b"].reason == "below_minimum_classified_share", "the first reason, not the last"


def test_a_theme_purity_refusal_names_the_floor_that_refused_it() -> None:
    """The same shape every other cell uses: unavailable-with-a-reason, never answered and
    never missing. `below_minimum_classified_share` is the honest one — the classifier could
    not judge enough of the issuer's revenue to rank it."""
    entry = classify_question(
        REQ[Question.Q6_THEME_PURITY],
        universe_id=QQQ,
        issuers=["issuer:thin"],
        funds=[],
        cells_by_column={
            "mart.issuer_theme_purity.theme_share": (Cell("issuer:thin", False, "below_minimum_classified_share"),)
        },
    )
    assert entry["answered"] == 0 and entry["missing"] == 0
    assert entry["unavailable"] == {"below_minimum_classified_share": 1}


def test_q6_is_bound_to_the_materialized_column_rather_than_left_missing() -> None:
    """The gap this closes: q6 carried no column, so every issuer counted as `missing` — a
    claim about the REGISTRY ("no column exists yet"), which stopped being true when
    `mart.issuer_theme_purity` landed. A question whose column exists and is not declared
    reports a gap that is nobody's to fix."""
    requirement = REQ[Question.Q6_THEME_PURITY]
    assert requirement.has_column
    assert [c.table for c in requirement.columns] == ["mart.issuer_theme_purity"]
    assert [c.column for c in requirement.columns] == ["theme_share"]
    assert requirement.standards == ("segment_revenue",), "and it names the standard it consumes"


def test_supply_chain_cells_records_status_and_reasons() -> None:
    rows = [
        ("issuer:a", "available", [], Decimal("0.35")),
        ("issuer:b", "unavailable", ["no_disclosed_suppliers"], None),
        ("issuer:c", "stale", ["stale_filing"], None),
    ]
    cells = supply_chain_cells(_Rows(rows), "run")
    assert [c.answered for c in cells] == [True, False, False]
    assert cells[1].reason == "no_disclosed_suppliers" and cells[2].reason == "stale_filing"


def test_analyst_rating_cells_records_status_and_reasons() -> None:
    rows = [
        ("issuer:a", "available", [], Decimal("4.2")),
        ("issuer:b", "unavailable", ["no_analyst_coverage"], None),
    ]
    cells = analyst_rating_cells(_Rows(rows), "run")
    assert [c.answered for c in cells] == [True, False]
    assert cells[1].reason == "no_analyst_coverage"


def test_q3_is_bound_to_the_materialized_column_rather_than_left_missing() -> None:
    requirement = REQ[Question.Q3_SUPPLY_CHAIN_EXPOSURE]
    assert requirement.has_column
    assert [c.table for c in requirement.columns] == ["mart.issuer_supply_chain_exposure"]
    assert [c.column for c in requirement.columns] == ["exposure_score"]


def test_q4_is_bound_to_the_materialized_column_rather_than_left_missing() -> None:
    requirement = REQ[Question.Q4_ANALYST_TRACK_RECORD]
    assert requirement.has_column
    assert [c.table for c in requirement.columns] == ["mart.issuer_analyst_ratings"]
    assert [c.column for c in requirement.columns] == ["consensus_rating"]


def test_coverage_verdict_failure_flags_zero_answered_applicable_questions_only() -> None:
    """An applicable question with zero answered subjects fails the coverage verdict (#1643)."""
    healthy_report = {
        "universe_id": QQQ,
        "questions": {
            "q1": classify_question(
                REQ[Question.Q1_MODEL_LEVERAGE],
                universe_id=QQQ,
                issuers=["issuer:a"],
                cells_by_column={"mart.metric_node_values.value": (Cell("issuer:a", True),)},
            ),
            # Q2 is scoped to strategy role; on sentinel CANARY it has no column (missing == denominator).
            "q2": classify_question(
                REQ[Question.Q2_VALUATION_VS_GROWTH],
                universe_id=CANARY,
                issuers=["issuer:a"],
                cells_by_column={},
            ),
            # Q5 with no fund subjects (denominator == 0) is not applicable.
            "q5": classify_question(
                REQ[Question.Q5_ETF_VIRTUAL_COMPANY],
                universe_id=TOPT,
                issuers=["issuer:a"],
                funds=[],
                cells_by_column={},
            ),
        },
    }
    assert coverage_verdict_failure(healthy_report) is None

    zero_q2_report = {
        "universe_id": QQQ,
        "questions": {
            **healthy_report["questions"],
            "q2": classify_question(
                REQ[Question.Q2_VALUATION_VS_GROWTH],
                universe_id=QQQ,
                issuers=["issuer:a"],
                cells_by_column={},
            ),
        },
    }
    assert coverage_verdict_failure(zero_q2_report) == f"zero answered for q2=0/1 on {QQQ}"

    zero_q5_report = {
        "universe_id": QQQ,
        "questions": {
            **healthy_report["questions"],
            "q5": classify_question(
                REQ[Question.Q5_ETF_VIRTUAL_COMPANY],
                universe_id=QQQ,
                issuers=["issuer:a"],
                funds=["etf:series:S000101292"],
                cells_by_column={
                    "mart.fund_virtual_company.weighted_valuation_gap": (
                        Cell("etf:series:S000101292", False, "valued_weight_below_minimum"),
                    )
                },
            ),
        },
    }
    assert coverage_verdict_failure(zero_q5_report) == f"zero answered for q5=0/1 on {QQQ}"


def test_compile_report_qqq_empty_fund_virtual_company_fails_q5_via_expected_fund_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mart.fund_virtual_company has 0 rows for QQQ, expected_fund_ids prevents GREEN-WHILE-EMPTY on q5."""
    from data_engine.datahub import question_coverage as qc

    head = GovernedHead(universe_id=QQQ, run_id="capture-run:qqq-empty-q5", cutoff=datetime(2026, 10, 6, tzinfo=UTC))

    class _FakeCursor(_Rows):
        def fetchone(self):
            return self.rows[0] if self.rows else None

    class _FakeConn:
        def execute(self, sql: str, params=None):
            if "from mart.environment_identity" in sql:
                return _FakeCursor([("staging",)])
            if "from mart.fund_holdings_resolved" in sql:
                return _FakeCursor([("etf:series:S000101292",)])
            return _FakeCursor([])

    monkeypatch.setattr(qc, "governed_head", lambda *_a, **_k: head)
    monkeypatch.setattr(qc, "gppe_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(
        qc,
        "peg_cells",
        lambda *_a, **_k: (Cell("issuer:a", True),),
    )
    monkeypatch.setattr(qc, "supply_chain_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(qc, "analyst_rating_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(qc, "fund_cells", lambda *_a, **_k: ())
    monkeypatch.setattr(qc, "theme_purity_cells", lambda *_a, **_k: (Cell("issuer:a", True),))

    report = qc.compile_report(_FakeConn(), universe="universe-list:qqq", executed_at=datetime(2026, 10, 6, tzinfo=UTC))  # type: ignore[arg-type]
    assert report is not None
    q5 = report["questions"]["q5"]
    assert q5["denominator"] == 1
    assert q5["answered"] == 0
    assert q5["unavailable"] == {NO_ROW: 1}
    assert coverage_verdict_failure(report) == f"zero answered for q5=0/1 on {QQQ}"


def test_questions_uses_frozen_snapshot_members_for_denominator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing GPPE rows must not shrink the denominator when frozen core snapshot members exist (#1747)."""
    from data_engine.datahub import question_coverage as qc

    head = GovernedHead(universe_id=QQQ, run_id="capture-run:qqq-denom", cutoff=datetime(2026, 10, 6, tzinfo=UTC))

    class _SnapshotConn:
        def execute(self, sql: str, params=None):
            if "from staging.core_snapshot_members" in sql:
                return _Rows([("issuer:a",), ("issuer:b",)])
            return _Rows([])

    monkeypatch.setattr(qc, "gppe_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(qc, "peg_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(qc, "supply_chain_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(qc, "analyst_rating_cells", lambda *_a, **_k: (Cell("issuer:a", True),))
    monkeypatch.setattr(qc, "fund_cells", lambda *_a, **_k: ())
    monkeypatch.setattr(qc, "theme_purity_cells", lambda *_a, **_k: (Cell("issuer:a", True),))

    issuer_count, questions = qc._questions(_SnapshotConn(), "qqq", head)  # type: ignore[arg-type]
    assert issuer_count == 2
    assert questions["q1"]["denominator"] == 2
    assert questions["q1"]["answered"] == 1
    assert questions["q1"]["unavailable"] == {NO_ROW: 1}


def test_reports_current_for_head_requires_both_coverage_and_confidence_reports() -> None:
    """A head is current only when both coverage and confidence reports match its run_id (#1643)."""
    head = GovernedHead(universe_id=TOPT, run_id="capture-run:new", cutoff=datetime(2026, 10, 6, tzinfo=UTC))

    class _ReportConnection:
        def __init__(
            self,
            coverage_run: str | None,
            confidence_run: str | None,
            registry: str = REGISTRY_SHA256,
            compiler_version: str | None = REPORT_COMPILER_VERSION,
        ) -> None:
            self.coverage_run = coverage_run
            self.confidence_run = confidence_run
            self.registry = registry
            self.compiler_version = compiler_version
            self._row: tuple[Any, ...] | None = None

        def execute(self, sql: str, params=None):
            if "reason_registry_sha256" in sql:
                self._row = (self.registry, self.compiler_version) if self.coverage_run is not None else None
            elif "mart.question_coverage_report" in sql:
                self._row = (self.coverage_run,) if self.coverage_run is not None else None
            elif "mart.datahub_confidence_report" in sql:
                self._row = (self.confidence_run,) if self.confidence_run is not None else None
            else:
                self._row = None
            return self

        def fetchone(self):
            return self._row

    assert stored_confidence_report_run(_ReportConnection("capture-run:new", None), TOPT) is None
    assert stored_confidence_report_run(_ReportConnection("capture-run:new", "capture-run:new"), TOPT) == (
        "capture-run:new"
    )
    assert reports_current_for_head(_ReportConnection("capture-run:new", None), head) is False
    assert reports_current_for_head(_ReportConnection("capture-run:new", "capture-run:old"), head) is False
    assert reports_current_for_head(_ReportConnection("capture-run:old", "capture-run:new"), head) is False
    assert reports_current_for_head(_ReportConnection("capture-run:new", "capture-run:new"), head) is True
    old_rules = _ReportConnection("capture-run:new", "capture-run:new", registry="0" * 64)
    assert reports_current_for_head(old_rules, head) is False, "a report of other rules is not current (#1851)"
    old_compiler = _ReportConnection("capture-run:new", "capture-run:new", compiler_version=None)
    assert reports_current_for_head(old_compiler, head) is False, "an unversioned compiler report is stale (#1747)"


def test_head_reports_sensor_retriggers_when_compiler_version_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the head run_id has not moved but stored report rules are stale, the sensor launches a refresh (#1747)."""
    import json

    import dagster as dg
    from data_engine.datahub import question_coverage as qc
    from data_engine.lanes import standards

    head = GovernedHead(universe_id=SP500, run_id="capture-run:sp500", cutoff=datetime(2026, 11, 24, tzinfo=UTC))

    class _DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(standards.db, "connect", lambda: _DummyConn())
    monkeypatch.setattr(standards, "scheduled_universe_names", lambda: ("universe-list:sp500",))
    monkeypatch.setattr(qc, "governed_head_of", lambda _conn, _key: head)
    monkeypatch.setattr(qc, "stored_report_run", lambda _conn, _uid: head.run_id)
    stale = {"current": False}
    monkeypatch.setattr(qc, "stored_report_rules_current", lambda _conn, _uid: stale["current"])

    ctx = dg.build_sensor_context(cursor=json.dumps({"universe-list:sp500": head.run_id}))
    emitted = list(standards.head_reports_sensor(ctx))
    assert len(emitted) == 1 and isinstance(emitted[0], dg.RunRequest)
    expected_target = f"{head.run_id}:{qc.REPORT_RULES_TAG}"
    assert emitted[0].run_key == f"head:universe-list:sp500:{expected_target}"
    assert json.loads(ctx.cursor or "{}")["universe-list:sp500"] == expected_target

    # Second tick with matching cursor skips without launching again.
    emitted_again = list(standards.head_reports_sensor(ctx))
    assert len(emitted_again) == 1 and isinstance(emitted_again[0], dg.SkipReason)

    # Fresh cursor against already-current report rules skips without launching.
    stale["current"] = True
    fresh_ctx = dg.build_sensor_context()
    emitted_current = list(standards.head_reports_sensor(fresh_ctx))
    assert len(emitted_current) == 1 and isinstance(emitted_current[0], dg.SkipReason)


def test_gppe_cells_and_classify_question_honor_persisted_not_applicable_status() -> None:
    """Owner Decision 2 (#1664): persisted availability_status='not_applicable' keeps denominator at 16 without masking failures."""
    applicable = [f"issuer:applicable:{i}" for i in range(16)]
    exempt = [
        "issuer:lei:J3WHBG0MTS7O8ZVMDC91",  # XOM
        "issuer:cik:0001403161",  # V
        "issuer:cik:0001141391",  # MA
        "issuer:cik:0001067983",  # BRK.B
    ]
    rows = [(issuer, "available", [], Decimal("100")) for issuer in applicable] + [
        (issuer, "not_applicable", ["missing_gross_profit"], None) for issuer in exempt
    ]
    cells = gppe_cells(_Rows(rows), "capture-run:topt")
    assert len(cells) == 20
    assert sum(1 for c in cells if c.not_applicable) == 4

    q1 = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=TOPT,
        issuers=[c.subject_id for c in cells],
        cells_by_column={"mart.metric_node_values.value": cells},
    )
    assert q1["answered"] == 16
    assert q1["denominator"] == 16
    assert q1["unavailable"] == {}
    assert q1["missing"] == 0
    assert q1["not_applicable"] == 4

    degraded_rows = [
        (applicable[0], "unavailable", ["missing_headcount"], None),
        *rows[1:],
    ]
    degraded_cells = gppe_cells(_Rows(degraded_rows), "capture-run:topt")
    degraded_q1 = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=TOPT,
        issuers=[c.subject_id for c in degraded_cells],
        cells_by_column={"mart.metric_node_values.value": degraded_cells},
    )
    assert degraded_q1["answered"] == 15
    assert degraded_q1["denominator"] == 16
    assert degraded_q1["unavailable"] == {"missing_headcount": 1}


# --- #1851 layer 1 step 2: classes, violations, change -------------------------------------------------


def _q1(cells, issuers):
    return classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=QQQ,
        issuers=issuers,
        cells_by_column={"mart.metric_node_values.value": cells},
    )


def test_every_empty_cell_is_in_one_class_or_one_violation() -> None:
    cells = (
        Cell("issuer:a", True),
        Cell("issuer:b", False, "missing_headcount"),
        Cell("issuer:c", False, "model_no_answer"),
        Cell("issuer:d", False, "no_us_gaap_gross_profit_tag", not_applicable=True),
        Cell("issuer:e", False, UNRECORDED_REASON),
        Cell("issuer:f", False, "no_such_registered_code"),
        Cell("issuer:g", False, "nonpositive_headcount"),
    )
    entry = _q1(cells, [f"issuer:{x}" for x in "abcdefgh"])

    assert entry["by_class"] == {"disputed": 1, "not_applicable": 1, "pipeline_gap": 1, "transient": 1}
    assert entry["violations"] == {"no_reason": 1, "no_row": 1, "unregistered": 1}
    assert entry["unregistered_reasons"] == {"no_such_registered_code": 1}
    empty = 8 - entry["answered"]
    assert sum(entry["by_class"].values()) + sum(entry["violations"].values()) == empty
    assert entry["denominator"] == 7, "the not-applicable issuer leaves the denominator"
    assert entry["reasons"] == {
        "issuer:b": "missing_headcount",
        "issuer:c": "model_no_answer",
        "issuer:d": "no_us_gaap_gross_profit_tag",
        "issuer:e": UNRECORDED_REASON,
        "issuer:f": "no_such_registered_code",
        "issuer:g": "nonpositive_headcount",
        "issuer:h": NO_ROW,
    }


def test_an_evidence_code_is_never_the_reason_of_an_empty_cell() -> None:
    """Theme purity writes its evidence flags first; the refusal is the floor (#1851 review)."""
    rows = [
        ("issuer:a", "unavailable", ["unclassified_revenue", "partition_residual", "below_minimum_classified_share"])
    ]
    (cell,) = theme_purity_cells(_Rows(rows), "run")
    assert cell.reason == "below_minimum_classified_share"


def test_the_served_status_alone_says_a_measure_does_not_apply() -> None:
    """The producer sets `not_applicable` and the TOPT baseline counts it; a reason class only routes
    the gap (#1851 review: `zero_denominator` would have left the q1 denominator)."""
    (gap,) = gppe_cells(_Rows([("issuer:a", "unavailable", ["zero_denominator"], None)]), "run")
    assert (gap.reason, gap.not_applicable) == ("zero_denominator", False)
    (listed,) = gppe_cells(
        _Rows([("issuer:b", "not_applicable", ["missing_headcount", "no_us_gaap_gross_profit_tag"], None)]), "run"
    )
    assert (listed.reason, listed.not_applicable) == ("no_us_gaap_gross_profit_tag", True)
    entry = _q1((gap, listed), ["issuer:a", "issuer:b"])
    assert entry["denominator"] == 1 and entry["by_class"] == {"not_applicable": 1, "pipeline_gap": 1}
    (applicable,) = gppe_cells(
        _Rows([("issuer:c", "unavailable", ["missing_headcount", "nonpositive_revenue"], None)]), "run"
    )
    assert applicable.reason == "missing_headcount", "in an applicable cell the first outcome code is the reason"


def test_an_applicable_cell_with_a_not_applicable_reason_is_a_conflict_not_a_class() -> None:
    """#1851 review: PEG serves `non_positive_growth` on an applicable row. Counted as not applicable
    it sat in the class and in the denominator at once; it is a conflict the producer or the registry fixes."""
    entry = classify_question(
        REQ[Question.Q2_VALUATION_VS_GROWTH],
        universe_id=TOPT,
        issuers=["issuer:a", "issuer:b"],
        cells_by_column={
            "mart.strategy_decisions.peg": (Cell("issuer:a", False, "non_positive_growth"), Cell("issuer:b", True))
        },
    )
    assert entry["denominator"] == 2 and entry["unavailable"] == {"non_positive_growth": 1}
    assert entry["by_class"] == {} and entry["violations"] == {"status_class_conflict": 1}
    assert "not_applicable" not in entry


def test_the_reason_of_peg_wins_over_the_exclusion_of_its_decision() -> None:
    """#1804 and the #1851 review: PEG is computed for an excluded decision, so an exclusion of the
    class not_applicable must not hide the PEG gap."""
    rows = [
        ("issuer:a", None, "excluded", "nonpositive_gross_profit", ["missing_net_income"]),
        ("issuer:b", None, "excluded", "nonpositive_gross_profit", []),
    ]
    own, excluded = peg_cells(_Rows(rows), run_id="capture-run:" + "0" * 64)
    assert (own.reason, own.not_applicable) == ("missing_net_income", False)
    assert (excluded.reason, excluded.not_applicable) == ("excluded:nonpositive_gross_profit", False)


def test_a_cell_without_any_code_is_no_reason_and_not_its_status() -> None:
    (cell,) = supply_chain_cells(_Rows([("issuer:a", "unavailable", [])]), "run")
    assert cell.reason == UNRECORDED_REASON


def test_the_change_names_the_counts_and_the_subjects_that_moved() -> None:
    issuers = ["issuer:a", "issuer:b"]
    before = {
        "q1": _q1((Cell("issuer:a", False, "missing_headcount"), Cell("issuer:b", False, "model_no_answer")), issuers)
    }
    after = {"q1": _q1((Cell("issuer:a", True), Cell("issuer:b", False, "missing_headcount")), issuers)}

    change = with_change(after, before)["q1"]["change"]

    assert change["answered"] == 1
    assert change["by_class"] == {"transient": -1}
    assert change["unavailable"] == {"model_no_answer": -1}
    assert change["subjects_changed"] == 2
    assert change["examples"] == [
        {"subject": "issuer:a", "before": "missing_headcount", "after": None},
        {"subject": "issuer:b", "before": "model_no_answer", "after": "missing_headcount"},
    ]
    assert "reasons" not in after["q1"], "the per-subject reasons are not stored"


def test_without_a_previous_head_there_is_no_change() -> None:
    questions = {"q1": _q1((Cell("issuer:a", True),), ["issuer:a"])}
    assert "change" not in with_change(questions, None)["q1"]
    assert "reasons" not in questions["q1"]


def test_classify_question_separates_world_absence_and_pipeline_gap() -> None:
    """Honest metrics (#1747): world_absence shrinks actionable_denominator; pipeline_gap stays actionable."""
    entry = classify_question(
        REQ[Question.Q1_MODEL_LEVERAGE],
        universe_id=TOPT,
        issuers=["issuer:a", "issuer:b", "issuer:c", "issuer:d", "issuer:e"],
        cells_by_column={
            "mart.metric_node_values.value": (
                Cell("issuer:a", True),
                Cell("issuer:b", True),
                Cell("issuer:c", False, "missing_labor_cost_disclosure"),
                Cell("issuer:d", False, "no_us_gaap_gross_profit_tag", not_applicable=True),
                Cell("issuer:e", False, "missing_headcount"),
            )
        },
    )
    assert entry["denominator"] == 4
    assert entry["answered"] == 2
    assert entry["not_applicable"] == 1
    assert entry["world_absence"] == 1
    assert entry["pipeline_gap"] == 1
    assert entry["actionable_denominator"] == 3
    assert entry["actionable_coverage_pct"] == pytest.approx(66.7)
    line = summary_line({"universe_id": TOPT, "cutoff": "2026-11-26T00:00:00Z", "questions": {"q1": entry}})
    assert "2/3 answered (66.7% actionable, gaps=1)" in line
