import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from factors.batches.core_strategy_tiny import (
    CoreMetric,
    CoreObservation,
    CoreTinyActivation,
    CoreTinyRequest,
    H0HeadcountFactorInput,
    IssuerBranch,
    RankingCandidate,
    SubjectKind,
    ValuationTier,
    evaluate_core_tiny,
    rank_core_tiny_results,
    rank_provisional_candidates,
)
from factors.batches.core_strategy_tiny.e0_slice import (
    FROZEN_CORPUS_SHA256,
    H0_GOVERNANCE_HANDOFF_ID,
    H0_GOVERNANCE_HANDOFF_SHA256,
    H0_RUNTIME_HANDOFF_ID,
    H0_RUNTIME_HANDOFF_SHA256,
    PUBLIC_GOLDEN_MANIFEST_SHA256,
    SEMANTIC_CANDIDATE_SHA256,
)
from factors.registry import FACTOR_REGISTRY
from pydantic import ValidationError
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.release import ReleaseManifest

REPO_ROOT = Path(__file__).resolve().parents[5]
AS_OF = datetime(2026, 2, 27, tzinfo=UTC)
VALID_PERIOD = date(2025, 12, 31)


def _sha256(relative_path: str) -> str:
    return hashlib.sha256((REPO_ROOT / relative_path).read_bytes()).hexdigest()


def _load(relative_path: str) -> dict:
    return json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def _observation(
    metric: CoreMetric,
    value: str | None,
    *,
    entity_id: str = "issuer.example",
    subject_kind: SubjectKind = SubjectKind.ISSUER,
    unit: str = "USD",
    currency: str | None = "USD",
    confidence: str = "0.90",
    as_of: datetime = AS_OF,
    valid_period: date = VALID_PERIOD,
    availability_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
) -> CoreObservation:
    return CoreObservation(
        subject_kind=subject_kind,
        entity_id=entity_id,
        metric=metric,
        value=value,
        unit=unit,
        currency=currency,
        valid_period=valid_period,
        confidence=confidence,
        as_of=as_of,
        availability_status=availability_status,
    )


def _request(
    *observations: CoreObservation,
    issuer_id: str = "issuer.example",
    instrument_id: str = "instrument.us.example",
    branch: IssuerBranch = IssuerBranch.NON_FINANCIAL,
    reporting_currency: str = "USD",
    market_currency: str = "USD",
) -> CoreTinyRequest:
    return CoreTinyRequest(
        issuer_id=issuer_id,
        instrument_id=instrument_id,
        issuer_branch=branch,
        reporting_currency=reporting_currency,
        market_currency=market_currency,
        as_of=AS_OF,
        observations=observations,
    )


def _activation() -> CoreTinyActivation:
    return CoreTinyActivation(environment="ci")


def _non_financial_inputs(*, confidence: str = "0.80") -> tuple[CoreObservation, ...]:
    return (
        _observation(CoreMetric.ANNUAL_GROSS_PROFIT, "100000000", confidence="0.90"),
        _observation(
            CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
            "100",
            unit="employees",
            currency=None,
            confidence=confidence,
        ),
        _observation(
            CoreMetric.CURRENT_PS,
            "6",
            entity_id="instrument.us.example",
            subject_kind=SubjectKind.INSTRUMENT,
            unit="ratio",
            currency=None,
            confidence="0.95",
        ),
    )


