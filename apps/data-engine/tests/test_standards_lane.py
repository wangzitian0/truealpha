"""Unit tests for coverage verdict truth and confidence-report freshness in the standards lane (#1643)."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import question_coverage
from data_engine.lanes import standards
from data_engine.quality import nightly_verdicts

QQQ_LANE = "universe-list:qqq"
QQQ_ID = "universe:qqq-us-2026-06-30"
TOPT_ID = "universe:topt-us-2026-03-31"
HEAD_RUN = "capture-run:" + "a" * 64
STALE_RUN = "capture-run:" + "b" * 64
EXECUTED_AT = "2026-10-06T04:00:00+00:00"
CUTOFF = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")


class _StubConnection:
    """Minimal database connection stub for standards lane op tests."""

    def __enter__(self) -> _StubConnection:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def transaction(self) -> Any:
        return nullcontext()

    def commit(self) -> None:
        return None


def _sample_report(*, q5_answered: int) -> dict[str, Any]:
    return {
        "universe": QQQ_LANE,
        "universe_id": QQQ_ID,
        "run_id": HEAD_RUN,
        "cutoff": CUTOFF.isoformat(),
        "environment": "staging",
        "requirements_sha256": "0" * 64,
        "generated_at": EXECUTED_AT,
        "denominator": 20,
        "questions": {
            "q1": {
                "text": "Q1",
                "tracking_issue": None,
                "scope": "issuer",
                "denominator": 20,
                "column": "mart.metric_node_values.value",
                "columns": ["mart.metric_node_values.value"],
                "answered": 18,
                "unavailable": {"missing_headcount": 2},
                "missing": 0,
            },
            "q2": {
                "text": "Q2",
                "tracking_issue": None,
                "scope": "issuer",
                "denominator": 20,
                "column": None,
                "columns": [],
                "answered": 0,
                "unavailable": {},
                "missing": 20,
            },
            "q5": {
                "text": "Q5",
                "tracking_issue": "#36",
                "scope": "fund",
                "denominator": 1,
                "column": "mart.fund_virtual_company.weighted_valuation_gap",
                "columns": ["mart.fund_virtual_company.weighted_valuation_gap"],
                "answered": q5_answered,
                "unavailable": {} if q5_answered > 0 else {"valued_weight_below_minimum": 1},
                "missing": 0,
            },
        },
    }


def test_zero_answered_applicable_question_records_red_coverage_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an applicable question has 0 answered subjects, `question_coverage@<universe>` ends red (#1643)."""
    recorded: list[dict[str, Any]] = []
    report = _sample_report(q5_answered=0)

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _StubConnection())
    monkeypatch.setattr(question_coverage, "compile_report", lambda *_a, **_k: report)
    monkeypatch.setattr(question_coverage, "persist", lambda _c, _r: "question-coverage-report:test")
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    config = standards.StandardBackfillConfig(executed_at=EXECUTED_AT, universe=QQQ_LANE)
    analyst_summary = json.dumps({"universe": QQQ_LANE, "executed_at": EXECUTED_AT, "rows": 20, "fetch_errors": 0})
    coverage_summary = standards.run_question_coverage(dg.build_op_context(), config, analyst_summary)
    parsed_coverage = json.loads(coverage_summary)

    expected_failure = f"zero answered for q5=0/1 on {QQQ_ID}"
    assert parsed_coverage[standards.VERDICT_FAILURE] == expected_failure

    with pytest.raises(RuntimeError, match=expected_failure):
        standards.fail_if_a_lane_failed(dg.build_op_context(), analyst_summary, coverage_summary)

    assert [(row["check"], row["ok"]) for row in recorded] == [
        (f"question_coverage@{QQQ_LANE}", True),
        (f"question_coverage@{QQQ_LANE}", False),
    ]
    assert recorded[-1]["summary"] == f"failed: {expected_failure}"


def test_nonzero_answered_applicable_questions_keep_coverage_verdict_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """When all applicable questions have answered > 0, `question_coverage@<universe>` stays green (#1643)."""
    recorded: list[dict[str, Any]] = []
    report = _sample_report(q5_answered=1)

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _StubConnection())
    monkeypatch.setattr(question_coverage, "compile_report", lambda *_a, **_k: report)
    monkeypatch.setattr(question_coverage, "persist", lambda _c, _r: "question-coverage-report:test")
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    config = standards.StandardBackfillConfig(executed_at=EXECUTED_AT, universe=QQQ_LANE)
    analyst_summary = json.dumps({"universe": QQQ_LANE, "executed_at": EXECUTED_AT, "rows": 20, "fetch_errors": 0})
    coverage_summary = standards.run_question_coverage(dg.build_op_context(), config, analyst_summary)
    parsed_coverage = json.loads(coverage_summary)

    assert standards.VERDICT_FAILURE not in parsed_coverage
    standards.fail_if_a_lane_failed(dg.build_op_context(), analyst_summary, coverage_summary)

    assert [(row["check"], row["ok"]) for row in recorded] == [(f"question_coverage@{QQQ_LANE}", True)]


