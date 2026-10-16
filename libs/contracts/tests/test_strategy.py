import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources

import pytest
from pydantic import ValidationError
from truealpha_contracts.strategy import (
    ConfidenceEligibilityRule,
    ExclusionReason,
    FactorDimension,
    GoldenDecisionOutcome,
    GoldenDecisionSet,
    GoldenInputRecord,
    LargeModelValueV0Definition,
    RiskFreeInstrument,
)

# The end-to-end recompute of these golden decisions now lives in
# factors.tests.test_strategy_evaluator, which runs the single-source
# `strategy_evaluator` both this golden and the #26 replay consume (#393). This
# module keeps the schema-, identity-, and negative-case contract tests.

CORPUS_RESOURCE = resources.files("truealpha_contracts.data").joinpath("large_model_value_v0_definition_1_0_0.json")
CORPUS_SHA256 = "004e716a11f1a9604275d2da5b13160676411fbb8fc7846b25489b43aef31496"


def _corpus() -> dict[str, object]:
    raw = CORPUS_RESOURCE.read_bytes()
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


def test_semantic_content_hash_tracks_every_parameter() -> None:
    pinned = _expected()["strategy_definition_id"]
    mutations = (
        ("eligibility.minimum_confidence", "0.75"),
        ("selection.selection_count", 3),
        ("tier_valuation.bands.1.target_pgp_upper_bound", "15.00"),
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

    # The labor-cost denominator is a legitimate future versioned variant of the
    # one uniform schema. The former financial-asset-base variant was removed with
    # the financial branch (2026-07-18 owner decision): there is no separate
    # financial capital-charge base -- every issuer uses total_assets.


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
    assert outcomes.count(GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT) == 1
    assert outcomes.count(GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND) == 3
    assert outcomes.count(GoldenDecisionOutcome.EXCLUDED) == 2

    by_issuer = {
        issuer: {decision.expected.exclusion_reason for decision in golden.decisions if decision.issuer.id == issuer}
        for issuer in issuers
    }
    assert by_issuer["issuer:ddog"] == {ExclusionReason.BELOW_CONFIDENCE_FLOOR}
    assert by_issuer["issuer:jpm"] == {None}

    for decision in golden.decisions:
        for record in decision.inputs:
            assert record.grounding.startswith("apps/data-engine/samples/")
            assert record.knowable_at <= decision.cutoff_at

    assert definition.labor_efficiency.unit.dimension is FactorDimension.REPORTING_CURRENCY_PER_EMPLOYEE
    assert definition.price_to_sales.unit.dimension is FactorDimension.DIMENSIONLESS
    assert definition.tier_valuation.unit.dimension is FactorDimension.DIMENSIONLESS
    assert definition.price_to_gross_profit is not None
    assert definition.price_to_gross_profit.unit.dimension is FactorDimension.DIMENSIONLESS


def test_the_definition_names_the_measured_series_and_keeps_explicit_golden_rates() -> None:
    """#1255: definition 1.0.0 declares the rate as a per-cutoff parameter of the 3-month Treasury bill.

    The data engine fills that parameter from the stored series (`datahub.risk_free`). The golden set
    keeps its two explicit rates, so that the golden replay stays fixed. No function of the contract
    resolves a rate from them any more.
    """
    from truealpha_contracts import strategy

    risk_free = _definition().labor_efficiency.risk_free
    assert risk_free.rule_id == "per_cutoff_versioned_parameter"
    assert risk_free.instrument is RiskFreeInstrument.US_TREASURY_BILL_3_MONTH
    assert risk_free.instrument.value == "us_treasury_bill_3_month"
    assert risk_free.rate_unit == "annualized_decimal_fraction"
    assert risk_free.missing_rate_reason is ExclusionReason.MISSING_RISK_FREE_RATE_PARAMETER

    assert [(rate.cutoff_at, rate.annualized_rate) for rate in _golden().risk_free_rates] == [
        (datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC), Decimal("0.0380")),
        (datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC), Decimal("0.0365")),
    ]
    assert not hasattr(strategy, "live_risk_free_rate"), "the table lookup is deleted"
    assert "live_risk_free_rate" not in strategy.__all__
    assert not hasattr(GoldenDecisionSet, "rate_for"), "no cutoff before the first point falls back to the first rate"