def test_e0_artifact_pins_and_claim_ceiling_are_exact() -> None:
    assert _sha256("governance/handoffs/H0-core-headcount-extraction.v2.json") == H0_GOVERNANCE_HANDOFF_SHA256
    assert _sha256("governance/gate0/issue-59.research-semantics.candidate-v1.json") == SEMANTIC_CANDIDATE_SHA256
    assert _sha256("governance/gate0/public-goldens/manifest.v1.json") == PUBLIC_GOLDEN_MANIFEST_SHA256
    assert _sha256("libs/factors/tests/batches/core_strategy_tiny/fixtures/corpus.v1.json") == FROZEN_CORPUS_SHA256

    handoff = _load("governance/handoffs/H0-core-headcount-extraction.v2.json")
    candidate = _load("governance/gate0/issue-59.research-semantics.candidate-v1.json")
    public_manifest = _load("governance/gate0/public-goldens/manifest.v1.json")
    assert handoff["handoff_id"] == H0_GOVERNANCE_HANDOFF_ID
    assert candidate["state"] == "candidate_unapproved"
    assert public_manifest["evidence_class"] == "public_development_regression"
    assert public_manifest["claim_ceiling"] == "deterministic public-development regression evidence only"
    for case in public_manifest["cases"]:
        for artifact in case["artifacts"].values():
            assert _sha256(artifact["path"]) == artifact["sha256"]


def test_activation_rejects_staging_release_and_handoff_drift() -> None:
    activation = _activation()
    assert activation.governance_handoff_id == H0_GOVERNANCE_HANDOFF_ID
    assert activation.runtime_handoff_id == H0_RUNTIME_HANDOFF_ID
    assert activation.runtime_handoff_sha256 == H0_RUNTIME_HANDOFF_SHA256
    assert not activation.staging_allowed
    assert not activation.schedule_allowed
    assert not activation.release_allowed

    with pytest.raises(ValidationError):
        CoreTinyActivation(environment="staging")
    with pytest.raises(ValidationError):
        CoreTinyActivation(environment="ci", release_allowed=True)
    with pytest.raises(ValidationError):
        CoreTinyActivation(environment="ci", governance_handoff_sha256="0" * 64)


def test_strict_observation_rejects_factor_visible_provenance() -> None:
    payload = _observation(CoreMetric.ANNUAL_GROSS_PROFIT, "100").model_dump(mode="python")
    for forbidden in (
        "input_id",
        "normalized_record_id",
        "source",
        "raw_ref",
        "accession",
        "model",
        "prompt",
        "span",
        "review",
    ):
        with pytest.raises(ValidationError):
            CoreObservation.model_validate({**payload, forbidden: "forbidden"})
    with pytest.raises(ValidationError, match="matching currency unit"):
        CoreObservation.model_validate({**payload, "unit": "employees"})


def test_metric_units_and_request_currencies_are_strict() -> None:
    headcount = _observation(
        CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
        "100",
        unit="employees",
        currency=None,
    ).model_dump(mode="python")
    with pytest.raises(ValidationError, match="headcount observations require"):
        CoreObservation.model_validate({**headcount, "unit": "USD", "currency": "USD"})

    current_ps = _observation(
        CoreMetric.CURRENT_PS,
        "6",
        entity_id="instrument.us.example",
        subject_kind=SubjectKind.INSTRUMENT,
        unit="ratio",
        currency=None,
    ).model_dump(mode="python")
    with pytest.raises(ValidationError, match="current P/S observations require"):
        CoreObservation.model_validate({**current_ps, "unit": "USD", "currency": "USD"})

    with pytest.raises(ValidationError, match="annual_gross_profit currency"):
        _request(
            _observation(CoreMetric.ANNUAL_GROSS_PROFIT, "100", unit="EUR", currency="EUR"),
            reporting_currency="USD",
        )
    with pytest.raises(ValidationError, match="FX unit must match"):
        _request(
            _observation(
                CoreMetric.FX_REPORTING_TO_MARKET,
                "1.1",
                entity_id="instrument.us.example",
                subject_kind=SubjectKind.INSTRUMENT,
                unit="EUR_per_USD",
                currency=None,
            ),
            reporting_currency="EUR",
            market_currency="USD",
        )


