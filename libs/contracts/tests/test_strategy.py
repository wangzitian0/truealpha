import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.research import TargetPsSelection
from truealpha_contracts.strategy import (
    ConfidenceEligibilityRule,
    DecimalQuantization,
    ExclusionReason,
    FactorDimension,
    GoldenDecisionOutcome,
    GoldenDecisionSet,
    GoldenInputRecord,
    LargeModelValueV0Definition,
    StrategyEngineBinding,
)

CORPUS_PATH = Path(__file__).with_name("fixtures") / "large_model_value_v0_strategy.v1.json"
CORPUS_SHA256 = "0d110a3adc94500cba2bc35d5cd33a788a18bc76ef66895c5625489be6ea50e6"

_REQUIRED_INPUT_REASONS = (
    ("gross_profit", ExclusionReason.MISSING_GROSS_PROFIT_FACT),
    ("total_assets", ExclusionReason.MISSING_TOTAL_ASSETS_FACT),
    ("headcount", ExclusionReason.MISSING_HEADCOUNT_DISCLOSURE),
    ("revenue", ExclusionReason.MISSING_REVENUE_FACT),
    ("shares_outstanding", ExclusionReason.MISSING_MARKET_VALUE_INPUT),
    ("last_close", ExclusionReason.MISSING_MARKET_VALUE_INPUT),
)


def _corpus() -> dict[str, object]:
    raw = CORPUS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CORPUS_SHA256
    return json.loads(raw)


def _expected() -> dict[str, str]:
    expected = _corpus()["expected"]
    assert isinstance(expected, dict)
    return expected


def _definition_payload() -> dict[str, object]:
    return copy.deepcopy(_corpus()["strategy_definition"])


def _validate_definition(payload: dict[str, object]) -> LargeModelValueV0Definition:
    return LargeModelValueV0Definition.model_validate_json(json.dumps(payload))


def _definition() -> LargeModelValueV0Definition:
    return _validate_definition(_definition_payload())


def _golden_payload() -> dict[str, object]:
    return copy.deepcopy(_corpus()["golden_decision_set"])


def _golden() -> GoldenDecisionSet:
    return GoldenDecisionSet.model_validate_json(json.dumps(_golden_payload()))


def _binding_payload(key: str = "engine_binding") -> dict[str, object]:
    return copy.deepcopy(_corpus()[key])


def _binding(key: str = "engine_binding") -> StrategyEngineBinding:
    return StrategyEngineBinding.model_validate_json(json.dumps(_binding_payload(key)))


def _apply_corpus_mutation(payload: dict[str, object], case: dict[str, object]) -> None:
    parts = str(case["path"]).split(".")
    target: object = payload
    for part in parts[:-1]:
        if isinstance(target, list):
            target = target[int(part)]
        elif isinstance(target, dict):
            target = target[part]
        else:  # pragma: no cover - the frozen corpus contains only object/list paths
            raise AssertionError(f"mutation path does not resolve to a container: {case['path']}")
    key = parts[-1]
    operation = case["operation"]
    if isinstance(target, list):
        index = int(key)
        if operation == "remove":
            target.pop(index)
        else:
            target[index] = case["value"]
    elif isinstance(target, dict):
        if operation == "remove":
            target.pop(key)
        else:
            target[key] = case["value"]
    else:  # pragma: no cover - the frozen corpus contains only object/list paths
        raise AssertionError(f"mutation path does not resolve to a container: {case['path']}")


def _quantize(value: Decimal, quantization: DecimalQuantization) -> Decimal:
    assert quantization.rounding == "half_even"
    return value.quantize(Decimal(1).scaleb(-quantization.decimal_places), ROUND_HALF_EVEN)


def test_frozen_corpus_pins_one_canonical_strategy_identity() -> None:
    expected = _expected()
    first = _definition()
    second = _definition()
    assert first.strategy_definition_id == expected["strategy_definition_id"]
    assert first.content_sha256 == expected["strategy_definition_sha256"]
    assert first.strategy_definition_id == f"strategy-definition:{first.content_sha256}"
    assert second.strategy_definition_id == first.strategy_definition_id

    restored = LargeModelValueV0Definition.model_validate_json(first.model_dump_json())
    assert restored.strategy_definition_id == first.strategy_definition_id
    assert restored.content_sha256 == first.content_sha256

    golden = _golden()
    assert golden.golden_decision_set_id == expected["golden_decision_set_id"]
    assert golden.strategy_definition_id == first.strategy_definition_id
    assert golden.strategy_definition_sha256 == first.content_sha256

    for factor in (first.labor_efficiency, first.price_to_sales, first.tier_valuation):
        assert factor.factor_definition_id == f"factor-definition:{factor.content_sha256}"
        assert factor.factor_version == "0.1.0"


