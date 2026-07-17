from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from data_engine.quality.confidence import build_topt_confidence_sensitivity_report
from pydantic import ValidationError
from truealpha_contracts.confidence import ConfidenceCalibrationReport


def test_topt_sensitivity_report_is_content_addressed_and_keeps_the_denominator() -> None:
    report = build_topt_confidence_sensitivity_report()
    repeated = build_topt_confidence_sensitivity_report()

    assert report == repeated
    assert report.report_id == "confidence-calibration-report:" + report.content_sha256
    assert report.denominator_size == 20
    assert report.empirically_observed_subject_ids == (
        "ticker:DDOG",
        "ticker:DUOL",
        "ticker:NICE",
        "ticker:SHOP",
    )
    assert len(report.scenarios) == 10
    assert report.claim_ceiling == "development_sensitivity_only"
    assert ConfidenceCalibrationReport.model_validate_json(report.model_dump_json()) == report
    with localcontext() as context:
        context.prec = 10
        assert build_topt_confidence_sensitivity_report() == report


def test_independent_support_is_continuous_and_same_origin_is_deduplicated() -> None:
    report = build_topt_confidence_sensitivity_report()
    scores = {scenario.scenario_id: scenario.evaluation.score_100 for scenario in report.scenarios}

    assert scores["topt.single-independent-source"] == Decimal("63.139100")
    assert scores["topt.same-origin-duplicate"] == scores["topt.single-independent-source"]
    assert scores["topt.two-independent-agreeing"] == Decimal("86.412800")
    assert scores["topt.three-independent-agreeing"] == Decimal("94.991600")
    assert (
        scores["topt.single-independent-source"]
        < scores["topt.two-independent-agreeing"]
        < scores["topt.three-independent-agreeing"]
        < Decimal("100")
    )


def test_quality_penalties_and_empirical_anchor_remain_explainable() -> None:
    report = build_topt_confidence_sensitivity_report()
    scenarios = {scenario.scenario_id: scenario for scenario in report.scenarios}

    assert scenarios["topt.stale-source"].evaluation.score_100 == Decimal("39.286900")
    assert scenarios["topt.semantic-mismatch"].evaluation.score_100 == Decimal("53.093500")
    assert scenarios["topt.partial-lineage"].evaluation.score_100 == Decimal("54.965800")
    assert scenarios["topt.missing-components"].evaluation.score_100 == Decimal("54.965800")
    assert scenarios["topt.cross-source-conflict"].evaluation.score_100 == Decimal("49.197000")

    empirical = scenarios["topt.yahoo-twelve-data-four-symbol-anchor"]
    assert empirical.evidence_class == "empirical_anchor"
    assert empirical.evaluation.score_100 == Decimal("74.597200")
    assert {source.reliability for source in empirical.evaluation.source_scores} == {Decimal("0.800000")}
    assert "reliability.provisional-unobserved-ceiling" in empirical.evaluation.reason_codes
    assert "quality.required-component-penalty" in empirical.evaluation.reason_codes


def test_evaluation_rejects_denormalized_score_or_identity_drift() -> None:
    evaluation = build_topt_confidence_sensitivity_report().scenarios[0].evaluation
    payload = evaluation.model_dump(mode="python", exclude={"evaluation_id", "content_sha256"})

    with pytest.raises(ValidationError, match="exact normalized confidence projection"):
        type(evaluation)(**{**payload, "score_100": evaluation.score_100 + Decimal("0.000001")})
    with pytest.raises(ValidationError, match="policy identity and hash"):
        type(evaluation)(**{**payload, "policy_sha256": "0" * 64})
