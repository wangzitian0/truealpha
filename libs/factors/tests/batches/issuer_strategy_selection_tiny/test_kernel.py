from __future__ import annotations

import copy
import hashlib
import json
from decimal import ROUND_DOWN, ROUND_UP, Context, Decimal, getcontext, localcontext, setcontext
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path

import pytest
from factors.batches.issuer_strategy_selection_tiny import (
    CandidateSelectionReason,
    IssuerStrategySelectionTinyRequest,
    QlibSelectionExecutionBinding,
    SelectionAvailability,
    SelectionFailureReason,
    current_qlib_execution_binding,
    kernel,
    run_qlib_large_model_value_selection,
)
from factors.batches.issuer_tier_valuation_tiny import (
    IssuerTierValuationTinyResult,
)
from pydantic import ValidationError
from truealpha_contracts.research import (
    IssuerPriceToSalesPolicy,
    LargeModelValueV0Binding,
    LargeModelValueV0Policy,
    StrategyCandidateUniverse,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS_PATH = Path(__file__).with_name("fixtures") / "corpus.v1.json"
requires_qlib = pytest.mark.skipif(find_spec("qlib") is None, reason="requires the isolated S7 Qlib runtime")


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _corpus() -> dict[str, object]:
    return _load_json(CORPUS_PATH)


def _binding() -> LargeModelValueV0Binding:
    corpus = _corpus()
    refs = corpus["contract_refs"]
    frame = corpus["selection_frame"]
    assert isinstance(refs, dict) and isinstance(frame, dict)

    strategy_ref = refs["strategy_corpus"]
    binding_ref = refs["binding_corpus"]
    candidates = frame["candidates"]
    assert isinstance(strategy_ref, dict) and isinstance(binding_ref, dict) and isinstance(candidates, list)

    strategy_corpus = _load_json(ROOT / str(strategy_ref["path"]))
    definitions = strategy_corpus["valid_definitions"]
    assert isinstance(definitions, dict)
    strategy_payload = copy.deepcopy(definitions[str(strategy_ref["definition_key"])])
    assert isinstance(strategy_payload, dict)

    binding_corpus = _load_json(ROOT / str(binding_ref["path"]))
    base_binding = binding_corpus[str(binding_ref["binding_key"])]
    assert isinstance(base_binding, dict)
    candidate_universe_payload = copy.deepcopy(base_binding["candidate_universe"])
    assert isinstance(candidate_universe_payload, dict)
    candidate_universe_payload["candidates"] = [
        {
            "issuer": {"kind": "issuer", "id": item["issuer_id"]},
            "execution_listing": {"kind": "listing", "id": item["execution_listing_id"]},
            "market_value_components": [
                {
                    "security": {"kind": "security", "id": item["security_id"]},
                    "price_listing": {"kind": "listing", "id": item["execution_listing_id"]},
                }
            ],
            "complete_share_class_coverage": True,
        }
        for item in candidates
        if isinstance(item, dict)
    ]

    return LargeModelValueV0Binding(
        strategy=LargeModelValueV0Policy.model_validate_json(json.dumps(strategy_payload)),
        candidate_universe=StrategyCandidateUniverse.model_validate_json(json.dumps(candidate_universe_payload)),
        price_to_sales_policy=IssuerPriceToSalesPolicy.model_validate_json(
            json.dumps(base_binding["price_to_sales_policy"])
        ),
    )


def _valuation_results(binding: LargeModelValueV0Binding) -> tuple[IssuerTierValuationTinyResult, ...]:
    corpus = _corpus()
    frame = corpus["selection_frame"]
    positive = corpus["positive_case"]
    assert isinstance(frame, dict) and isinstance(positive, dict)
    candidates = frame["candidates"]
    rows = positive["valuation_results"]
    assert isinstance(candidates, list) and isinstance(rows, list)
    issuer_by_public_id = {
        str(item["public_candidate_id"]): str(item["issuer_id"]) for item in candidates if isinstance(item, dict)
    }
    return tuple(
        IssuerTierValuationTinyResult.model_validate(
            {
                "strategy_binding_id": binding.strategy_binding_id,
                "candidate_universe_id": binding.candidate_universe.candidate_universe_id,
                "price_to_sales_policy_id": binding.price_to_sales_policy.price_to_sales_policy_id,
                "issuer_id": issuer_by_public_id[str(row["public_candidate_id"])],
                "as_of": frame["cutoff"],
                "reporting_currency": frame["reporting_currency"],
                **{key: value for key, value in row.items() if key != "public_candidate_id"},
            }
        )
        for row in rows
        if isinstance(row, dict)
    )


def _request_payload(binding: LargeModelValueV0Binding) -> dict[str, object]:
    frame = _corpus()["selection_frame"]
    assert isinstance(frame, dict)
    return {
        "activation": {"environment": "ci"},
        "execution": current_qlib_execution_binding().model_dump(mode="json"),
        "strategy_binding_id": binding.strategy_binding_id,
        "candidate_universe_id": binding.candidate_universe.candidate_universe_id,
        "price_to_sales_policy_id": binding.price_to_sales_policy.price_to_sales_policy_id,
        "cutoff": frame["cutoff"],
        "reporting_currency": frame["reporting_currency"],
        "valuation_results": [item.model_dump(mode="json") for item in _valuation_results(binding)],
    }


def _request(binding: LargeModelValueV0Binding) -> IssuerStrategySelectionTinyRequest:
    return IssuerStrategySelectionTinyRequest.model_validate(_request_payload(binding))


def _replace_result(
    result: IssuerTierValuationTinyResult,
    **updates: object,
) -> IssuerTierValuationTinyResult:
    payload = result.model_dump(mode="json", exclude={"issuer_tier_valuation_id", "content_sha256"})
    payload.update(updates)
    return IssuerTierValuationTinyResult.model_validate(payload)


def _decimal_oracle(
    binding: LargeModelValueV0Binding,
    results: tuple[IssuerTierValuationTinyResult, ...],
) -> tuple[str, ...]:
    minimum = binding.strategy.eligibility.minimum_confidence
    eligible = [
        item
        for item in results
        if item.availability.value == "available" and item.confidence >= minimum and item.valuation_gap is not None
    ]
    ordered = sorted(eligible, key=lambda item: item.valuation_gap or Decimal("0"), reverse=True)
    return tuple(item.issuer_id for item in ordered[: binding.strategy.selection_count])


def test_frozen_corpus_and_isolated_lock_are_exact() -> None:
    assert hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest() == kernel.FROZEN_CORPUS_SHA256
    assert (
        hashlib.sha256((ROOT / "libs/factors/qlib-runtime/uv.lock").read_bytes()).hexdigest() == kernel.QLIB_LOCK_SHA256
    )
    engine = _corpus()["engine_binding"]
    assert isinstance(engine, dict)
    assert engine["version"] == kernel.QLIB_VERSION
    assert engine["release_commit"] == kernel.QLIB_RELEASE_COMMIT
    assert engine["adapter_id"] == kernel.QLIB_ADAPTER_ID
    assert engine["operator_registry_id"] == kernel.QLIB_OPERATOR_REGISTRY_ID
    assert engine["strategy_id"] == kernel.QLIB_STRATEGY_ID
    assert engine["builtin_topk_dropout_is_equivalent"] is False


@requires_qlib
def test_installed_qlib_runtime_is_exact() -> None:
    assert version("pyqlib") == "0.9.7"


@requires_qlib
def test_actual_qlib_adapter_matches_independent_decimal_oracle() -> None:
    binding = _binding()
    request = _request(binding)
    result = run_qlib_large_model_value_selection(binding, request)
    expected = _corpus()["positive_case"]
    assert isinstance(expected, dict) and isinstance(expected["expected"], dict)
    public_expected = expected["expected"]

    assert result.availability is SelectionAvailability.AVAILABLE
    assert result.selected_issuer_ids == _decimal_oracle(binding, request.valuation_results)
    assert [item.rsplit(".", 1)[-1] for item in result.selected_issuer_ids] == public_expected[
        "selected_public_candidate_ids"
    ]
    assert result.confidence == Decimal(str(public_expected["confidence"]))
    assert result.selection_id == f"issuer-strategy-selection:{result.semantic_sha256}"
    assert result.evidence_id == f"issuer-strategy-selection-evidence:{result.content_sha256}"
    assert result.qlib_execution_binding_id == request.execution.qlib_execution_binding_id


@requires_qlib
def test_input_order_and_ambient_decimal_context_do_not_change_outputs() -> None:
    binding = _binding()
    baseline = _request_payload(binding)
    reversed_payload = copy.deepcopy(baseline)
    values = reversed_payload["valuation_results"]
    assert isinstance(values, list)
    values.reverse()

    original = getcontext().copy()
    try:
        outputs = []
        for context in (Context(prec=4, rounding=ROUND_DOWN), Context(prec=64, rounding=ROUND_UP)):
            setcontext(context)
            request = IssuerStrategySelectionTinyRequest.model_validate(copy.deepcopy(reversed_payload))
            outputs.append(run_qlib_large_model_value_selection(binding, request))
    finally:
        setcontext(original)

    assert outputs[0] == outputs[1]
    assert outputs[0].selected_issuer_ids == _decimal_oracle(binding, _request(binding).valuation_results)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("binding", SelectionFailureReason.BINDING_IDENTITY_MISMATCH),
        ("universe", SelectionFailureReason.CANDIDATE_UNIVERSE_IDENTITY_MISMATCH),
        ("policy", SelectionFailureReason.PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH),
        ("cutoff", SelectionFailureReason.CUTOFF_IDENTITY_MISMATCH),
        ("currency", SelectionFailureReason.REPORTING_CURRENCY_MISMATCH),
        ("duplicate", SelectionFailureReason.DUPLICATE_ISSUER_RESULT),
        ("extra", SelectionFailureReason.EXTRA_ISSUER_RESULT),
    ],
)
def test_identity_and_denominator_corruption_fail_closed(
    mutation: str,
    reason: SelectionFailureReason,
) -> None:
    binding = _binding()
    payload = _request_payload(binding)
    rows = payload["valuation_results"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    if mutation == "binding":
        rows[0]["strategy_binding_id"] = "strategy-binding:mismatch"
    elif mutation == "universe":
        rows[0]["candidate_universe_id"] = "strategy-candidate-universe:mismatch"
    elif mutation == "policy":
        rows[0]["price_to_sales_policy_id"] = "price-to-sales-policy:mismatch"
    elif mutation == "cutoff":
        rows[0]["as_of"] = "2026-03-31T19:59:59.999999Z"
    elif mutation == "currency":
        rows[0]["reporting_currency"] = "EUR"
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "extra":
        extra = copy.deepcopy(rows[0])
        assert isinstance(extra, dict)
        extra["issuer_id"] = "issuer.synthetic.extra"
        extra.pop("issuer_tier_valuation_id", None)
        extra.pop("content_sha256", None)
        rows.append(extra)
    if mutation in {"binding", "universe", "policy", "cutoff", "currency"}:
        rows[0].pop("issuer_tier_valuation_id", None)
        rows[0].pop("content_sha256", None)
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(payload),
    )
    assert result.availability is SelectionAvailability.UNAVAILABLE
    assert result.reason_codes == (reason,)


