from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from factors.batches.issuer_price_to_sales_tiny.kernel import (
    FROZEN_CORPUS_SHA256,
    FxRateObservation,
    InputAvailability,
    IssuerPriceToSalesRequest,
    IssuerPriceToSalesTinyActivation,
    ReasonCode,
    ResultAvailability,
    RevenueBasis,
    compute_issuer_price_to_sales,
)
from factors.registry import FACTOR_REGISTRY
from pydantic import ValidationError
from truealpha_contracts.research import (
    IssuerPriceToSalesPolicy,
    LargeModelValueV0Binding,
    LargeModelValueV0Policy,
    StrategyCandidateUniverse,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS_PATH = Path(__file__).with_name("fixtures") / "corpus.v1.json"
S4_CORPUS_PATH = ROOT / "libs/contracts/tests/fixtures/issuer_price_to_sales.v1.json"
STRATEGY_CORPUS_PATH = ROOT / "libs/contracts/tests/fixtures/large_model_value_v0.v1.json"


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _corpus() -> dict[str, object]:
    return _load_json(CORPUS_PATH)


def _binding() -> LargeModelValueV0Binding:
    corpus = _corpus()
    binding_ref = corpus["binding_ref"]
    assert isinstance(binding_ref, dict)
    s4 = _load_json(ROOT / str(binding_ref["path"]))
    base_ref = binding_ref["base_strategy_ref"]
    assert isinstance(base_ref, dict)
    strategy_corpus = _load_json(ROOT / str(base_ref["path"]))
    definitions = strategy_corpus["valid_definitions"]
    assert isinstance(definitions, dict)
    strategy = LargeModelValueV0Policy.model_validate_json(json.dumps(definitions[base_ref["definition_key"]]))
    binding_payload = s4[binding_ref["binding_key"]]
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


def _case(case_id: str) -> dict[str, object]:
    cases = _corpus()["positive_cases"]
    assert isinstance(cases, list)
    found = next(item for item in cases if isinstance(item, dict) and item["case_id"] == case_id)
    return copy.deepcopy(found)


def _request_payload(binding: LargeModelValueV0Binding, case_id: str) -> dict[str, object]:
    case = _case(case_id)
    expected = case.pop("expected")
    case.pop("case_id")
    assert isinstance(expected, dict)
    return {
        "activation": {"environment": "ci"},
        "strategy_binding_id": binding.strategy_binding_id,
        "candidate_universe_id": binding.candidate_universe.candidate_universe_id,
        "price_to_sales_policy_id": binding.price_to_sales_policy.price_to_sales_policy_id,
        "revenue_basis": expected["revenue_basis"],
        **case,
    }


def _request(binding: LargeModelValueV0Binding, case_id: str) -> IssuerPriceToSalesRequest:
    return IssuerPriceToSalesRequest.model_validate(_request_payload(binding, case_id))


def test_frozen_corpus_and_contract_references_are_exact() -> None:
    corpus_bytes = CORPUS_PATH.read_bytes()
    assert hashlib.sha256(corpus_bytes).hexdigest() == FROZEN_CORPUS_SHA256
    corpus = _corpus()
    binding_ref = corpus["binding_ref"]
    assert isinstance(binding_ref, dict)
    assert hashlib.sha256(S4_CORPUS_PATH.read_bytes()).hexdigest() == binding_ref["sha256"]
    base_ref = binding_ref["base_strategy_ref"]
    assert isinstance(base_ref, dict)
    assert hashlib.sha256(STRATEGY_CORPUS_PATH.read_bytes()).hexdigest() == base_ref["sha256"]


@pytest.mark.parametrize(
    "case_id",
    ("dual-class-four-quarter-fx", "single-class-fiscal-year-fallback"),
)
def test_positive_cases_match_frozen_decimal_outputs(case_id: str) -> None:
    binding = _binding()
    request = _request(binding, case_id)
    expected = _case(case_id)["expected"]
    assert isinstance(expected, dict)

    result = compute_issuer_price_to_sales(binding, request)

    assert result.availability is ResultAvailability.AVAILABLE
    assert result.market_cap == Decimal(str(expected["market_cap"]))
    assert result.revenue == Decimal(str(expected["revenue"]))
    assert result.price_to_sales == Decimal(str(expected["price_to_sales"]))
    assert result.confidence == Decimal(str(expected["confidence"]))
    assert result.revenue_basis is RevenueBasis(str(expected["revenue_basis"]))
    assert result.component_count == expected["component_count"]
    assert (
        sum(len(group) for group in (request.prices, request.shares, request.revenues, request.fx_rates))
        == expected["consumed_input_count"]
    )
    assert result.issuer_price_to_sales_id == f"issuer-price-to-sales:{result.content_sha256}"
    assert "input:" not in result.model_dump_json()


def test_dual_class_market_cap_uses_both_components_once() -> None:
    binding = _binding()
    result = compute_issuer_price_to_sales(binding, _request(binding, "dual-class-four-quarter-fx"))

    # USD class: 100 * 10; EUR class: 50 * 20 * 1.2.
    assert result.market_cap == Decimal("2200")
    assert result.component_count == 2


def test_reordering_inputs_and_components_preserves_semantic_identity() -> None:
    binding = _binding()
    request_payload = _request_payload(binding, "dual-class-four-quarter-fx")
    original = IssuerPriceToSalesRequest.model_validate(request_payload)
    reordered_payload = copy.deepcopy(request_payload)
    for field in ("prices", "shares", "revenues", "fx_rates"):
        values = reordered_payload[field]
        assert isinstance(values, list)
        values.reverse()

    reordered_binding_payload = json.loads(binding.model_dump_json())
    candidates = reordered_binding_payload["candidate_universe"]["candidates"]
    candidates.reverse()
    alphabet = next(item for item in candidates if item["issuer"]["id"] == "issuer.alphabet")
    alphabet["market_value_components"].reverse()
    reordered_binding = LargeModelValueV0Binding.model_validate_json(json.dumps(reordered_binding_payload))
    reordered = IssuerPriceToSalesRequest.model_validate(reordered_payload)

    assert reordered.model_dump_json() == original.model_dump_json()
    assert reordered_binding.strategy_binding_id == binding.strategy_binding_id
    assert compute_issuer_price_to_sales(binding, original) == compute_issuer_price_to_sales(
        reordered_binding,
        reordered,
    )


def _negative_request(
    binding: LargeModelValueV0Binding,
    case_id: str,
) -> tuple[IssuerPriceToSalesRequest | None, ReasonCode | None]:
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    expected_reason = next(
        item["expected_reason"]
        for item in _corpus()["negative_cases"]
        if isinstance(item, dict) and item["case_id"] == case_id
    )
    if case_id == "binding-id-mismatch":
        payload["strategy_binding_id"] = "strategy-binding:mismatch"
    elif case_id == "candidate-universe-id-mismatch":
        payload["candidate_universe_id"] = "strategy-candidate-universe:mismatch"
    elif case_id == "price-to-sales-policy-id-mismatch":
        payload["price_to_sales_policy_id"] = "price-to-sales-policy:mismatch"
    elif case_id == "missing-share-class-price":
        payload["prices"] = payload["prices"][:1]
    elif case_id == "duplicate-component-price":
        duplicate = copy.deepcopy(payload["prices"][0])
        duplicate["input_id"] = "input:price:alphabet-a:duplicate"
        payload["prices"].append(duplicate)
    elif case_id == "future-price-knowledge":
        payload["prices"][0]["knowable_at"] = "2026-04-02T20:00:00Z"
    elif case_id == "shares-effective-after-price-date":
        payload["shares"][0]["effective_on"] = "2026-04-01"
    elif case_id == "future-shares-knowledge":
        payload["shares"][0]["knowable_at"] = "2026-04-02T20:00:00Z"
    elif case_id == "wrong-price-listing":
        payload["prices"][0]["listing_id"] = "listing.xnys.unbound"
    elif case_id == "missing-fx":
        payload["fx_rates"] = []
    elif case_id == "fx-session-mismatch":
        payload["fx_rates"][0]["session_close_at"] = "2026-03-30T20:00:00Z"
    elif case_id == "incomplete-quarter-window-without-fiscal-year":
        payload["revenues"] = payload["revenues"][:3]
    elif case_id == "future-revenue-knowledge":
        payload["revenues"][0]["knowable_at"] = "2026-04-02T20:00:00Z"
    elif case_id == "nonpositive-revenue":
        for revenue in payload["revenues"]:
            revenue["value"] = "0"
    elif case_id == "unavailable-required-input":
        payload["prices"][0]["availability"] = "unavailable"
        payload["prices"][0]["value"] = None
    elif case_id == "vendor-precomputed-ratio":
        payload["precomputed_price_to_sales"] = "5.5"
        with pytest.raises(ValidationError):
            IssuerPriceToSalesRequest.model_validate(payload)
        assert expected_reason == "request_schema_rejected"
        return None, None
    else:
        raise AssertionError(f"unhandled frozen negative case {case_id}")
    return IssuerPriceToSalesRequest.model_validate(payload), ReasonCode(str(expected_reason))


def test_every_frozen_negative_case_fails_closed_with_its_reason() -> None:
    binding = _binding()
    negative_cases = _corpus()["negative_cases"]
    assert isinstance(negative_cases, list) and len(negative_cases) == 16

    for case in negative_cases:
        assert isinstance(case, dict)
        request, expected_reason = _negative_request(binding, str(case["case_id"]))
        if request is None:
            continue
        result = compute_issuer_price_to_sales(binding, request)
        assert result.availability is ResultAvailability.UNAVAILABLE
        assert result.reason_codes == (expected_reason,), case["case_id"]
        assert result.price_to_sales is None
        assert result.market_cap is None
        assert result.revenue is None
        assert result.confidence == 0


def test_invalid_units_and_non_pit_times_are_rejected_at_the_request_boundary() -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    payload["prices"][0]["unit"] = "USD"
    with pytest.raises(ValidationError):
        IssuerPriceToSalesRequest.model_validate(payload)

    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    payload["cutoff"] = "2026-04-01T20:00:00"
    with pytest.raises(ValidationError, match="timezone-aware"):
        IssuerPriceToSalesRequest.model_validate(payload)

    for field, invalid_basis in (
        ("prices", {"price_basis": "adjusted_close"}),
        ("shares", {"corporate_action_basis": "split_adjusted_shares"}),
    ):
        payload = _request_payload(binding, "dual-class-four-quarter-fx")
        payload[field][0].update(invalid_basis)
        with pytest.raises(ValidationError):
            IssuerPriceToSalesRequest.model_validate(payload)


def test_activation_cannot_enable_live_staging_schedule_or_release() -> None:
    for field in ("live_source_allowed", "staging_allowed", "schedule_allowed", "release_allowed"):
        with pytest.raises(ValidationError):
            IssuerPriceToSalesTinyActivation(environment="ci", **{field: True})

    with pytest.raises(ValidationError, match="artifact identity drifted"):
        IssuerPriceToSalesTinyActivation(environment="ci", frozen_corpus_sha256="f" * 64)


def test_unused_fx_is_rejected_and_output_round_trip_preserves_identity() -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    extra = copy.deepcopy(payload["fx_rates"][0])
    extra["input_id"] = "input:fx:gbp-usd:2026-03-31-close"
    extra["from_currency"] = "GBP"
    payload["fx_rates"].append(extra)
    request = IssuerPriceToSalesRequest.model_validate(payload)
    rejected = compute_issuer_price_to_sales(binding, request)
    assert rejected.reason_codes == (ReasonCode.UNEXPECTED_FX_RATE,)

    accepted = compute_issuer_price_to_sales(binding, _request(binding, "dual-class-four-quarter-fx"))
    restored = type(accepted).model_validate_json(accepted.model_dump_json())
    assert restored == accepted


def test_future_fx_is_distinct_from_missing_fx() -> None:
    binding = _binding()
    request = _request(binding, "dual-class-four-quarter-fx")
    future_fx = request.fx_rates[0].model_copy(update={"knowable_at": request.cutoff + timedelta(seconds=1)})
    payload = request.model_dump(mode="python")
    payload["fx_rates"] = (FxRateObservation.model_validate(future_fx),)
    future_request = IssuerPriceToSalesRequest.model_validate(payload)

    result = compute_issuer_price_to_sales(binding, future_request)
    assert result.reason_codes == (ReasonCode.FUTURE_KNOWN_FX,)


def test_available_input_cannot_hide_a_missing_value() -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    payload["prices"][0]["value"] = None
    payload["prices"][0]["availability"] = InputAvailability.AVAILABLE
    with pytest.raises(ValidationError, match="exactly when"):
        IssuerPriceToSalesRequest.model_validate(payload)


def test_kernel_rejects_unresolved_history_instead_of_silently_selecting_a_vintage() -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    prior_price = copy.deepcopy(payload["prices"][0])
    prior_price["input_id"] = "input:price:alphabet-a:2026-03-30"
    prior_price["session_close_at"] = "2026-03-30T20:00:00Z"
    prior_price["knowable_at"] = "2026-03-30T20:00:00Z"
    payload["prices"].append(prior_price)

    result = compute_issuer_price_to_sales(binding, IssuerPriceToSalesRequest.model_validate(payload))

    assert result.reason_codes == (ReasonCode.DUPLICATE_COMPONENT_PRICE,)


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    (
        (
            lambda payload: payload["shares"][0].update(security_id="security.unbound"),
            ReasonCode.UNEXPECTED_SHARES_SECURITY,
        ),
        (
            lambda payload: payload["revenues"][0].update(issuer_id="issuer.unbound"),
            ReasonCode.UNEXPECTED_REVENUE_ISSUER,
        ),
        (
            lambda payload: payload["revenues"][0].update(currency="EUR"),
            ReasonCode.REVENUE_CURRENCY_MISMATCH,
        ),
        (
            lambda payload: payload["revenues"][-1].update(period_end="2026-04-02"),
            ReasonCode.FUTURE_REVENUE_PERIOD,
        ),
    ),
)
def test_wrong_subject_currency_and_future_period_fail_closed(mutate, expected_reason: ReasonCode) -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    mutate(payload)

    result = compute_issuer_price_to_sales(binding, IssuerPriceToSalesRequest.model_validate(payload))

    assert result.reason_codes == (expected_reason,)


