from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.service_demand import (
    ConfidenceTargetBand,
    DataHubServiceDemand,
    DataQualityObjective,
    DemandIntakeReasonCode,
    DemandIntakeStatus,
    DemandRequester,
    DemandRequesterKind,
    DownstreamRecomputationHandoff,
    FieldSemanticExpectation,
    FieldValueKind,
    QualityReportDimension,
    RepresentativeSampleManifest,
    SampleArtifact,
    SampleAssertion,
    SampleAssertionOperator,
    SampleCase,
    ServiceRequirement,
    UnitBehavior,
    ValidTimeBehavior,
    evaluate_datahub_service_demand,
)
from truealpha_contracts.universe import SubjectKind, UniverseRef
from truealpha_contracts.usage import DataRequirement, RequirementLevel

FIXTURE = Path(__file__).parent / "fixtures" / "datahub_service_demand.v1.json"
AT = datetime(2026, 7, 17, tzinfo=UTC)


def _requirement() -> DataRequirement:
    return DataRequirement(
        capture_requirement_id=f"capture-requirement:{'1' * 64}",
        semantic_type_id="semantic.financial-fact",
        domain=DataDomain.FINANCIAL_FACTS,
        metric="gross_profit",
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=400),
        valid_period_rule_id="fiscal-period:annual",
        maximum_age=timedelta(days=2),
        cadence=timedelta(days=1),
    )


def _demand() -> DataHubServiceDemand:
    requirement = _requirement()
    field = FieldSemanticExpectation(
        requirement_id=requirement.requirement_id,
        field_name="gross_profit",
        definition="Revenue less cost of revenue for the reporting period.",
        value_kind=FieldValueKind.DECIMAL,
        required=True,
        nullable=False,
        unit_behavior=UnitBehavior.FROM_RECORD,
        valid_time_behavior=ValidTimeBehavior.REPORTING_PERIOD,
        knowable_time_rule_id="filing-publication-time:v1",
    )
    artifact = SampleArtifact(
        artifact_sha256="2" * 64,
        relative_path="samples/topt/financial-fact.json",
        media_type="application/json",
        byte_length=128,
    )
    assertion = SampleAssertion(
        requirement_id=requirement.requirement_id,
        field_name=field.field_name,
        operator=SampleAssertionOperator.EXACT,
        expected_value="210000000",
    )
    samples = RepresentativeSampleManifest(
        artifacts=(artifact,),
        cases=(
            SampleCase(
                case_name="nonfinancial-gross-profit",
                sample_artifact_id=artifact.sample_artifact_id,
                assertions=(assertion,),
            ),
        ),
    )
    quality = DataQualityObjective(
        minimum_coverage=Decimal("1"),
        minimum_availability=Decimal("0.95"),
        confidence_policy_id=f"confidence-policy:{'3' * 64}",
        confidence_policy_sha256="3" * 64,
        minimum_confidence_score=Decimal("70"),
        confidence_target_band=ConfidenceTargetBand.HIGH,
        minimum_independent_origin_groups=2,
        report_cadence=timedelta(days=1),
        report_dimensions=frozenset(QualityReportDimension),
    )
    return DataHubServiceDemand(
        requester=DemandRequester(
            kind=DemandRequesterKind.FACTOR,
            requester_id="factor:gross-profit-per-employee",
            requester_version="production-topt-v0.1.0",
            requester_definition_id=f"gppe-definition:{'4' * 64}",
            requester_definition_sha256="4" * 64,
        ),
        universe=UniverseRef(
            universe_id="universe:topt-candidate-v1",
            universe_version="2026-03-31-v1",
            content_sha256="5" * 64,
        ),
        requirements=(
            ServiceRequirement(
                data_requirement=requirement,
                refresh_cadence=timedelta(days=1),
                freshness_max_age=timedelta(days=2),
                fields=(field,),
            ),
        ),
        representative_samples=samples,
        quality_objective=quality,
        recomputation_handoffs=(
            DownstreamRecomputationHandoff(
                materialization_kind="factor-output:gppe",
                definition_id=f"gppe-definition:{'4' * 64}",
                definition_sha256="4" * 64,
                input_requirement_ids=(requirement.requirement_id,),
            ),
        ),
        effective_at=AT,
    )


