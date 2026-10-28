from __future__ import annotations

from decimal import Decimal

import pytest
from factors.confidence import (
    evaluate_cell_confidence,
    evaluate_continuous_confidence,
    verify_confidence_calibration_report,
)
from pydantic import ValidationError
from truealpha_contracts.confidence import (
    ConfidenceCalibrationReport,
    ConfidenceCalibrationScenario,
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)
from truealpha_contracts.confidence_bands import confidence_band


def _input(case_id: str) -> ContinuousConfidenceInput:
    source = SourceConfidenceEvidence(
        provider_id="provider:primary",
        origin_group_id="origin:primary",
        independence_weight="1",
        successful_outcome_mass="8",
        failed_outcome_mass="2",
        freshness="1",
        sample_conformance="1",
        transport_integrity="1",
        evidence_ids=("evidence:primary:v1",),
        reason_codes=("measured",),
    )
    return ContinuousConfidenceInput(
        case_id=case_id,
        sources=(source,),
        agreement="1",
        semantic_mapping_quality="1",
        lineage_completeness="1",
        required_component_completeness="1",
    )


def test_report_verifier_recomputes_each_evaluation_from_its_exact_input() -> None:
    policy = ContinuousConfidencePolicy()
    confidence_input = _input("case:recomputed")
    evaluation = evaluate_continuous_confidence(policy, confidence_input)
    forged = type(evaluation)(
        **{
            **evaluation.model_dump(mode="python", exclude={"evaluation_id", "content_sha256", "source_support"}),
            "source_support": evaluation.source_support - Decimal("0.000001"),
        }
    )
    report = ConfidenceCalibrationReport(
        policy=policy,
        denominator_id="universe:test",
        denominator_size=1,
        empirically_observed_subject_ids=(),
        scenarios=(
            ConfidenceCalibrationScenario(
                scenario_id=confidence_input.case_id,
                evidence_class="sensitivity",
                expected_effect="The verifier must reject a denormalized decomposition.",
                input=confidence_input,
                evaluation=forged,
            ),
        ),
        limitations=("test-only",),
    )

    with pytest.raises(ValueError, match="exactly reproduce"):
        verify_confidence_calibration_report(report)


def test_the_evaluation_has_one_scale_and_no_score_100() -> None:
    evaluation = evaluate_continuous_confidence(ContinuousConfidencePolicy(), _input("case:one-scale"))
    assert "score_100" not in type(evaluation).model_fields
    assert "score_100" not in evaluation.model_dump(mode="json")
    payload = evaluation.model_dump(mode="python", exclude={"evaluation_id", "content_sha256"})
    with pytest.raises(ValidationError, match="score_100"):
        type(evaluation)(**payload, score_100=evaluation.confidence * 100)


def _prior_source(
    provider_id: str,
    origin_group_id: str,
    prior: Decimal,
    *,
    succ: str = "0",
    fail: str = "0",
) -> SourceConfidenceEvidence:
    return SourceConfidenceEvidence(
        provider_id=provider_id,
        origin_group_id=origin_group_id,
        independence_weight="1",
        reliability_prior=prior,
        successful_outcome_mass=succ,
        failed_outcome_mass=fail,
        freshness="1",
        sample_conformance="1",
        transport_integrity="1",
        evidence_ids=(f"evidence:{provider_id}",),
        reason_codes=("measured",),
    )


def test_source_reliability_prior_drives_band_ordering_and_outcome_mass_updates() -> None:
    sec = _prior_source("origin:sec-company-facts:v1", "lineage:sec-company-facts", Decimal("0.98"))
    moomoo = _prior_source("origin:moomoo-financials:v1", "lineage:moomoo", Decimal("0.96"))
    twelve = _prior_source("origin:twelve-data:v1", "lineage:twelve-data", Decimal("0.95"))
    yahoo = _prior_source("origin:yahoo:v1", "lineage:yahoo", Decimal("0.90"))
    prose = _prior_source("origin:sec-10k:v1", "lineage:sec-10k", Decimal("0.88"))

    c_sec = evaluate_cell_confidence(case_id="case:sec", sources=(sec,)).confidence
    c_moomoo = evaluate_cell_confidence(case_id="case:moomoo", sources=(moomoo,)).confidence
    c_twelve = evaluate_cell_confidence(case_id="case:twelve", sources=(twelve,)).confidence
    c_yahoo = evaluate_cell_confidence(case_id="case:yahoo", sources=(yahoo,)).confidence
    c_prose = evaluate_cell_confidence(case_id="case:prose", sources=(prose,)).confidence
    c_sec_moomoo = evaluate_cell_confidence(case_id="case:sec-moomoo", sources=(sec, moomoo)).confidence

    assert c_sec > c_moomoo > c_twelve > c_yahoo > c_prose
    assert confidence_band(c_sec_moomoo) == "high"
    assert confidence_band(c_sec) == "medium"
    assert confidence_band(c_yahoo) == "low"

    sec_with_failures = _prior_source(
        "origin:sec-company-facts:v1",
        "lineage:sec-company-facts",
        Decimal("0.98"),
        succ="0",
        fail="4",
    )
    c_sec_failed = evaluate_cell_confidence(case_id="case:sec-failed", sources=(sec_with_failures,)).confidence
    assert c_sec_failed < c_sec


def test_evaluate_cell_confidence_handles_definitional_and_missing_cells() -> None:
    definitional = evaluate_cell_confidence(case_id="case:release", definitional=True)
    assert definitional.confidence == Decimal("1")
    assert definitional.reason_codes == ("confidence.definitional",)
    assert len(definitional.policy_hash) == 64
    assert len(definitional.input_hash) == 64

    missing = evaluate_cell_confidence(
        case_id="case:missing", has_value=False, extra_reason_codes=("reason:no_origin_value",)
    )
    assert missing.confidence == Decimal("0")
    assert "confidence.no-origin-value" in missing.reason_codes
    assert "reason:no_origin_value" in missing.reason_codes
    assert len(missing.policy_hash) == 64
    assert len(missing.input_hash) == 64
