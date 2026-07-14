from __future__ import annotations

import copy
import hashlib
import json
from decimal import ROUND_DOWN, ROUND_UP, Context, Decimal, getcontext, setcontext
from pathlib import Path

import pytest
from factors.batches.issuer_price_to_sales_tiny.kernel import (
    IssuerPriceToSalesRequest,
    IssuerPriceToSalesTinyResult,
    compute_issuer_price_to_sales,
)
from factors.batches.issuer_tier_valuation_tiny.kernel import (
    FROZEN_CORPUS_SHA256,
    PUBLIC_GOLDEN_MANIFEST_SHA256,
    S4_MANIFEST_SHA256,
    S5_TERMINAL_MANIFEST_SHA256,
    SEMANTIC_CANDIDATE_SHA256,
    IssuerTierValuationRequest,
    IssuerTierValuationTinyActivation,
    TierValuationAvailability,
    TierValuationReasonCode,
    compute_issuer_tier_valuation,
)
from factors.registry import FACTOR_REGISTRY
from pydantic import ValidationError
from truealpha_contracts.research import (
    IssuerPriceToSalesPolicy,
    LargeModelValueV0Binding,
    LargeModelValueV0Policy,
    StrategyCandidateUniverse,
    ValuationTier,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS_PATH = Path(__file__).with_name("fixtures") / "corpus.v1.json"


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _corpus() -> dict[str, object]:
    return _load_json(CORPUS_PATH)


def _binding() -> LargeModelValueV0Binding:
    refs = _corpus()["contract_refs"]
    assert isinstance(refs, dict)
    binding_ref = refs["binding"]
    assert isinstance(binding_ref, dict)
    binding_corpus = _load_json(ROOT / str(binding_ref["path"]))
    strategy_ref = binding_ref["base_strategy_ref"]
    assert isinstance(strategy_ref, dict)
    strategy_corpus = _load_json(ROOT / str(strategy_ref["path"]))
    definitions = strategy_corpus["valid_definitions"]
    assert isinstance(definitions, dict)
    strategy = LargeModelValueV0Policy.model_validate_json(json.dumps(definitions[strategy_ref["definition_key"]]))
    binding_payload = binding_corpus[binding_ref["binding_key"]]
    assert isinstance(binding_payload, dict)
    return LargeModelValueV0Binding(
        strategy=strategy,
        candidate_universe=StrategyCandidateUniverse.model_validate_json(
            json.dumps(binding_payload["candidate_universe"])
        ),
        price_to_sales_policy=IssuerPriceToSalesPolicy.model_validate_json(
            json.dumps(binding_payload["price_to_sales_policy"])
        ),
    )


def _s5_corpus() -> dict[str, object]:
    refs = _corpus()["contract_refs"]
    assert isinstance(refs, dict)
    ref = refs["s5_corpus"]
    assert isinstance(ref, dict)
    return _load_json(ROOT / str(ref["path"]))


def _s5_case(case_id: str) -> dict[str, object]:
    cases = _s5_corpus()["positive_cases"]
    assert isinstance(cases, list)
    found = next(item for item in cases if isinstance(item, dict) and item["case_id"] == case_id)
    return copy.deepcopy(found)


def _price_to_sales(binding: LargeModelValueV0Binding, case_id: str) -> IssuerPriceToSalesTinyResult:
    case = _s5_case(case_id)
    expected = case.pop("expected")
    case.pop("case_id")
    assert isinstance(expected, dict)
    request = IssuerPriceToSalesRequest.model_validate(
        {
            "activation": {"environment": "ci"},
            "strategy_binding_id": binding.strategy_binding_id,
            "candidate_universe_id": binding.candidate_universe.candidate_universe_id,
            "price_to_sales_policy_id": binding.price_to_sales_policy.price_to_sales_policy_id,
            "revenue_basis": expected["revenue_basis"],
            **case,
        }
    )
    return compute_issuer_price_to_sales(binding, request)


def _positive_case(case_id: str) -> dict[str, object]:
    cases = _corpus()["positive_cases"]
    assert isinstance(cases, list)
    found = next(item for item in cases if isinstance(item, dict) and item["case_id"] == case_id)
    return copy.deepcopy(found)


def _request_payload(binding: LargeModelValueV0Binding, case_id: str) -> dict[str, object]:
    case = _positive_case(case_id)
    case.pop("case_id")
    case.pop("expected")
    source_case_id = str(case.pop("source_price_to_sales_case_id"))
    price_to_sales = _price_to_sales(binding, source_case_id)
    gppe = case["gppe"]
    assert isinstance(gppe, dict)
    return {
        "activation": {"environment": "ci"},
        "strategy_binding_id": binding.strategy_binding_id,
        "candidate_universe_id": binding.candidate_universe.candidate_universe_id,
        "price_to_sales_policy_id": binding.price_to_sales_policy.price_to_sales_policy_id,
        "issuer_id": gppe["entity_id"],
        "cutoff": price_to_sales.as_of,
        "reporting_currency": price_to_sales.reporting_currency,
        "gppe": gppe,
        "price_to_sales": price_to_sales.model_dump(mode="json"),
    }


def _request(binding: LargeModelValueV0Binding, case_id: str) -> IssuerTierValuationRequest:
    return IssuerTierValuationRequest.model_validate(_request_payload(binding, case_id))


def _iter_artifact_refs(value: object):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            yield value
        for child in value.values():
            yield from _iter_artifact_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_artifact_refs(child)


def test_frozen_corpus_and_all_artifact_references_are_exact() -> None:
    assert hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest() == FROZEN_CORPUS_SHA256
    refs = _corpus()["contract_refs"]
    artifact_refs = list(_iter_artifact_refs(refs))
    assert len(artifact_refs) == 8
    for ref in artifact_refs:
        assert hashlib.sha256((ROOT / ref["path"]).read_bytes()).hexdigest() == ref["sha256"]

    assert hashlib.sha256((ROOT / "governance/batches/S4-issuer-price-sales.v1.json").read_bytes()).hexdigest() == (
        S4_MANIFEST_SHA256
    )
    assert (
        hashlib.sha256((ROOT / "governance/batches/S5-issuer-price-sales-kernel.v1.json").read_bytes()).hexdigest()
        == S5_TERMINAL_MANIFEST_SHA256
    )
    assert (
        hashlib.sha256(
            (ROOT / "governance/gate0/issue-59.research-semantics.candidate-v1.json").read_bytes()
        ).hexdigest()
        == SEMANTIC_CANDIDATE_SHA256
    )
    assert (
        hashlib.sha256((ROOT / "governance/gate0/public-goldens/manifest.v1.json").read_bytes()).hexdigest()
        == PUBLIC_GOLDEN_MANIFEST_SHA256
    )


def test_source_price_to_sales_results_match_the_frozen_terminal_s5_outputs() -> None:
    binding = _binding()
    frozen = _corpus()["source_price_to_sales_results"]
    assert isinstance(frozen, list) and len(frozen) == 2
    for expected in frozen:
        assert isinstance(expected, dict)
        result = _price_to_sales(binding, str(expected["source_case_id"]))
        assert result.issuer_price_to_sales_id == expected["issuer_price_to_sales_id"]
        assert result.content_sha256 == expected["content_sha256"]
        assert result.issuer_id == expected["issuer_id"]
        assert result.as_of.isoformat().replace("+00:00", "Z") == expected["as_of"]
        assert result.reporting_currency == expected["reporting_currency"]
        assert result.price_to_sales == Decimal(str(expected["price_to_sales"]))
        assert result.confidence == Decimal(str(expected["confidence"]))
        assert result.revenue_basis.value == expected["revenue_basis"]


@pytest.mark.parametrize("case", _corpus()["positive_cases"])
def test_all_positive_cases_match_frozen_tiers_gaps_and_confidence(case: object) -> None:
    assert isinstance(case, dict)
    binding = _binding()
    result = compute_issuer_tier_valuation(binding, _request(binding, str(case["case_id"])))
    expected = case["expected"]
    assert isinstance(expected, dict)

    assert result.availability is TierValuationAvailability.AVAILABLE
    assert result.tier is ValuationTier(str(expected["tier"]))
    assert result.target_ps_lower == Decimal(str(expected["target_ps_lower"]))
    assert result.target_ps_upper == Decimal(str(expected["target_ps_upper"]))
    assert result.target_ps_midpoint == Decimal(str(expected["target_ps_midpoint"]))
    assert result.current_price_to_sales == Decimal(str(expected["current_price_to_sales"]))
    assert result.valuation_gap == Decimal(str(expected["valuation_gap"]))
    assert result.confidence == Decimal(str(expected["confidence"]))
    assert result.reason_codes == ()
    assert result.issuer_tier_valuation_id == f"issuer-tier-valuation:{result.content_sha256}"
    assert "input:" not in result.model_dump_json()


def _negative_payload(
    binding: LargeModelValueV0Binding,
    case_id: str,
) -> tuple[dict[str, object], str]:
    cases = _corpus()["negative_cases"]
    assert isinstance(cases, list)
    case = next(item for item in cases if isinstance(item, dict) and item["case_id"] == case_id)
    payload = _request_payload(binding, str(case["base_case_id"]))
    expected_reason = str(case["expected_reason"])
    gppe = payload["gppe"]
    price_to_sales = payload["price_to_sales"]
    activation = payload["activation"]
    assert isinstance(gppe, dict)
    assert isinstance(price_to_sales, dict)
    assert isinstance(activation, dict)

    if case_id == "strategy-binding-id-mismatch":
        payload["strategy_binding_id"] = "strategy-binding:mismatch"
    elif case_id == "candidate-universe-id-mismatch":
        payload["candidate_universe_id"] = "strategy-candidate-universe:mismatch"
    elif case_id == "price-to-sales-policy-id-mismatch":
        payload["price_to_sales_policy_id"] = "price-to-sales-policy:mismatch"
    elif case_id == "issuer-id-mismatch":
        gppe["entity_id"] = "issuer.nvidia"
    elif case_id == "older-gppe-cutoff":
        gppe["as_of"] = "2026-04-01T19:59:59.999999Z"
    elif case_id == "future-gppe-cutoff":
        gppe["as_of"] = "2026-04-01T20:00:00.000001Z"
    elif case_id == "reporting-currency-mismatch":
        gppe["currency"] = "EUR"
        gppe["unit"] = "EUR_per_employee"
    elif case_id == "missing-gppe":
        payload["gppe"] = None
    elif case_id == "unavailable-gppe":
        gppe["availability"] = "unavailable"
        gppe["value"] = None
    elif case_id == "financial-comparison-observation":
        gppe["metric"] = "financial_efficiency"
    elif case_id == "unavailable-price-to-sales":
        price_to_sales["issuer_price_to_sales_id"] = ""
        price_to_sales["content_sha256"] = ""
        price_to_sales["availability"] = "unavailable"
        price_to_sales["price_to_sales"] = None
        price_to_sales["market_cap"] = None
        price_to_sales["revenue"] = None
        price_to_sales["confidence"] = "0"
        price_to_sales["reason_codes"] = ["unavailable_required_input"]
    elif case_id == "nonpositive-price-to-sales":
        price_to_sales["issuer_price_to_sales_id"] = ""
        price_to_sales["content_sha256"] = ""
        price_to_sales["price_to_sales"] = "0"
    elif case_id == "semantic-artifact-drift":
        activation["semantic_candidate_sha256"] = "f" * 64
    elif case_id == "provenance-field":
        gppe["source"] = "vendor"
        gppe["raw_ref"] = "s3://raw/object"
    elif case_id == "release-activation-field":
        activation["release_allowed"] = True
    else:
        raise AssertionError(f"unhandled frozen negative case {case_id}")
    return payload, expected_reason


def test_every_frozen_negative_case_fails_at_its_declared_boundary() -> None:
    binding = _binding()
    negative_cases = _corpus()["negative_cases"]
    assert isinstance(negative_cases, list) and len(negative_cases) == 15

    for case in negative_cases:
        assert isinstance(case, dict)
        payload, expected_reason = _negative_payload(binding, str(case["case_id"]))
        if expected_reason.endswith("_schema_rejected"):
            with pytest.raises(ValidationError):
                IssuerTierValuationRequest.model_validate(payload)
            continue
        request = IssuerTierValuationRequest.model_validate(payload)
        result = compute_issuer_tier_valuation(binding, request)
        assert result.availability is TierValuationAvailability.UNAVAILABLE, case["case_id"]
        assert result.reason_codes == (TierValuationReasonCode(expected_reason),), case["case_id"]
        assert result.tier is None
        assert result.target_ps_lower is None
        assert result.target_ps_upper is None
        assert result.target_ps_midpoint is None
        assert result.current_price_to_sales is None
        assert result.valuation_gap is None
        assert result.confidence == 0


def test_request_order_and_equivalent_timezones_preserve_semantic_identity() -> None:
    binding = _binding()
    payload = _request_payload(binding, "alphabet-exact-tech-boundary")
    original = IssuerTierValuationRequest.model_validate(payload)

    reordered_payload = dict(reversed(list(copy.deepcopy(payload).items())))
    gppe = reordered_payload["gppe"]
    assert isinstance(gppe, dict)
    reordered_payload["gppe"] = dict(reversed(list(gppe.items())))
    reordered = IssuerTierValuationRequest.model_validate(reordered_payload)
    assert reordered == original

    offset_payload = copy.deepcopy(payload)
    offset_payload["cutoff"] = "2026-04-02T04:00:00+08:00"
    offset_gppe = offset_payload["gppe"]
    offset_ps = offset_payload["price_to_sales"]
    assert isinstance(offset_gppe, dict)
    assert isinstance(offset_ps, dict)
    offset_gppe["as_of"] = "2026-04-02T04:00:00+08:00"
    offset_ps["as_of"] = "2026-04-02T04:00:00+08:00"
    offset_ps["issuer_price_to_sales_id"] = ""
    offset_ps["content_sha256"] = ""
    offset = IssuerTierValuationRequest.model_validate(offset_payload)

    assert offset == original
    assert compute_issuer_tier_valuation(binding, offset) == compute_issuer_tier_valuation(binding, original)


def test_ambient_decimal_context_cannot_change_output_or_content_identity() -> None:
    binding = _binding()
    request = _request(binding, "alphabet-exact-native-boundary")
    original_context = getcontext().copy()
    try:
        setcontext(Context(prec=6, rounding=ROUND_DOWN))
        low_precision = compute_issuer_tier_valuation(binding, request)
        setcontext(Context(prec=50, rounding=ROUND_UP))
        high_precision = compute_issuer_tier_valuation(binding, request)
    finally:
        setcontext(original_context)

    assert low_precision == high_precision
    assert low_precision.valuation_gap == Decimal("3.545454545454545454545454545")


def test_high_precision_gppe_is_not_rounded_across_a_tier_boundary() -> None:
    binding = _binding()
    payload = _request_payload(binding, "alphabet-below-tech-boundary")
    gppe = payload["gppe"]
    assert isinstance(gppe, dict)
    gppe["value"] = "999999.999999999999999999999999999"

    result = compute_issuer_tier_valuation(binding, IssuerTierValuationRequest.model_validate(payload))

    assert result.tier is ValuationTier.TRADITIONAL


@pytest.mark.parametrize("value", ("NaN", "Infinity", "-Infinity"))
def test_nonfinite_gppe_is_rejected_at_the_schema_boundary(value: str) -> None:
    binding = _binding()
    payload = _request_payload(binding, "alphabet-exact-tech-boundary")
    gppe = payload["gppe"]
    assert isinstance(gppe, dict)
    gppe["value"] = value

    with pytest.raises(ValidationError, match="finite number"):
        IssuerTierValuationRequest.model_validate(payload)


@pytest.mark.parametrize("value", ("NaN", "Infinity", "-Infinity"))
def test_nonfinite_price_to_sales_is_rejected_at_the_schema_boundary(value: str) -> None:
    binding = _binding()
    payload = _request_payload(binding, "alphabet-exact-tech-boundary")
    price_to_sales = payload["price_to_sales"]
    assert isinstance(price_to_sales, dict)
    price_to_sales["issuer_price_to_sales_id"] = ""
    price_to_sales["content_sha256"] = ""
    price_to_sales["price_to_sales"] = value

    with pytest.raises(ValidationError, match="finite number"):
        IssuerTierValuationRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("strategy_binding_id", "strategy-binding:mismatch", TierValuationReasonCode.BINDING_IDENTITY_MISMATCH),
        (
            "candidate_universe_id",
            "strategy-candidate-universe:mismatch",
            TierValuationReasonCode.CANDIDATE_UNIVERSE_IDENTITY_MISMATCH,
        ),
        (
            "price_to_sales_policy_id",
            "price-to-sales-policy:mismatch",
            TierValuationReasonCode.PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH,
        ),
        ("issuer_id", "issuer.nvidia", TierValuationReasonCode.ISSUER_IDENTITY_MISMATCH),
        ("as_of", "2026-04-01T19:59:59.999999Z", TierValuationReasonCode.CUTOFF_IDENTITY_MISMATCH),
        ("reporting_currency", "EUR", TierValuationReasonCode.REPORTING_CURRENCY_MISMATCH),
    ),
)
def test_composite_rejects_identity_drift_inside_the_s5_result(
    field: str,
    value: str,
    reason: TierValuationReasonCode,
) -> None:
    binding = _binding()
    payload = _request_payload(binding, "alphabet-exact-tech-boundary")
    price_to_sales = payload["price_to_sales"]
    assert isinstance(price_to_sales, dict)
    price_to_sales["issuer_price_to_sales_id"] = ""
    price_to_sales["content_sha256"] = ""
    price_to_sales[field] = value

    result = compute_issuer_tier_valuation(binding, IssuerTierValuationRequest.model_validate(payload))

    assert result.reason_codes == (reason,)