def test_non_financial_e0_level_tier_and_gap_are_exact_and_deterministic() -> None:
    inputs = _non_financial_inputs()
    first = evaluate_core_tiny(activation=_activation(), request=_request(*inputs))
    reordered = evaluate_core_tiny(activation=_activation(), request=_request(*reversed(inputs)))
    local = evaluate_core_tiny(activation=CoreTinyActivation(environment="local"), request=_request(*inputs))

    assert first.level.availability_status is AvailabilityStatus.AVAILABLE
    assert first.level.metric == "gppe_level"
    assert first.level.value == Decimal("1000000")
    assert first.level.comparison_band == ValuationTier.TECH.value
    assert first.level.confidence == Decimal("0.80")
    assert first.valuation.tier is ValuationTier.TECH
    assert first.valuation.target_ps_midpoint == Decimal("9")
    assert first.valuation.current_ps == Decimal("6")
    assert first.valuation.current_ps_basis == "supplied_precomputed"
    assert first.valuation.current_ps_construction_state == "candidate_unapproved"
    assert first.valuation.valuation_gap == Decimal("0.5")
    assert first.valuation.confidence == Decimal("0.80")
    assert first.elasticity_status is AvailabilityStatus.UNAVAILABLE
    assert first.elasticity_reason_codes == ("insufficient_distinct_periods",)
    assert first.factor_validation_status is FactorValidationStatus.NOT_EVALUATED
    assert first.semantic_policy_state == "candidate_unapproved"
    assert first.claim_ceiling == "development_calibration_only"
    assert not first.stable_handoff
    assert not first.release_activation
    assert reordered.result_id == first.result_id
    assert reordered.content_sha256 == first.content_sha256
    assert local.result_id != first.result_id
    assert local.activation_sha256 != first.activation_sha256


@pytest.mark.parametrize("numerator", ["0", "-100000000"])
def test_zero_and_negative_profit_remain_in_the_lowest_declared_band(numerator: str) -> None:
    result = evaluate_core_tiny(
        activation=_activation(),
        request=_request(
            _observation(CoreMetric.ANNUAL_GROSS_PROFIT, numerator),
            _observation(
                CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
                "100",
                unit="employees",
                currency=None,
            ),
            _observation(
                CoreMetric.CURRENT_PS,
                "2",
                entity_id="instrument.us.example",
                subject_kind=SubjectKind.INSTRUMENT,
                unit="ratio",
                currency=None,
            ),
        ),
    )

    assert result.level.availability_status is AvailabilityStatus.AVAILABLE
    assert result.level.value == Decimal(numerator) / Decimal("100")
    assert result.level.comparison_band == ValuationTier.TRADITIONAL.value
    assert result.valuation.tier is ValuationTier.TRADITIONAL


@pytest.mark.parametrize(
    ("case_slug", "expected_band", "expected_midpoint"),
    [
        ("boundary-1000000", ValuationTier.TECH, Decimal("9")),
        ("boundary-3000000", ValuationTier.LARGE_MODEL_NATIVE, Decimal("25")),
    ],
)
def test_public_gppe_boundaries_enter_lower_inclusive_tiers(
    case_slug: str,
    expected_band: ValuationTier,
    expected_midpoint: Decimal,
) -> None:
    public_input = _load(f"governance/gate0/public-goldens/gppe/{case_slug}.input.json")["input"]
    expected = _load(f"governance/gate0/public-goldens/gppe/{case_slug}.expected.json")["expected"]
    result = evaluate_core_tiny(
        activation=_activation(),
        request=_request(
            _observation(CoreMetric.ANNUAL_GROSS_PROFIT, public_input["annual_gross_profit"]),
            _observation(
                CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
                public_input["period_average_employee_count"],
                unit="employees",
                currency=None,
            ),
            _observation(
                CoreMetric.CURRENT_PS,
                str(expected_midpoint),
                entity_id="instrument.us.example",
                subject_kind=SubjectKind.INSTRUMENT,
                unit="ratio",
                currency=None,
            ),
        ),
    )

    assert result.level.value == Decimal(expected["gppe_level"])
    assert result.level.comparison_band == expected_band.value == expected["gppe_band"]
    assert result.valuation.target_ps_midpoint == expected_midpoint
    assert result.valuation.valuation_gap == Decimal("0")