def test_topt_service_demand_is_content_addressed_source_neutral_and_round_trips() -> None:
    demand = _demand()
    payload = demand.model_dump(mode="json")
    repeated = DataHubServiceDemand.model_validate(payload)

    assert repeated == demand
    assert demand.service_demand_id.endswith(demand.content_sha256)
    assert demand.quality_objective.minimum_confidence_score == Decimal("70")
    assert demand.quality_objective.minimum_independent_origin_groups == 2
    forbidden_keys = {"provider", "source", "credential", "host", "bucket", "database", "latest"}
    assert forbidden_keys.isdisjoint(payload)

    intake = evaluate_datahub_service_demand(payload)
    assert intake.status is DemandIntakeStatus.ACCEPTED
    assert intake.accepted_demand == demand
    assert intake.reason_codes == ()


def test_checked_in_topt_fixture_round_trips_to_the_same_identity() -> None:
    fixture = DataHubServiceDemand.model_validate(json.loads(FIXTURE.read_text()))

    assert fixture == _demand()


def test_intake_rejects_missing_sample_with_stable_non_secret_reason() -> None:
    payload = _demand().model_dump(mode="json")
    payload["representative_samples"]["artifacts"] = []

    report = evaluate_datahub_service_demand(payload)

    assert report.status is DemandIntakeStatus.REJECTED
    assert report.accepted_demand is None
    assert report.reason_codes == (DemandIntakeReasonCode.SAMPLE_INVALID,)
    assert "financial-fact.json" not in report.model_dump_json()


def test_high_confidence_target_requires_two_independent_origin_groups() -> None:
    payload = _demand().quality_objective.model_dump(mode="json", exclude={"quality_objective_id", "content_sha256"})
    payload["minimum_independent_origin_groups"] = 1

    with pytest.raises(ValidationError, match="at least two independent origin groups"):
        DataQualityObjective.model_validate(payload)


def test_service_requirement_rejects_cadence_or_freshness_drift() -> None:
    requirement = _requirement()
    field = _demand().requirements[0].fields[0]

    with pytest.raises(ValidationError, match="refresh cadence disagrees"):
        ServiceRequirement(
            data_requirement=requirement,
            refresh_cadence=timedelta(days=2),
            freshness_max_age=requirement.maximum_age,
            fields=(field,),
        )
    with pytest.raises(ValidationError, match="freshness maximum age disagrees"):
        ServiceRequirement(
            data_requirement=requirement,
            refresh_cadence=requirement.cadence,
            freshness_max_age=timedelta(days=3),
            fields=(field,),
        )


@pytest.mark.parametrize("relative_path", ("/tmp/sample.json", "../sample.json", "https://vendor/sample.json"))
def test_sample_artifact_rejects_unsafe_or_transport_specific_paths(relative_path: str) -> None:
    with pytest.raises(ValidationError, match="safe relative POSIX path"):
        SampleArtifact(
            artifact_sha256="6" * 64,
            relative_path=relative_path,
            media_type="application/json",
            byte_length=1,
        )


def test_demand_rejects_provider_or_infrastructure_fields() -> None:
    for forbidden_field in ("provider_id", "host", "bucket", "database", "latest"):
        payload = _demand().model_dump(mode="json")
        payload[forbidden_field] = "forbidden"
        report = evaluate_datahub_service_demand(payload)
        assert report.status is DemandIntakeStatus.REJECTED
        assert report.reason_codes == (DemandIntakeReasonCode.CROSS_CONTRACT_INVALID,)


def test_required_field_must_be_covered_by_a_sample_assertion() -> None:
    demand = _demand()
    artifact = demand.representative_samples.artifacts[0]
    assertion = SampleAssertion(
        requirement_id=demand.requirements[0].data_requirement.requirement_id,
        field_name="other_field",
        operator=SampleAssertionOperator.EXACT,
        expected_value="value",
    )
    samples = RepresentativeSampleManifest(
        artifacts=(artifact,),
        cases=(
            SampleCase(
                case_name="wrong-field",
                sample_artifact_id=artifact.sample_artifact_id,
                assertions=(assertion,),
            ),
        ),
    )
    payload = demand.model_dump(mode="json")
    payload["service_demand_id"] = ""
    payload["content_sha256"] = ""
    payload["representative_samples"] = samples.model_dump(mode="json")

    report = evaluate_datahub_service_demand(payload)

    assert report.status is DemandIntakeStatus.REJECTED
    assert DemandIntakeReasonCode.CROSS_CONTRACT_INVALID in report.reason_codes


def test_decimal_quality_targets_reject_binary_float() -> None:
    payload = _demand().quality_objective.model_dump(mode="json", exclude={"quality_objective_id", "content_sha256"})
    payload["minimum_confidence_score"] = 70.0

    with pytest.raises(ValidationError, match="binary float"):
        DataQualityObjective.model_validate(payload)
