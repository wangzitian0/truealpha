from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal

import pytest
from factors.confidence import evaluate_continuous_confidence, verify_confidence_calibration_report
from pydantic import ValidationError
from truealpha_contracts.confidence import (
    ConfidenceCalibrationReport,
    ConfidenceCalibrationScenario,
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)


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


def test_score_projection_is_exact_beyond_evaluation_precision() -> None:
    policy = ContinuousConfidencePolicy()
    evaluation = evaluate_continuous_confidence(policy, _input("case:high-precision-projection"))
    confidence = Decimal("0." + ("1234567890" * 12))
    sign, digits, exponent = confidence.as_tuple()
    assert isinstance(exponent, int)
    exact_score = Decimal((sign, digits, exponent + 2))
    rounded_score = Context(prec=100, rounding=ROUND_HALF_EVEN).multiply(confidence, Decimal(100))
    payload = evaluation.model_dump(
        mode="python",
        exclude={"evaluation_id", "content_sha256", "confidence", "score_100"},
    )

    exact = type(evaluation)(**payload, confidence=confidence, score_100=exact_score)
    assert exact.score_100 == exact_score
    assert rounded_score != exact_score
    with pytest.raises(ValidationError, match="exact normalized confidence projection"):
        type(evaluation)(**payload, confidence=confidence, score_100=rounded_score)