@requires_qlib
def test_missing_and_low_confidence_candidates_remain_explicit() -> None:
    binding = _binding()
    payload = _request_payload(binding)
    rows = payload["valuation_results"]
    assert isinstance(rows, list)
    rows.pop(10)
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(payload),
    )
    assert result.availability is SelectionAvailability.AVAILABLE
    missing = next(item for item in result.decisions if item.issuer_id.endswith("candidate-11"))
    assert missing.reason is CandidateSelectionReason.MISSING_REQUIRED_RESULT

    payload = _request_payload(binding)
    rows = payload["valuation_results"]
    assert isinstance(rows, list) and isinstance(rows[10], dict)
    rows[10]["confidence"] = "0.79"
    rows[10].pop("issuer_tier_valuation_id", None)
    rows[10].pop("content_sha256", None)
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(payload),
    )
    low = next(item for item in result.decisions if item.issuer_id.endswith("candidate-11"))
    assert low.reason is CandidateSelectionReason.LOW_CONFIDENCE


def test_insufficient_candidates_and_equal_decimal_scores_fail_closed() -> None:
    binding = _binding()
    payload = _request_payload(binding)
    rows = payload["valuation_results"]
    assert isinstance(rows, list)
    del rows[9:11]
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(payload),
    )
    assert result.reason_codes == (SelectionFailureReason.INSUFFICIENT_ELIGIBLE_CANDIDATES,)

    results = list(_valuation_results(binding))
    candidate_one = results[0]
    candidate_two = results[1]
    results[1] = _replace_result(
        candidate_two,
        tier=candidate_one.tier,
        target_ps_lower=candidate_one.target_ps_lower,
        target_ps_upper=candidate_one.target_ps_upper,
        target_ps_midpoint=candidate_one.target_ps_midpoint,
        current_price_to_sales=candidate_one.current_price_to_sales,
        valuation_gap=candidate_one.valuation_gap,
    )
    request_payload = _request_payload(binding)
    request_payload["valuation_results"] = [item.model_dump(mode="json") for item in results]
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(request_payload),
    )
    assert result.reason_codes == (SelectionFailureReason.RANKING_TIE_BREAK_UNAPPROVED,)