@pytest.mark.parametrize(
    ("coverage_run", "confidence_run", "is_current"),
    [
        (HEAD_RUN, HEAD_RUN, True),
        (HEAD_RUN, None, False),
        (HEAD_RUN, STALE_RUN, False),
        (STALE_RUN, HEAD_RUN, False),
    ],
    ids=["both-current", "confidence-missing", "confidence-stale", "coverage-stale"],
)
def test_head_reports_start_requires_fresh_confidence_report(
    monkeypatch: pytest.MonkeyPatch,
    coverage_run: str | None,
    confidence_run: str | None,
    is_current: bool,
) -> None:
    """`head_reports_start` marks a head current only when both coverage and confidence reports match (#1643)."""
    head = question_coverage.GovernedHead(universe_id=TOPT_ID, run_id=HEAD_RUN, cutoff=CUTOFF)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _StubConnection())
    monkeypatch.setattr(question_coverage, "governed_head", lambda _c, **_k: head)
    monkeypatch.setattr(question_coverage, "stored_report_run", lambda _c, _u: coverage_run)
    monkeypatch.setattr(question_coverage, "stored_confidence_report_run", lambda _c, _u: confidence_run)
    monkeypatch.setattr(standards, "newest_is_red", lambda _name: False)

    config = standards.HeadReportsStartConfig(executed_at=EXECUTED_AT, universe="topt", only_if_stale=True)
    summary = json.loads(standards.head_reports_start(dg.build_op_context(), config))

    assert (standards.REPORTS_CURRENT in summary) is is_current


def _backfill_op_summary(monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, int]) -> dict[str, Any]:
    """Run the backfill op over one stub report per standard with the given cell outcomes."""
    from collections import Counter

    from data_engine.datahub.standards.backfill import BackfillReport
    from data_engine.sources import gateway

    def report(_connection: object, *, universe: str, standard_name: str, **_kwargs: object) -> BackfillReport:
        return BackfillReport(universe, standard_name, "backfill", CUTOFF, issuers=3, outcomes=Counter(outcomes))

    monkeypatch.setattr(standards, "_run_standard_backfill", report)
    monkeypatch.setattr(standards.db, "connect", lambda: _StubConnection())
    monkeypatch.setattr(gateway, "capacity_scope", lambda: nullcontext())
    config = standards.StandardBackfillConfig(executed_at=EXECUTED_AT, universe="topt", standard="employees_total")
    return json.loads(standards.run_standard_backfill(dg.build_op_context(), config))


@pytest.mark.parametrize("failed_status", ["error", "deferred_capacity"])
def test_a_backfill_cell_without_an_answer_is_a_lane_failure(monkeypatch: pytest.MonkeyPatch, failed_status) -> None:
    """A failed cell does not stop the op (#1851): the summary carries it to the run's last op."""
    summary = _backfill_op_summary(monkeypatch, {"resolved": 2, failed_status: 1})

    assert summary[standards.LANE_FAILURE] == "standard backfill: employees_total: 1 of 3 cells got no answer"
    assert summary[standards.VERDICT_TEXT] == summary[standards.LANE_FAILURE]
    assert [s["outcomes"] for s in summary["standards"]] == [{"resolved": 2, failed_status: 1}]


def test_a_backfill_without_a_failed_cell_has_no_lane_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = _backfill_op_summary(monkeypatch, {"resolved": 2, "no_candidate": 1, "model_declined": 1})

    assert standards.LANE_FAILURE not in summary
    assert summary["universe"] == "topt"


def test_the_backfill_failure_reaches_the_last_op_through_every_lane_without_a_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every lane ends without a head here, as on the first backfill of a new universe; the failure
    of the backfill before them still makes the last op record a red verdict and fail the run."""
    monkeypatch.setattr(question_coverage, "governed_head_of", lambda _c, _k: question_coverage.NoHead("no head"))
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _StubConnection())
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))
    failure = "standard backfill: employees_total: 2 of 17 cells got no answer"
    upstream = json.dumps({standards.LANE_FAILURE: failure, standards.VERDICT_TEXT: failure})
    config = standards.StandardBackfillConfig(executed_at=EXECUTED_AT, universe="topt")
    context = dg.build_op_context()

    purity = standards.run_theme_purity(context, config, upstream)
    supply_chain = standards.run_supply_chain_exposure(context, config, purity)
    analyst = standards.run_analyst_ratings(context, config, supply_chain)
    with pytest.raises(RuntimeError, match="2 of 17 cells got no answer"):
        standards.fail_if_a_lane_failed(context, analyst, "{}")

    for summary in (purity, supply_chain, analyst):
        assert json.loads(summary)["reason"] == "no_governed_head"
        assert json.loads(summary)[standards.LANE_FAILURE] == failure
    assert [(row["check"], row["ok"]) for row in recorded] == [
        ("theme_purity@topt", None),
        ("question_coverage@topt", False),
    ]
    assert failure in recorded[-1]["summary"]


def test_a_lane_without_an_upstream_failure_carries_none() -> None:
    clean = json.dumps({"universe": "topt", "rows": 0})
    assert standards._with_upstream_failure(clean, "{}") == clean
    own = json.dumps({standards.LANE_FAILURE: "own", standards.VERDICT_TEXT: "own"})
    upstream = json.dumps({standards.LANE_FAILURE: "upstream"})
    assert json.loads(standards._with_upstream_failure(own, upstream))[standards.LANE_FAILURE] == "own"
