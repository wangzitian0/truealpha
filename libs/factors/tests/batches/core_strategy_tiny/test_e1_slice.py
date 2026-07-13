from pathlib import Path

import pytest
from factors.batches.core_strategy_tiny.e0_slice import FROZEN_CORPUS_SHA256
from factors.batches.core_strategy_tiny.e1_slice import (
    CoreTinyCaseEvidence,
    CoreTinyEvidence,
    CoreTinyRunEvidence,
    FindingClass,
    InMemoryCoreTinyEvidenceRepository,
    run_e1_suite,
)
from pydantic import ValidationError
from truealpha_contracts.execution import FactorValidationStatus

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]


def _cases(evidence: CoreTinyEvidence) -> dict[str, CoreTinyCaseEvidence]:
    return {case.case_id: case for case in evidence.cases}


def _finding_codes(case: CoreTinyCaseEvidence) -> set[tuple[FindingClass, str]]:
    return {(finding.classification, finding.code) for run in case.runs for finding in run.reverse_review.findings}


def test_e1_executes_the_exact_frozen_corpus_without_a_stable_handoff() -> None:
    evidence = run_e1_suite(REPOSITORY_ROOT)

    assert evidence.corpus_sha256 == FROZEN_CORPUS_SHA256
    assert evidence.activation.environment == "ci"
    assert evidence.factor_validation_status is FactorValidationStatus.NOT_EVALUATED
    assert evidence.stable_handoff is False
    assert evidence.requires_e2_contract_repair is False
    assert all(case.passed for case in evidence.cases)
    assert set(_cases(evidence)) == {
        "plug-restatement-publication-boundary",
        "ddog-provenance-free-success",
        "jpm-financial-proxy-branch",
        "nvda-missing-headcount",
        "alphabet-dual-listing-identity",
        "nice-lookahead-sentinel",
        "ddog-stale-low-confidence",
        "nice-cross-currency-without-fx",
        "public-golden-ordering-and-boundaries",
    }


def test_e1_reordered_and_repeated_execution_preserves_every_identity() -> None:
    first = run_e1_suite(REPOSITORY_ROOT)
    repeated = run_e1_suite(REPOSITORY_ROOT)
    reordered = run_e1_suite(REPOSITORY_ROOT, reverse_case_order=True, reverse_observation_order=True)

    assert repeated == first
    assert reordered == first
    assert reordered.evidence_id == first.evidence_id
    for case in first.cases:
        for run in case.runs:
            assert run.trace.output_id == (
                run.factor_result.result_id if run.factor_result is not None else run.ranking.ranking_id
            )
            assert run.usage_audit.trace_id == run.trace.trace_id
            assert run.reverse_review.usage_audit_id == run.usage_audit.usage_audit_id
            assert run.reverse_review.trace_id == run.trace.trace_id


def test_e1_enforces_restatement_and_lookahead_boundaries() -> None:
    cases = _cases(run_e1_suite(REPOSITORY_ROOT))
    plug_runs = cases["plug-restatement-publication-boundary"].runs

    selected_headcounts = [
        next(
            selection.source_identity
            for selection in run.input_selections
            if selection.input_kind == "headcount" and selection.selected
        )
        for run in plug_runs
    ]
    assert selected_headcounts == [
        "normalized-record:dd0e02b953d4ee566b675186ded6130f7ba78acd766a6b4b7be1ce62d4716c6e",
        "normalized-record:0120c267f8e692bb421815ea592dee787aa0bf3e684557c2972c41a1aa1e6cab",
        "normalized-record:0120c267f8e692bb421815ea592dee787aa0bf3e684557c2972c41a1aa1e6cab",
    ]

    before_publication, at_publication = cases["nice-lookahead-sentinel"].runs
    assert all(not selection.selected for selection in before_publication.input_selections)
    assert {
        "future_known_annual_gross_profit",
        "future_known_headcount",
    }.issubset(before_publication.reverse_review.blocker_codes)
    assert all(selection.selected for selection in at_publication.input_selections)


