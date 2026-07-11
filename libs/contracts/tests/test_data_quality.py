from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts import (
    STRATEGY_DATA_REQUIREMENTS,
    DataQualityReport,
    EvidenceCase,
    QualityCheckResult,
    QualityStatus,
    ReadinessAssessment,
    ReadinessLevel,
    Strategy,
)


def test_requirement_catalog_covers_all_seven_factor_modules():
    covered = {strategy for requirement in STRATEGY_DATA_REQUIREMENTS for strategy in requirement.strategies}
    factor_strategies = set(Strategy) - {Strategy.BACKTEST_CORE}
    assert covered >= factor_strategies
    assert len(factor_strategies) == 7


def test_quality_report_rejects_duplicate_level_requirement_result():
    result = QualityCheckResult(
        requirement_id="prices.history",
        level=ReadinessLevel.LOCAL_BACKTEST,
        status=QualityStatus.FAIL,
        observed="364 days",
        expected="1095 days",
    )
    with pytest.raises(ValidationError, match="one result per requirement and readiness level"):
        DataQualityReport(
            generated_at=datetime.now(UTC),
            sample_root="samples",
            checks=(result, result),
            assessments=(
                ReadinessAssessment(level=ReadinessLevel.LOCAL_BACKTEST, ready=False, blockers=("prices.history",)),
            ),
        )


def test_evidence_case_requires_one_hash_per_safe_artifact_path():
    with pytest.raises(ValidationError, match="equal length"):
        EvidenceCase(
            evidence_id="evidence.test.case",
            requirement_id="test.requirement",
            kind="real",
            artifact_paths=("one.json", "two.json"),
            artifact_sha256=("0" * 64,),
            subject_entity_ids=("company:test",),
            assertion_ids=("test.assertion",),
            notes="Mismatched artifacts and hashes.",
        )

    with pytest.raises(ValidationError, match="sample root"):
        EvidenceCase(
            evidence_id="evidence.test.case",
            requirement_id="test.requirement",
            kind="real",
            artifact_paths=("../secret.json",),
            artifact_sha256=("0" * 64,),
            subject_entity_ids=("company:test",),
            assertion_ids=("test.assertion",),
            notes="Unsafe path.",
        )
