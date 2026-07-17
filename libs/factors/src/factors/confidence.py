"""Continuous confidence computation over versioned DataHub evidence contracts."""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext

from truealpha_contracts.confidence import (
    ConfidenceCalibrationReport,
    ContinuousConfidenceEvaluation,
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    OriginGroupContribution,
    SourceConfidenceEvidence,
    SourceConfidenceScore,
)


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimal_places))


def _power(value: Decimal, exponent: Decimal) -> Decimal:
    if value == 0:
        return Decimal(0)
    return (value.ln() * exponent).exp()


def _evaluation_context(precision: int) -> Context:
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def evaluate_continuous_confidence(
    policy: ContinuousConfidencePolicy,
    evidence: ContinuousConfidenceInput,
) -> ContinuousConfidenceEvaluation:
    """Evaluate the versioned formula without binary floating point or provider branching."""

    with localcontext(_evaluation_context(policy.calculation_precision)):
        prior_total = policy.reliability_prior_success + policy.reliability_prior_failure
        source_scores: list[SourceConfidenceScore] = []
        grouped: dict[str, list[tuple[SourceConfidenceEvidence, Decimal]]] = defaultdict(list)
        for source in evidence.sources:
            observed_mass = source.successful_outcome_mass + source.failed_outcome_mass
            reliability = (source.successful_outcome_mass + policy.reliability_prior_success) / (
                observed_mass + prior_total
            )
            if observed_mass == 0:
                reliability = min(reliability, policy.unobserved_reliability_ceiling)
            source_quality = reliability * source.freshness * source.sample_conformance * source.transport_integrity
            source_scores.append(
                SourceConfidenceScore(
                    source_evidence_id=source.source_evidence_id,
                    provider_id=source.provider_id,
                    origin_group_id=source.origin_group_id,
                    reliability=_quantize(reliability, policy.output_decimal_places),
                    source_quality=_quantize(source_quality, policy.output_decimal_places),
                )
            )
            grouped[source.origin_group_id].append((source, source_quality))

        origin_groups: list[OriginGroupContribution] = []
        evidence_mass = Decimal(0)
        for origin_group_id, candidates in grouped.items():
            selected, selected_quality = sorted(
                candidates,
                key=lambda item: (-item[1], item[0].source_evidence_id),
            )[0]
            effective_support = selected.independence_weight * selected_quality
            evidence_mass += effective_support
            origin_groups.append(
                OriginGroupContribution(
                    origin_group_id=origin_group_id,
                    independence_weight=selected.independence_weight,
                    selected_source_evidence_id=selected.source_evidence_id,
                    selected_source_quality=_quantize(selected_quality, policy.output_decimal_places),
                    effective_support=_quantize(effective_support, policy.output_decimal_places),
                )
            )

        source_support = Decimal(1) - (-evidence_mass).exp()
        quantized_evidence_mass = _quantize(evidence_mass, policy.output_decimal_places)
        quantized_source_support = _quantize(source_support, policy.output_decimal_places)
        confidence = (
            quantized_source_support
            * _power(evidence.agreement, policy.agreement_exponent)
            * _power(evidence.semantic_mapping_quality, policy.semantic_mapping_exponent)
            * _power(evidence.lineage_completeness, policy.lineage_exponent)
            * _power(evidence.required_component_completeness, policy.completeness_exponent)
        )
        confidence = _quantize(confidence, policy.output_decimal_places)
        score_100 = _quantize(confidence * Decimal(100), policy.output_decimal_places)

    reason_codes = {"confidence.evaluated"}
    if len(grouped) == 1:
        reason_codes.add("support.single-origin-ceiling")
    if len(evidence.sources) > len(grouped):
        reason_codes.add("support.same-origin-deduplicated")
    if any(source.successful_outcome_mass + source.failed_outcome_mass == 0 for source in evidence.sources):
        reason_codes.add("reliability.provisional-unobserved-ceiling")
    if evidence.agreement < 1:
        reason_codes.add("quality.source-disagreement")
    if evidence.semantic_mapping_quality < 1:
        reason_codes.add("quality.semantic-mapping-penalty")
    if evidence.lineage_completeness < 1:
        reason_codes.add("quality.lineage-penalty")
    if evidence.required_component_completeness < 1:
        reason_codes.add("quality.required-component-penalty")
    if any(source.transport_integrity == 0 for source in evidence.sources):
        reason_codes.add("evidence.raw-transport-missing")

    return ContinuousConfidenceEvaluation(
        policy_id=policy.policy_id,
        policy_sha256=policy.content_sha256,
        input_id=evidence.input_id,
        input_sha256=evidence.content_sha256,
        source_scores=tuple(source_scores),
        origin_groups=tuple(origin_groups),
        evidence_mass=quantized_evidence_mass,
        source_support=quantized_source_support,
        agreement=evidence.agreement,
        semantic_mapping_quality=evidence.semantic_mapping_quality,
        lineage_completeness=evidence.lineage_completeness,
        required_component_completeness=evidence.required_component_completeness,
        confidence=confidence,
        score_100=score_100,
        reason_codes=tuple(reason_codes),
    )


def verify_confidence_calibration_report(report: ConfidenceCalibrationReport) -> ConfidenceCalibrationReport:
    """Fail closed unless every embedded evaluation exactly matches the factor."""

    for scenario in report.scenarios:
        expected = evaluate_continuous_confidence(report.policy, scenario.input)
        if scenario.evaluation != expected:
            raise ValueError("every calibration evaluation must exactly reproduce from policy and input")
    return report