def test_semantic_content_hash_tracks_every_parameter() -> None:
    pinned = _expected()["strategy_definition_id"]
    mutations = (
        ("eligibility.minimum_confidence", "0.75"),
        ("selection.selection_count", 3),
        ("tier_valuation.bands.1.target_ps_upper_bound", "7.00"),
        ("labor_efficiency.factor_version", "0.2.0"),
        ("transaction_cost.basis_points", "10"),
    )
    seen = {pinned}
    for path, value in mutations:
        payload = _definition_payload()
        _apply_corpus_mutation(payload, {"path": path, "operation": "replace", "value": value})
        changed = _validate_definition(payload)
        assert changed.strategy_definition_id != pinned
        assert changed.strategy_definition_id not in seen
        seen.add(changed.strategy_definition_id)


def test_formula_variants_are_configuration_not_structure() -> None:
    payload = _definition_payload()
    labor = payload["labor_efficiency"]
    assert isinstance(labor, dict)
    labor["factor_version"] = "0.2.0"
    labor["denominator"] = "labor_cost_salaries_plus_esop"
    labor["unit"] = {"dimension": "dimensionless", "unit_code": "ratio"}
    labor["missing_denominator_reason"] = "missing_labor_cost_disclosure"
    labor["nonpositive_denominator_reason"] = "nonpositive_labor_cost"
    labor_cost_variant = _validate_definition(payload)
    assert labor_cost_variant.strategy_definition_id != _expected()["strategy_definition_id"]
    assert labor_cost_variant.labor_efficiency.unit.dimension is FactorDimension.DIMENSIONLESS

    payload = _definition_payload()
    labor = payload["labor_efficiency"]
    assert isinstance(labor, dict)
    labor["factor_version"] = "0.3.0"
    labor["capital_charge_base"] = "average_investable_financial_assets"
    labor["missing_capital_base_reason"] = "missing_financial_asset_base_fact"
    financial_base_variant = _validate_definition(payload)
    assert financial_base_variant.strategy_definition_id != labor_cost_variant.strategy_definition_id


def test_engine_identity_stays_outside_the_semantic_hash() -> None:
    expected = _expected()
    binding = _binding()
    alternate = _binding("engine_binding_alternate")
    assert binding.engine_binding_id == expected["engine_binding_id"]
    assert alternate.engine_binding_id == expected["engine_binding_alternate_id"]
    assert binding.engine_binding_id != alternate.engine_binding_id
    assert binding.execution_binding.version != alternate.execution_binding.version
    assert binding.strategy_definition_id == expected["strategy_definition_id"]
    assert alternate.strategy_definition_id == expected["strategy_definition_id"]
    assert binding.operator_registry_id == alternate.operator_registry_id

    definition_text = json.dumps(_corpus()["strategy_definition"])
    assert "pyqlib" not in definition_text
    assert "operator_registry" not in definition_text
    assert "adapter" not in definition_text


def test_corpus_negative_cases_fail_closed() -> None:
    corpus = _corpus()
    negative_cases = corpus["negative_cases"]
    assert isinstance(negative_cases, list)
    assert len(negative_cases) == 25
    payloads = {
        "strategy_definition": _definition_payload,
        "golden_decision_set": _golden_payload,
        "engine_binding": _binding_payload,
    }
    validators = {
        "strategy_definition": LargeModelValueV0Definition,
        "golden_decision_set": GoldenDecisionSet,
        "engine_binding": StrategyEngineBinding,
    }
    for case in negative_cases:
        assert isinstance(case, dict)
        target = str(case["target"])
        candidate = payloads[target]()
        _apply_corpus_mutation(candidate, case)
        with pytest.raises(ValidationError):
            validators[target].model_validate_json(json.dumps(candidate))


def test_python_mode_rejects_binary_float_parameters() -> None:
    with pytest.raises(ValidationError):
        ConfidenceEligibilityRule(
            rule_id="minimum_consumed_confidence_floor",
            minimum_confidence=0.7,  # type: ignore[arg-type]
            confidence_semantics="minimum_consumed_input_confidence",
            maximum_input_age_days=455,
            below_floor_reason=ExclusionReason.BELOW_CONFIDENCE_FLOOR,
            stale_input_reason=ExclusionReason.STALE_REQUIRED_INPUT,
        )
    with pytest.raises(ValidationError):
        GoldenInputRecord(
            input_key="gross_profit",
            value=1956145000.0,  # type: ignore[arg-type]
            unit_code="usd",
            confidence=Decimal("0.90"),
            knowable_at=datetime(2026, 2, 26, tzinfo=UTC),
            grounding="apps/data-engine/samples/sec/NICE_CIK0001003935.json",
        )


