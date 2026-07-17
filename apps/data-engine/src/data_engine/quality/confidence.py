"""Deterministic continuous-confidence evaluation for DataHub evidence."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
from typing import Literal

from truealpha_contracts.confidence import (
    ConfidenceCalibrationReport,
    ConfidenceCalibrationScenario,
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


def evaluate_continuous_confidence(
    policy: ContinuousConfidencePolicy,
    evidence: ContinuousConfidenceInput,
) -> ContinuousConfidenceEvaluation:
    """Evaluate the versioned formula without binary floating point or source-name branching."""

    with localcontext() as context:
        context.prec = policy.calculation_precision
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
        confidence = (
            source_support
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

    return ContinuousConfidenceEvaluation(
        policy_id=policy.policy_id,
        policy_sha256=policy.content_sha256,
        input_id=evidence.input_id,
        input_sha256=evidence.content_sha256,
        source_scores=tuple(source_scores),
        origin_groups=tuple(origin_groups),
        evidence_mass=_quantize(evidence_mass, policy.output_decimal_places),
        source_support=_quantize(source_support, policy.output_decimal_places),
        agreement=evidence.agreement,
        semantic_mapping_quality=evidence.semantic_mapping_quality,
        lineage_completeness=evidence.lineage_completeness,
        required_component_completeness=evidence.required_component_completeness,
        confidence=confidence,
        score_100=score_100,
        reason_codes=tuple(reason_codes),
    )


def _source(
    case_id: str,
    provider_id: str,
    origin_group_id: str,
    *,
    independence_weight: str = "1",
    successful_outcome_mass: str = "1000",
    failed_outcome_mass: str = "0",
    freshness: str = "1",
) -> SourceConfidenceEvidence:
    return SourceConfidenceEvidence(
        provider_id=provider_id,
        origin_group_id=origin_group_id,
        independence_weight=Decimal(independence_weight),
        successful_outcome_mass=Decimal(successful_outcome_mass),
        failed_outcome_mass=Decimal(failed_outcome_mass),
        freshness=Decimal(freshness),
        sample_conformance=Decimal(1),
        transport_integrity=Decimal(1),
        evidence_ids=(f"sample-evidence:{case_id}:{provider_id}",),
        reason_codes=("sample-evidence.measured",),
    )


def _evaluate_case(
    policy: ContinuousConfidencePolicy,
    case_id: str,
    sources: tuple[SourceConfidenceEvidence, ...],
    *,
    agreement: str = "1",
    semantic_mapping_quality: str = "1",
    lineage_completeness: str = "1",
    required_component_completeness: str = "1",
) -> ContinuousConfidenceEvaluation:
    return evaluate_continuous_confidence(
        policy,
        ContinuousConfidenceInput(
            case_id=case_id,
            sources=sources,
            agreement=Decimal(agreement),
            semantic_mapping_quality=Decimal(semantic_mapping_quality),
            lineage_completeness=Decimal(lineage_completeness),
            required_component_completeness=Decimal(required_component_completeness),
        ),
    )


def build_topt_confidence_sensitivity_report() -> ConfidenceCalibrationReport:
    """Build the reviewable v0.1 report without claiming full-TOPT empirical calibration."""

    policy = ContinuousConfidencePolicy()
    scenarios: list[ConfidenceCalibrationScenario] = []

    def add(
        scenario_id: str,
        expected_effect: str,
        sources: tuple[SourceConfidenceEvidence, ...],
        *,
        evidence_class: Literal["sensitivity", "empirical_anchor"] = "sensitivity",
        agreement: str = "1",
        semantic_mapping_quality: str = "1",
        lineage_completeness: str = "1",
        required_component_completeness: str = "1",
    ) -> None:
        scenarios.append(
            ConfidenceCalibrationScenario(
                scenario_id=scenario_id,
                evidence_class=evidence_class,
                expected_effect=expected_effect,
                evaluation=_evaluate_case(
                    policy,
                    scenario_id,
                    sources,
                    agreement=agreement,
                    semantic_mapping_quality=semantic_mapping_quality,
                    lineage_completeness=lineage_completeness,
                    required_component_completeness=required_component_completeness,
                ),
            )
        )

    add(
        "topt.single-independent-source",
        "One near-perfect origin remains capped near 63 support points.",
        (_source("single", "provider:primary", "origin:primary"),),
    )
    add(
        "topt.two-independent-agreeing",
        "A second independent agreeing origin raises support without reaching certainty.",
        (
            _source("two", "provider:primary", "origin:primary"),
            _source("two", "provider:secondary", "origin:secondary"),
        ),
    )
    add(
        "topt.three-independent-agreeing",
        "Three independent agreeing origins approach but do not equal 100.",
        (
            _source("three", "provider:primary", "origin:primary"),
            _source("three", "provider:secondary", "origin:secondary"),
            _source("three", "provider:tertiary", "origin:tertiary"),
        ),
    )
    add(
        "topt.same-origin-duplicate",
        "A mirror of the primary origin contributes no second unit of support.",
        (
            _source("same-origin", "provider:primary", "origin:primary"),
            _source("same-origin", "provider:mirror", "origin:primary"),
        ),
    )
    add(
        "topt.stale-source",
        "Cadence-relative freshness decay reduces source evidence continuously.",
        (_source("stale", "provider:primary", "origin:primary", freshness="0.5"),),
    )
    add(
        "topt.semantic-mismatch",
        "Ambiguous mapping or definition drift lowers the semantic dimension.",
        (_source("semantic", "provider:primary", "origin:primary"),),
        semantic_mapping_quality="0.5",
    )
    add(
        "topt.partial-lineage",
        "Missing provenance edges lower confidence without erasing the observation.",
        (_source("lineage", "provider:primary", "origin:primary"),),
        lineage_completeness="0.5",
    )
    add(
        "topt.missing-components",
        "An incomplete demanded record is penalized instead of removed from the denominator.",
        (_source("completeness", "provider:primary", "origin:primary"),),
        required_component_completeness="0.5",
    )
    add(
        "topt.cross-source-conflict",
        "Independent support cannot hide material cross-source disagreement.",
        (
            _source("conflict", "provider:primary", "origin:primary"),
            _source("conflict", "provider:secondary", "origin:secondary"),
        ),
        agreement="0.2",
    )

    empirical_case = "topt.yahoo-twelve-data-four-symbol-anchor"
    add(
        empirical_case,
        "Four symbols anchor agreement, while missing adjusted close and actions keep the result provisional.",
        (
            _source(
                "empirical",
                "provider:yahoo-chart",
                "origin:yahoo-chart",
                successful_outcome_mass="0",
            ),
            _source(
                "empirical",
                "provider:twelve-data",
                "origin:twelve-data",
                successful_outcome_mass="0",
            ),
        ),
        evidence_class="empirical_anchor",
        agreement="0.999270557029",
        required_component_completeness="0.714285714286",
    )

    return ConfidenceCalibrationReport(
        policy=policy,
        denominator_id="universe:topt-us-2026-03-31",
        denominator_size=20,
        empirically_observed_subject_ids=("ticker:DDOG", "ticker:DUOL", "ticker:NICE", "ticker:SHOP"),
        scenarios=tuple(scenarios),
        limitations=(
            "Independent Yahoo/Twelve Data overlap covers four symbols, not the complete TOPT denominator.",
            "The report is sensitivity evidence and does not freeze a Production threshold.",
            "Adjusted close and corporate-action reconciliation are absent from the empirical anchor.",
            "Full TOPT calibration must retain all twenty issuers and report missing second-source evidence.",
        ),
    )