def test_e1_fails_closed_for_quality_listing_and_fx_controls() -> None:
    cases = _cases(run_e1_suite(REPOSITORY_ROOT))
    stale_run = cases["ddog-stale-low-confidence"].runs[0]
    stale_input = next(
        selection for selection in stale_run.input_selections if selection.metric == "annual_gross_profit"
    )
    assert stale_input.selected is False
    assert {
        "low_confidence_annual_gross_profit",
        "stale_annual_gross_profit",
    }.issubset(stale_run.reverse_review.blocker_codes)

    listing_run = cases["alphabet-dual-listing-identity"].runs[0]
    assert any("wrong_instrument_market_cap" in selection.rejection_codes for selection in listing_run.input_selections)

    fx_run = cases["nice-cross-currency-without-fx"].runs[0]
    assert "cross_currency_without_accepted_fx" in fx_run.reverse_review.blocker_codes


def test_e1_records_semantic_and_source_findings_without_fabricating_denominators() -> None:
    cases = _cases(run_e1_suite(REPOSITORY_ROOT))
    ddog_run = cases["ddog-provenance-free-success"].runs[0]
    ddog_result = ddog_run.factor_result
    assert ddog_result is not None
    assert ddog_result.level.reason_codes == ("missing_period_average_employee_count",)
    assert (
        FindingClass.SEMANTIC_DECISION,
        "point_headcount_not_period_average",
    ) in _finding_codes(cases["ddog-provenance-free-success"])
    assert (
        FindingClass.SEMANTIC_DECISION,
        "financial_tier_mapping_unapproved",
    ) in _finding_codes(cases["jpm-financial-proxy-branch"])
    assert (
        FindingClass.SOURCE_DATA_ISSUE,
        "no-total-headcount-disclosure",
    ) in _finding_codes(cases["nvda-missing-headcount"])
    assert (
        FindingClass.SEMANTIC_DECISION,
        "provisional_freshness_threshold_unapproved",
    ) in _finding_codes(cases["ddog-stale-low-confidence"])
    assert all(
        finding.classification is not FindingClass.CONTRACT_TOOLKIT_GAP
        for case in cases.values()
        for run in case.runs
        for finding in run.reverse_review.findings
    )


def test_e1_preserves_runner_only_provenance_and_confidence_identities() -> None:
    case = _cases(run_e1_suite(REPOSITORY_ROOT))["ddog-provenance-free-success"]
    run = case.runs[0]
    result = run.factor_result
    assert result is not None

    selected_headcount = next(
        selection for selection in run.input_selections if selection.input_kind == "headcount" and selection.selected
    )
    assert selected_headcount.source_identity.startswith("normalized-record:")
    assert selected_headcount.source_identity not in result.model_dump_json()
    assert selected_headcount.input_id in run.trace.selected_input_ids
    assert {item.input_id for item in run.confidence_evidence} == {
        item.input_id for item in run.input_selections if item.confidence is not None
    }


def test_e1_failed_evidence_is_distinct_content_and_append_only() -> None:
    accepted = run_e1_suite(REPOSITORY_ROOT)
    first_case = accepted.cases[0]
    run_values = first_case.runs[0].model_dump(mode="python", exclude={"run_id", "content_sha256"})
    run_values["passed"] = False
    failed_run = CoreTinyRunEvidence(**run_values)
    failed_case = CoreTinyCaseEvidence(
        case_id=first_case.case_id,
        strata=first_case.strata,
        passed=False,
        runs=(failed_run,),
    )
    values = accepted.model_dump(mode="python", exclude={"evidence_id", "content_sha256", "cases"})
    failed = CoreTinyEvidence(**values, cases=(failed_case, *accepted.cases[1:]))
    repository = InMemoryCoreTinyEvidenceRepository()

    assert failed.evidence_id != accepted.evidence_id
    assert repository.put(failed) is True
    assert repository.put(failed) is False
    assert repository.get(failed.evidence_id) == failed
    with pytest.raises(ValidationError, match="evidence_id does not match"):
        CoreTinyEvidence(
            **failed.model_dump(mode="python", exclude={"content_sha256", "evidence_id"}),
            evidence_id=accepted.evidence_id,
        )


def test_e1_public_golden_ranking_preserves_the_pinned_top_ten() -> None:
    run = _cases(run_e1_suite(REPOSITORY_ROOT))["public-golden-ordering-and-boundaries"].runs[0]
    assert run.ranking is not None
    assert run.ranking.selected_candidate_ids == (
        "candidate-01",
        "candidate-02",
        "candidate-03",
        "candidate-04",
        "candidate-05",
        "candidate-06",
        "candidate-07",
        "candidate-08",
        "candidate-09",
        "candidate-10",
    )
    assert len(run.input_selections) == 12
    assert all(selection.source_identity.startswith("public-golden:") for selection in run.input_selections)