def test_activation_and_result_boundaries_remain_candidate_only() -> None:
    for field in ("live_source_allowed", "staging_allowed", "schedule_allowed", "release_allowed"):
        with pytest.raises(ValidationError):
            IssuerTierValuationTinyActivation(environment="ci", **{field: True})
    for field in (
        "s4_manifest_sha256",
        "s5_terminal_manifest_sha256",
        "s6_prepared_manifest_sha256",
        "frozen_corpus_sha256",
        "semantic_candidate_sha256",
        "public_golden_manifest_sha256",
    ):
        with pytest.raises(ValidationError, match="artifact identity drifted"):
            IssuerTierValuationTinyActivation(environment="ci", **{field: "f" * 64})

    binding = _binding()
    result = compute_issuer_tier_valuation(binding, _request(binding, "nvidia-exact-native-boundary"))
    assert type(result).model_validate_json(result.model_dump_json()) == result
    assert result.semantic_policy_state == "candidate_unapproved"
    assert result.stable_handoff is False
    assert result.release_eligible is False
    assert "issuer_tier_valuation_tiny" not in FACTOR_REGISTRY


def test_result_and_request_reject_identity_or_provenance_tampering() -> None:
    binding = _binding()
    request_payload = _request_payload(binding, "alphabet-exact-tech-boundary")
    request_payload["source"] = "vendor"
    with pytest.raises(ValidationError):
        IssuerTierValuationRequest.model_validate(request_payload)

    result = compute_issuer_tier_valuation(binding, _request(binding, "alphabet-exact-tech-boundary"))
    result_payload = result.model_dump(mode="json")
    result_payload["confidence"] = "0.5"
    with pytest.raises(ValidationError, match="identity mismatch"):
        type(result).model_validate(result_payload)

    result_payload = result.model_dump(mode="json")
    result_payload["valuation_gap"] = "999"
    result_payload["issuer_tier_valuation_id"] = ""
    result_payload["content_sha256"] = ""
    with pytest.raises(ValidationError, match="valuation gap"):
        type(result).model_validate(result_payload)

    gppe_payload = _request_payload(binding, "alphabet-exact-tech-boundary")
    gppe = gppe_payload["gppe"]
    assert isinstance(gppe, dict)
    gppe["unit"] = "EUR_per_employee"
    with pytest.raises(ValidationError, match="unit must match"):
        IssuerTierValuationRequest.model_validate(gppe_payload)