def test_duplicate_shares_fail_closed() -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    duplicate = copy.deepcopy(payload["shares"][0])
    duplicate["input_id"] = "input:shares:alphabet-a:duplicate"
    payload["shares"].append(duplicate)

    result = compute_issuer_price_to_sales(binding, IssuerPriceToSalesRequest.model_validate(payload))

    assert result.reason_codes == (ReasonCode.DUPLICATE_COMPONENT_SHARES,)


def test_equivalent_timezone_representations_have_identical_results() -> None:
    binding = _binding()
    utc_payload = _request_payload(binding, "dual-class-four-quarter-fx")
    offset_payload = copy.deepcopy(utc_payload)
    offset_payload["cutoff"] = "2026-04-02T04:00:00+08:00"
    for field in ("prices", "fx_rates"):
        for item in offset_payload[field]:
            item["session_close_at"] = "2026-04-01T04:00:00+08:00"

    utc_result = compute_issuer_price_to_sales(binding, IssuerPriceToSalesRequest.model_validate(utc_payload))
    offset_result = compute_issuer_price_to_sales(binding, IssuerPriceToSalesRequest.model_validate(offset_payload))

    assert offset_result == utc_result

    utc_payload["prices"] = utc_payload["prices"][:1]
    offset_payload["prices"] = offset_payload["prices"][:1]
    utc_unavailable = compute_issuer_price_to_sales(binding, IssuerPriceToSalesRequest.model_validate(utc_payload))
    offset_unavailable = compute_issuer_price_to_sales(
        binding, IssuerPriceToSalesRequest.model_validate(offset_payload)
    )
    assert offset_unavailable == utc_unavailable


def test_provenance_fields_and_default_registry_activation_are_forbidden() -> None:
    binding = _binding()
    payload = _request_payload(binding, "dual-class-four-quarter-fx")
    payload["prices"][0]["source"] = "vendor"
    payload["prices"][0]["raw_ref"] = "s3://raw/object"
    with pytest.raises(ValidationError):
        IssuerPriceToSalesRequest.model_validate(payload)

    # "price_to_sales" is intentionally registered as a real base factor
    # (factors.base.price_to_sales, #24/#25 preview) once imported elsewhere;
    # only the tiny batch's own private key must never appear here.
    assert "issuer_price_to_sales_tiny" not in FACTOR_REGISTRY