def test_golden_corpus_covers_the_required_scenarios_with_grounded_inputs() -> None:
    definition = _definition()
    golden = _golden()
    assert len(golden.decisions) == 10
    issuers = {decision.issuer.id for decision in golden.decisions}
    assert issuers == {"issuer:nice", "issuer:adm", "issuer:shop", "issuer:ddog", "issuer:jpm"}
    cutoffs = {decision.cutoff_at for decision in golden.decisions}
    assert cutoffs == {
        datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
    }

    outcomes = [decision.expected.outcome for decision in golden.decisions]
    assert outcomes.count(GoldenDecisionOutcome.SELECTED) == 4
    assert outcomes.count(GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND) == 2
    assert outcomes.count(GoldenDecisionOutcome.EXCLUDED) == 4

    by_issuer = {
        issuer: {decision.expected.exclusion_reason for decision in golden.decisions if decision.issuer.id == issuer}
        for issuer in issuers
    }
    assert by_issuer["issuer:ddog"] == {ExclusionReason.BELOW_CONFIDENCE_FLOOR}
    assert by_issuer["issuer:jpm"] == {ExclusionReason.MISSING_GROSS_PROFIT_FACT}

    for decision in golden.decisions:
        for record in decision.inputs:
            assert record.grounding.startswith("apps/data-engine/samples/")
            assert record.knowable_at <= decision.cutoff_at

    assert definition.labor_efficiency.unit.dimension is FactorDimension.REPORTING_CURRENCY_PER_EMPLOYEE
    assert definition.price_to_sales.unit.dimension is FactorDimension.DIMENSIONLESS
    assert definition.tier_valuation.unit.dimension is FactorDimension.DIMENSIONLESS


def test_golden_expectations_recompute_exactly_from_their_inputs() -> None:
    definition = _definition()
    golden = _golden()
    assert definition.tier_valuation.target_ps_selection is TargetPsSelection.BAND_MIDPOINT
    verified = 0
    for rate in golden.risk_free_rates:
        at_cutoff = [decision for decision in golden.decisions if decision.cutoff_at == rate.cutoff_at]
        assert len(at_cutoff) == 5
        gaps: dict[str, Decimal] = {}
        decisions_by_issuer = {decision.issuer.id: decision for decision in at_cutoff}
        for decision in at_cutoff:
            inputs = {record.input_key: record for record in decision.inputs}
            expected = decision.expected
            missing = next((reason for key, reason in _REQUIRED_INPUT_REASONS if key not in inputs), None)
            if missing is not None:
                assert expected.outcome is GoldenDecisionOutcome.EXCLUDED
                assert expected.exclusion_reason is missing
                assert not expected.eligible
                verified += 1
                continue
            consumed_confidence = min(record.confidence for record in inputs.values())
            if consumed_confidence < definition.eligibility.minimum_confidence:
                assert expected.outcome is GoldenDecisionOutcome.EXCLUDED
                assert expected.exclusion_reason is ExclusionReason.BELOW_CONFIDENCE_FLOOR
                assert not expected.eligible
                verified += 1
                continue

            capital_charge = inputs["total_assets"].value * rate.annualized_rate
            labor_efficiency = _quantize(
                (inputs["gross_profit"].value - capital_charge) / inputs["headcount"].value,
                definition.labor_efficiency.quantization,
            )
            band = definition.tier_valuation.band_for(labor_efficiency)
            current_ps = _quantize(
                inputs["shares_outstanding"].value * inputs["last_close"].value / inputs["revenue"].value,
                definition.price_to_sales.quantization,
            )
            target_ps = _quantize(
                (band.target_ps_lower_bound + band.target_ps_upper_bound) / 2,
                definition.tier_valuation.quantization,
            )
            valuation_gap = _quantize(target_ps / current_ps - 1, definition.valuation_gap.quantization)

            assert expected.capital_adjusted_labor_efficiency == labor_efficiency
            assert str(expected.capital_adjusted_labor_efficiency) == str(labor_efficiency)
            assert expected.tier is band.tier
            assert expected.current_price_to_sales == current_ps
            assert str(expected.current_price_to_sales) == str(current_ps)
            assert expected.target_price_to_sales == target_ps
            assert str(expected.target_price_to_sales) == str(target_ps)
            assert expected.valuation_gap == valuation_gap
            assert str(expected.valuation_gap) == str(valuation_gap)
            assert expected.eligible

            if current_ps > band.target_ps_upper_bound:
                assert expected.outcome is GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND
                verified += 1
                continue
            gaps[decision.issuer.id] = valuation_gap

        ranked = sorted(gaps, key=lambda issuer_id: (-gaps[issuer_id], issuer_id))
        selected_count = min(len(ranked), definition.selection.selection_count)
        assert selected_count > 0
        target_weight = _quantize(Decimal(1) / Decimal(selected_count), definition.sizing.quantization)
        for position, issuer_id in enumerate(ranked, start=1):
            expected = decisions_by_issuer[issuer_id].expected
            assert expected.rank == position
            if position <= selected_count:
                assert expected.outcome is GoldenDecisionOutcome.SELECTED
                assert expected.target_weight == target_weight
                assert str(expected.target_weight) == str(target_weight)
            else:  # pragma: no cover - the frozen corpus selects every ranked issuer
                assert expected.outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT
            verified += 1
    assert verified == 10
