from __future__ import annotations

from decimal import Decimal

import pytest
from factors.confidence import (
    evaluate_cell_confidence,
    evaluate_continuous_confidence,
)
from pydantic import ValidationError
from truealpha_contracts.confidence import (
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)
from truealpha_contracts.confidence_bands import confidence_band

pytestmark = pytest.mark.contract


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


def test_modules_3_and_4_use_min_consumed_confidence() -> None:
    from datetime import UTC, datetime

    from factors.base.analyst_track_record import AnalystRatingItem, analyst_track_record
    from factors.base.supply_chain_exposure import SupplyChainPartner, supply_chain_exposure

    now = datetime(2026, 5, 1, tzinfo=UTC)
    sc = supply_chain_exposure(
        [
            SupplyChainPartner("p1", "Partner 1", "supplier", Decimal("0.20"), Decimal("0.90")),
            SupplyChainPartner("p2", "Partner 2", "supplier", Decimal("0.10"), Decimal("0.45")),
        ],
        entity_id="issuer:a",
        as_of=now,
        supplies_to_edges_exist=True,
    )
    assert sc.result.confidence == Decimal("0.45")

    ar = analyst_track_record(
        [
            AnalystRatingItem(analyst_id="a1", rating=5, confidence=Decimal("0.92")),
            AnalystRatingItem(analyst_id="a2", rating=4, confidence=Decimal("0.40")),
        ],
        entity_id="issuer:a",
        as_of=now,
    )
    assert ar.result.confidence == Decimal("0.40")


def test_theme_purity_allocates_small_eliminations_pro_rata_and_refuses_large() -> None:
    from datetime import UTC, datetime

    from factors.base.theme_purity import ThemeSegment, theme_purity

    now = datetime(2026, 5, 1, tzinfo=UTC)
    small_elim = theme_purity(
        [ThemeSegment("Cloud", Decimal("50"), True), ThemeSegment("Legacy", Decimal("50"), False)],
        entity_id="issuer:a",
        theme="AI",
        as_of=now,
        consolidated_revenue=Decimal("85"),
        partition_residual=Decimal("-15"),
        confidence=Decimal("0.75"),
        gross_segments=True,
    )
    assert small_elim.result.value == Decimal("0.5")
    assert small_elim.result.confidence == Decimal("0.75")
    assert "elimination_allocated_pro_rata" in small_elim.result.flags

    large_elim = theme_purity(
        [ThemeSegment("Cloud", Decimal("50"), True), ThemeSegment("Legacy", Decimal("50"), False)],
        entity_id="issuer:a",
        theme="AI",
        as_of=now,
        consolidated_revenue=Decimal("70"),
        partition_residual=Decimal("-30"),
        confidence=Decimal("0.75"),
        gross_segments=True,
    )
    assert large_elim.result.value is None
    assert large_elim.result.confidence == Decimal("0")
    assert "elimination_not_allocated" in large_elim.result.flags


def test_evaluate_cell_confidence_sanitizes_whitespace_case_ids_while_preserving_valid_ids() -> None:
    """Theme names with whitespace must normalize without failing ``StableId`` validation."""
    src = _prior_source("origin:sec-company-facts:v1", "lineage:sec-company-facts", Decimal("0.98"))
    theme_cell = evaluate_cell_confidence(
        case_id="theme:AI infrastructure:company:cik:59478:000005947826000013",
        sources=(src,),
    )
    assert theme_cell.evaluation is not None
    assert theme_cell.evaluation.confidence == theme_cell.confidence
    assert " " not in theme_cell.input_id

    valid_direct = ContinuousConfidenceInput(
        case_id="segment:company:cik:1045810:acc-1",
        sources=(src,),
        agreement="1",
        semantic_mapping_quality="1",
        lineage_completeness="1",
        required_component_completeness="1",
    )
    valid_via_cell = evaluate_cell_confidence(
        case_id="segment:company:cik:1045810:acc-1",
        sources=(src,),
    )
    assert valid_via_cell.input_id == valid_direct.input_id
    assert valid_via_cell.input_hash == valid_direct.content_sha256