def test_public_missing_headcount_case_is_unavailable() -> None:
    public_input = _load("governance/gate0/public-goldens/gppe/missing-headcount.input.json")["input"]
    expected = _load("governance/gate0/public-goldens/gppe/missing-headcount.expected.json")["expected"]
    result = evaluate_core_tiny(
        activation=_activation(),
        request=_request(
            _observation(CoreMetric.ANNUAL_GROSS_PROFIT, public_input["annual_gross_profit"]),
        ),
    )

    assert result.level.availability_status.value == expected["status"]
    assert result.level.value is expected["gppe_level"]
    assert result.level.comparison_band is expected["gppe_band"]
    assert result.level.reason_codes == (expected["availability_reason"],)


def test_available_numeric_confidence_is_propagated_without_inventing_a_threshold() -> None:
    result = evaluate_core_tiny(
        activation=_activation(),
        request=_request(*_non_financial_inputs(confidence="0.10")),
    )

    assert result.level.availability_status is AvailabilityStatus.AVAILABLE
    assert result.level.confidence == Decimal("0.10")
    assert result.valuation.confidence == Decimal("0.10")


def test_h0_point_headcount_cannot_masquerade_as_period_average() -> None:
    wire_input = H0HeadcountFactorInput(
        entity_id="issuer.example",
        value="100",
        confidence="0.98",
        as_of=AS_OF,
        fiscal_period=VALID_PERIOD.isoformat(),
    )
    point_headcount = CoreObservation.from_h0_headcount(wire_input)
    result = evaluate_core_tiny(
        activation=_activation(),
        request=_request(
            _observation(CoreMetric.ANNUAL_GROSS_PROFIT, "100000000"),
            point_headcount,
        ),
    )

    assert point_headcount.metric is CoreMetric.EMPLOYEE_HEADCOUNT
    assert set(wire_input.model_dump()) == {"entity_id", "metric", "value", "confidence", "as_of", "fiscal_period"}
    assert result.level.availability_status is AvailabilityStatus.UNAVAILABLE
    assert result.level.value is None
    assert result.level.reason_codes == ("missing_period_average_employee_count",)
    assert result.valuation.reason_codes == ("missing_period_average_employee_count",)

    with pytest.raises(ValidationError):
        H0HeadcountFactorInput.model_validate({**wire_input.model_dump(mode="python"), "source": "forbidden"})


def test_financial_proxy_band_does_not_invent_a_valuation_tier() -> None:
    result = evaluate_core_tiny(
        activation=_activation(),
        request=_request(
            _observation(CoreMetric.PRE_PROVISION_PROFIT, "159256000000"),
            _observation(
                CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
                "318512",
                unit="employees",
                currency=None,
            ),
            branch=IssuerBranch.FINANCIAL,
        ),
    )

    assert result.level.availability_status is AvailabilityStatus.AVAILABLE
    assert result.level.metric == "financial_efficiency"
    assert result.level.value == Decimal("500000")
    assert result.level.comparison_band == "[500000,1000000)"
    assert result.valuation.availability_status is AvailabilityStatus.UNAVAILABLE
    assert result.valuation.tier is None
    assert result.valuation.reason_codes == ("financial_tier_mapping_unapproved",)


@pytest.mark.parametrize(
    ("availability", "expected_status", "expected_reason"),
    [
        (AvailabilityStatus.STALE, AvailabilityStatus.STALE, "stale_period_average_employee_count"),
        (
            AvailabilityStatus.LOW_CONFIDENCE,
            AvailabilityStatus.LOW_CONFIDENCE,
            "low_confidence_period_average_employee_count",
        ),
    ],
)
def test_upstream_stale_and_low_confidence_states_are_propagated(
    availability: AvailabilityStatus,
    expected_status: AvailabilityStatus,
    expected_reason: str,
) -> None:
    inputs = list(_non_financial_inputs())
    inputs[1] = _observation(
        CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
        "100",
        unit="employees",
        currency=None,
        availability_status=availability,
    )

    result = evaluate_core_tiny(activation=_activation(), request=_request(*inputs))

    assert result.level.availability_status is expected_status
    assert result.level.reason_codes == (expected_reason,)


