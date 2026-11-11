"""Continuous confidence computation over versioned DataHub evidence contracts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.confidence import (
    ConfidenceCalibrationReport,
    ContinuousConfidenceEvaluation,
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    OriginGroupContribution,
    SourceConfidenceEvidence,
    SourceConfidenceScore,
)

DEFAULT_CONFIDENCE_POLICY = ContinuousConfidencePolicy()


@dataclass(frozen=True)
class CellConfidence:
    confidence: Decimal
    policy_id: str
    policy_hash: str
    input_id: str
    input_hash: str
    reason_codes: tuple[str, ...]
    evaluation: ContinuousConfidenceEvaluation | None = None


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimal_places))


def _power(value: Decimal, exponent: Decimal) -> Decimal:
    return Decimal(0) if value == 0 else (value.ln() * exponent).exp()


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


def _confidence_reason_codes(
    evidence: ContinuousConfidenceInput,
    grouped_count: int,
) -> tuple[str, ...]:
    reasons = {"confidence.evaluated"}
    checks = (
        (grouped_count == 1, "support.single-origin-ceiling"),
        (len(evidence.sources) > grouped_count, "support.same-origin-deduplicated"),
        (
            any(s.successful_outcome_mass + s.failed_outcome_mass == 0 for s in evidence.sources),
            "reliability.provisional-unobserved-ceiling",
        ),
        (evidence.agreement < 1, "quality.source-disagreement"),
        (evidence.semantic_mapping_quality < 1, "quality.semantic-mapping-penalty"),
        (evidence.lineage_completeness < 1, "quality.lineage-penalty"),
        (evidence.required_component_completeness < 1, "quality.required-component-penalty"),
        (any(s.transport_integrity == 0 for s in evidence.sources), "evidence.raw-transport-missing"),
    )
    for triggered, code in checks:
        if triggered:
            reasons.add(code)
    return tuple(reasons)


def evaluate_continuous_confidence(
    policy: ContinuousConfidencePolicy,
    evidence: ContinuousConfidenceInput,
) -> ContinuousConfidenceEvaluation:
    with localcontext(_evaluation_context(policy.calculation_precision)):
        prior_total = policy.reliability_prior_success + policy.reliability_prior_failure
        source_scores: list[SourceConfidenceScore] = []
        grouped: dict[str, list[tuple[SourceConfidenceEvidence, Decimal]]] = defaultdict(list)
        for source in evidence.sources:
            observed_mass = source.successful_outcome_mass + source.failed_outcome_mass
            prior_success = (
                source.reliability_prior * prior_total
                if source.reliability_prior is not None
                else policy.reliability_prior_success
            )
            reliability = (source.successful_outcome_mass + prior_success) / (observed_mass + prior_total)
            if observed_mass == 0 and source.reliability_prior is None:
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
            selected, selected_quality = min(candidates, key=lambda item: (-item[1], item[0].source_evidence_id))
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

        quantized_evidence_mass = _quantize(evidence_mass, policy.output_decimal_places)
        quantized_source_support = _quantize(Decimal(1) - (-evidence_mass).exp(), policy.output_decimal_places)
        confidence = _quantize(
            quantized_source_support
            * _power(evidence.agreement, policy.agreement_exponent)
            * _power(evidence.semantic_mapping_quality, policy.semantic_mapping_exponent)
            * _power(evidence.lineage_completeness, policy.lineage_exponent)
            * _power(evidence.required_component_completeness, policy.completeness_exponent),
            policy.output_decimal_places,
        )

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
        reason_codes=_confidence_reason_codes(evidence, len(grouped)),
    )


def _static_cell_confidence(
    *,
    confidence: Decimal,
    policy: ContinuousConfidencePolicy,
    base_reason: str,
    extra_reason_codes: Sequence[str],
    payload: dict[str, object],
) -> CellConfidence:
    reasons = tuple(sorted({base_reason, *extra_reason_codes}))
    input_hash = canonical_sha256({**payload, "reason_codes": list(reasons)})
    return CellConfidence(
        confidence=confidence,
        policy_id=policy.policy_id,
        policy_hash=policy.content_sha256,
        input_id=f"confidence-input:{input_hash}",
        input_hash=input_hash,
        reason_codes=reasons,
        evaluation=None,
    )


def evaluate_cell_confidence(
    *,
    case_id: str,
    sources: Sequence[SourceConfidenceEvidence] = (),
    agreement: Decimal = Decimal("1"),
    semantic_mapping_quality: Decimal = Decimal("1"),
    lineage_completeness: Decimal = Decimal("1"),
    required_component_completeness: Decimal = Decimal("1"),
    definitional: bool = False,
    has_value: bool = True,
    extra_reason_codes: Sequence[str] = (),
    policy: ContinuousConfidencePolicy = DEFAULT_CONFIDENCE_POLICY,
) -> CellConfidence:
    if definitional:
        return _static_cell_confidence(
            confidence=Decimal("1"),
            policy=policy,
            base_reason="confidence.definitional",
            extra_reason_codes=extra_reason_codes,
            payload={"case_id": case_id, "definitional": True},
        )
    if not has_value or not sources:
        return _static_cell_confidence(
            confidence=Decimal("0"),
            policy=policy,
            base_reason="confidence.no-origin-value",
            extra_reason_codes=extra_reason_codes,
            payload={"case_id": case_id, "definitional": False, "has_value": False},
        )
    evidence = ContinuousConfidenceInput(
        case_id=case_id,
        sources=tuple(sources),
        agreement=agreement,
        semantic_mapping_quality=semantic_mapping_quality,
        lineage_completeness=lineage_completeness,
        required_component_completeness=required_component_completeness,
    )
    evaluation = evaluate_continuous_confidence(policy, evidence)
    return CellConfidence(
        confidence=evaluation.confidence,
        policy_id=evaluation.policy_id,
        policy_hash=evaluation.policy_sha256,
        input_id=evaluation.input_id,
        input_hash=evaluation.input_sha256,
        reason_codes=tuple(sorted({*evaluation.reason_codes, *extra_reason_codes})),
        evaluation=evaluation,
    )


def verify_confidence_calibration_report(report: ConfidenceCalibrationReport) -> ConfidenceCalibrationReport:
    for scenario in report.scenarios:
        if scenario.evaluation != evaluate_continuous_confidence(report.policy, scenario.input):
            raise ValueError("every calibration evaluation must exactly reproduce from policy and input")
    return report