def test_decimal_to_float_collision_fails_before_qlib() -> None:
    binding = _binding()
    results = list(_valuation_results(binding))
    with localcontext(Context(prec=28)):
        first_ps = Decimal("12.5")
        second_ps = Decimal("12.49999999999999999999999999")
        first_gap = Decimal("25") / first_ps - Decimal("1")
        second_gap = Decimal("25") / second_ps - Decimal("1")
    assert first_gap != second_gap and float(first_gap) == float(second_gap)
    results[0] = _replace_result(
        results[0],
        current_price_to_sales=first_ps,
        valuation_gap=first_gap,
    )
    results[1] = _replace_result(
        results[1],
        current_price_to_sales=second_ps,
        valuation_gap=second_gap,
    )
    payload = _request_payload(binding)
    payload["valuation_results"] = [item.model_dump(mode="json") for item in results]
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(payload),
    )
    assert result.reason_codes == (SelectionFailureReason.QLIB_SCORE_ORDER_NOT_PRESERVED,)


def test_execution_binding_drift_and_malformed_qlib_output_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _binding()
    payload = _request_payload(binding)
    execution = payload["execution"]
    assert isinstance(execution, dict)
    execution.pop("qlib_execution_binding_id", None)
    execution.pop("content_sha256", None)
    execution["version"] = "0.9.6"
    result = run_qlib_large_model_value_selection(
        binding,
        IssuerStrategySelectionTinyRequest.model_validate(payload),
    )
    assert result.reason_codes == (SelectionFailureReason.QLIB_EXECUTION_BINDING_MISMATCH,)

    monkeypatch.setattr(
        kernel, "_run_qlib_top_n", lambda *_args: tuple(reversed(_decimal_oracle(binding, _valuation_results(binding))))
    )
    result = run_qlib_large_model_value_selection(binding, _request(binding))
    assert result.reason_codes == (SelectionFailureReason.QLIB_STRATEGY_OUTPUT_INVALID,)