def test_unresolved_cutoff_and_wrong_subject_inputs_fail_closed() -> None:
    future_inputs = list(_non_financial_inputs())
    future_inputs[0] = _observation(
        CoreMetric.ANNUAL_GROSS_PROFIT,
        "100000000",
        as_of=AS_OF + timedelta(microseconds=1),
    )
    wrong_subject_inputs = list(_non_financial_inputs())
    wrong_subject_inputs[2] = _observation(
        CoreMetric.CURRENT_PS,
        "6",
        entity_id="instrument.us.other",
        subject_kind=SubjectKind.INSTRUMENT,
        unit="ratio",
        currency=None,
    )

    with pytest.raises(ValidationError, match="already be PIT-resolved"):
        _request(*future_inputs)
    wrong_subject = evaluate_core_tiny(activation=_activation(), request=_request(*wrong_subject_inputs))

    assert wrong_subject.level.availability_status is AvailabilityStatus.AVAILABLE
    assert wrong_subject.valuation.reason_codes == ("wrong_subject_current_ps",)


def test_multiple_vintages_are_rejected_instead_of_selected_inside_the_factor() -> None:
    original = _observation(
        CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
        "100",
        unit="employees",
        currency=None,
    )
    amendment = _observation(
        CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
        "101",
        unit="employees",
        currency=None,
    )

    with pytest.raises(ValidationError, match="one PIT-resolved observation per metric"):
        _request(original, amendment)


def test_cross_currency_components_do_not_create_an_unapproved_current_ps() -> None:
    base = (
        _observation(CoreMetric.ANNUAL_GROSS_PROFIT, "100000000", unit="EUR", currency="EUR"),
        _observation(
            CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
            "100",
            unit="employees",
            currency=None,
        ),
    )
    revenue = _observation(CoreMetric.ANNUAL_REVENUE, "100000000", unit="EUR", currency="EUR")
    market_cap = _observation(
        CoreMetric.MARKET_CAP,
        "600000000",
        entity_id="instrument.us.example",
        subject_kind=SubjectKind.INSTRUMENT,
        currency="USD",
    )
    missing_fx = evaluate_core_tiny(
        activation=_activation(),
        request=_request(*base, revenue, market_cap, reporting_currency="EUR", market_currency="USD"),
    )
    fx = _observation(
        CoreMetric.FX_REPORTING_TO_MARKET,
        "1.1",
        entity_id="instrument.us.example",
        subject_kind=SubjectKind.INSTRUMENT,
        unit="USD_per_EUR",
        currency=None,
    )
    with_fx = evaluate_core_tiny(
        activation=_activation(),
        request=_request(*base, revenue, market_cap, fx, reporting_currency="EUR", market_currency="USD"),
    )

    assert missing_fx.valuation.reason_codes == ("cross_currency_without_accepted_fx",)
    assert with_fx.valuation.reason_codes == ("current_ps_construction_unapproved",)


def test_public_ranking_golden_is_exact_and_order_independent() -> None:
    payload = _load("governance/gate0/public-goldens/strategy/twelve-candidate-top-10.input.json")["input"]
    expected = _load("governance/gate0/public-goldens/strategy/twelve-candidate-top-10.expected.json")["expected"]
    candidates = tuple(RankingCandidate.model_validate(item) for item in payload["candidates"])

    result = rank_provisional_candidates(candidates)
    reordered = rank_provisional_candidates(tuple(reversed(candidates)))

    assert result.availability_status is AvailabilityStatus.AVAILABLE
    assert [item.candidate_id for item in result.ranking] == [item["candidate_id"] for item in expected["ranking"]]
    assert [item.gap for item in result.ranking] == [Decimal(item["gap"]) for item in expected["ranking"]]
    assert list(result.selected_candidate_ids) == expected["selected_top_10"]
    assert [item.model_dump(mode="json") for item in result.ineligible_candidates] == expected["ineligible_candidates"]
    assert result.factor_validation_status is FactorValidationStatus.NOT_EVALUATED
    assert reordered.ranking_id == result.ranking_id
    assert reordered.content_sha256 == result.content_sha256


def test_unapproved_ranking_tie_break_fails_closed() -> None:
    result = rank_provisional_candidates(
        tuple(
            [
                RankingCandidate(candidate_id="candidate-01", target_ps="10", current_ps="5"),
                RankingCandidate(candidate_id="candidate-02", target_ps="20", current_ps="10"),
            ]
            + [
                RankingCandidate(
                    candidate_id=f"candidate-{index:02d}",
                    target_ps=str(index * 10),
                    current_ps="10",
                )
                for index in range(3, 11)
            ]
        )
    )

    assert result.availability_status is AvailabilityStatus.UNAVAILABLE
    assert result.ranking == ()
    assert result.selected_candidate_ids == ()
    assert result.reason_codes == ("ranking_tie_break_unapproved",)


def test_ranking_fails_closed_with_fewer_than_ten_eligible_candidates() -> None:
    result = rank_provisional_candidates(
        (
            RankingCandidate(candidate_id="candidate-01", target_ps="10", current_ps="5"),
            RankingCandidate(candidate_id="candidate-02", target_ps="9", current_ps=None),
        )
    )

    assert result.availability_status is AvailabilityStatus.UNAVAILABLE
    assert result.ranking == ()
    assert result.selected_candidate_ids == ()
    assert result.reason_codes == ("insufficient_eligible_candidates",)
    assert result.ineligible_candidates[0].candidate_id == "candidate-02"


def test_strategy_ranking_consumes_exact_core_tiny_result_identities() -> None:
    results = []
    for index in range(1, 13):
        issuer_id = f"issuer.candidate{index:02d}"
        instrument_id = f"instrument.us.candidate{index:02d}"
        observations = [
            _observation(CoreMetric.ANNUAL_GROSS_PROFIT, "300000000", entity_id=issuer_id),
            _observation(
                CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
                "100",
                entity_id=issuer_id,
                unit="employees",
                currency=None,
            ),
        ]
        if index < 12:
            observations.append(
                _observation(
                    CoreMetric.CURRENT_PS,
                    str(index + 4),
                    entity_id=instrument_id,
                    subject_kind=SubjectKind.INSTRUMENT,
                    unit="ratio",
                    currency=None,
                )
            )
        results.append(
            evaluate_core_tiny(
                activation=_activation(),
                request=_request(
                    *observations,
                    issuer_id=issuer_id,
                    instrument_id=instrument_id,
                ),
            )
        )

    ranking = rank_core_tiny_results(tuple(reversed(results)))

    assert ranking.availability_status is AvailabilityStatus.AVAILABLE
    assert len(ranking.selected_candidate_ids) == 10
    assert set(ranking.input_result_ids) == {item.result_id for item in results}
    assert ranking.ineligible_candidates[0].candidate_id == "instrument.us.candidate12"
    assert ranking.ineligible_candidates[0].reason == "missing_current_ps"
    assert ranking.semantic_policy_state == "candidate_unapproved"
    assert ranking.claim_ceiling == "development_calibration_only"


def test_batch_import_does_not_register_or_activate_release_surfaces() -> None:
    assert "core_strategy_tiny" not in FACTOR_REGISTRY
    assert "large_model_value_v0" not in FACTOR_REGISTRY
    assert "three_tier_valuation_v0" not in FACTOR_REGISTRY

    fixture = _load("libs/contracts/conformance/issue58.fixtures.json")
    accepted_release = ReleaseManifest.model_validate(fixture["contracts"]["ReleaseManifest"])
    assert "core-strategy-tiny" not in accepted_release.configuration_sha256
    assert "large-model-value-v0" not in accepted_release.configuration_sha256
    with pytest.raises(ValidationError):
        evaluate_core_tiny(
            activation=cast(Any, accepted_release),
            request=_request(*_non_financial_inputs()),
        )