def test_qlib_runtime_failure_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _binding()

    def fail_runtime(*_args: object) -> tuple[str, ...]:
        raise RuntimeError("isolated Qlib runtime is unavailable")

    monkeypatch.setattr(kernel, "_run_qlib_top_n", fail_runtime)
    result = run_qlib_large_model_value_selection(binding, _request(binding))
    assert result.availability is SelectionAvailability.UNAVAILABLE
    assert result.reason_codes == (SelectionFailureReason.QLIB_RUNTIME_FAILURE,)


def test_schema_rejects_provenance_supplied_values_and_release_activation() -> None:
    binding = _binding()
    payload = _request_payload(binding)
    payload["source_id"] = "vendor"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IssuerStrategySelectionTinyRequest.model_validate(payload)

    payload = _request_payload(binding)
    payload["target_ps"] = "25"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IssuerStrategySelectionTinyRequest.model_validate(payload)

    payload = _request_payload(binding)
    activation = payload["activation"]
    assert isinstance(activation, dict)
    activation["release_allowed"] = True
    with pytest.raises(ValidationError):
        IssuerStrategySelectionTinyRequest.model_validate(payload)


def test_execution_binding_identity_is_content_addressed() -> None:
    current = current_qlib_execution_binding()
    assert current.qlib_execution_binding_id == f"qlib-execution-binding:{current.content_sha256}"
    with pytest.raises(ValidationError, match="content hash mismatch"):
        QlibSelectionExecutionBinding.model_validate({**current.model_dump(mode="json"), "content_sha256": "f" * 64})
