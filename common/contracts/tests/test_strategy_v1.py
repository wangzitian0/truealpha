"""Strategy definitions 1.0.0 and 1.1.0 of large_model_value_v0 (#1235, #1518): the confirmed P/GP bands.

Definition 1.0.0 is the frozen file with the golden decisions. It is read by name. Definition
1.1.0 is the live file: the same bands, with labor efficiency pinned as a tree per issuer class.

The golden expectations in the packaged file are checked here against an INDEPENDENT
oracle: a plain Decimal calculation written in this file. It does not import the
evaluator, the factors, or the band lookup of the definition. It uses the band values
that the owner confirmed on #1235, written out below, so a wrong value in the packaged
file and a wrong rule in the evaluator both fail a test.

The arithmetic of one golden decision (``nice-2026-03-31``, rate 0.0380):

    capital charge      = 5,106,030,000 x 0.0380                = 194,029,140
    labor efficiency    = (1,956,145,000 - 194,029,140) / 9,626 = 183,057.95   -> traditional (< 1,000,000)
    market value        = 60,427,562 x 110.26                   = 6,662,742,986.12
    current P/GP        = 6,662,742,986.12 / 1,956,145,000      = 3.4061 (4 dp, half-even)
    target P/GP         = (7.50 + 10.00) / 2                    = 8.7500
    valuation gap       = 8.7500 / 3.4061 - 1                   = 1.5689 (4 dp, half-even)
    3.4061 <= 10.00 upper bound -> candidate; the highest gap of the cutoff -> rank 1, weight 1/2

The evaluator half of the agreement (the evaluator gives these same values) is
``common/factors/tests/test_strategy_evaluator.py``.
"""

from __future__ import annotations

import copy
import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from truealpha_contracts.strategy import (
    LIVE_STRATEGY_DEFINITION_RESOURCE,
    GoldenDecisionSet,
    GoldenExpectation,
    LargeModelValueV0Definition,
    ValuationBasis,
    load_live_strategy_definition,
    load_strategy_definition,
    load_strategy_definition_corpus,
)

#: The frozen #21 file: definition 1.0.0 and the golden decisions. Named here on purpose.
_FROZEN_RESOURCE = "large_model_value_v0_definition_1_0_0.json"

#: Owner decision 2026-10-12 on #1235: (tier, labor-efficiency lower, upper, target P/GP lower, upper).
_OWNER_BANDS: tuple[tuple[str, Decimal | None, Decimal | None, Decimal, Decimal], ...] = (
    ("traditional", None, Decimal("1000000"), Decimal("7.50"), Decimal("10.00")),
    ("tech", Decimal("1000000"), Decimal("3000000"), Decimal("11.43"), Decimal("14.29")),
    ("large_model_native", Decimal("3000000"), None, Decimal("26.67"), Decimal("40.00")),
)
#: The derivation recorded on #1235: vision.md P/S anchors / reference gross margin.
_VISION_PS_ANCHORS = {
    "traditional": (Decimal("3"), Decimal("4"), Decimal("0.40")),
    "tech": (Decimal("8"), Decimal("10"), Decimal("0.70")),
    "large_model_native": (Decimal("20"), Decimal("30"), Decimal("0.75")),
}
#: Unchanged from definition 0.1.0, restated by the owner: floor 0.70, top 2, equal weight.
_CONFIDENCE_FLOOR = Decimal("0.70")
_SELECTION_COUNT = 2
_V1_SHA256 = "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803"


def _q(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN)


def _corpus() -> dict[str, Any]:
    return load_strategy_definition_corpus(_FROZEN_RESOURCE)


def _definition_payload() -> dict[str, Any]:
    return copy.deepcopy(_corpus()["strategy_definition"])


def _validate(payload: dict[str, Any]) -> LargeModelValueV0Definition:
    return LargeModelValueV0Definition.model_validate_json(json.dumps(payload))


def _oracle(decisions: list[dict[str, Any]], rate: Decimal) -> dict[str, dict[str, Any]]:
    """Expected decisions at one cutoff, from the owner's values and plain Decimal arithmetic."""

    expected: dict[str, dict[str, Any]] = {}
    candidates: list[tuple[Decimal, str]] = []
    for decision in decisions:
        issuer = decision["issuer"]["id"]
        values = {item["input_key"]: Decimal(item["value"]) for item in decision["inputs"]}
        confidence = min(Decimal(item["confidence"]) for item in decision["inputs"])
        if confidence < _CONFIDENCE_FLOOR:
            expected[issuer] = {"outcome": "excluded", "exclusion_reason": "below_confidence_floor", "tier": None}
            continue
        labor = _q((values["gross_profit"] - values["total_assets"] * rate) / values["headcount"], 2)
        tier, _lower, _upper, target_lower, target_upper = next(
            band
            for band in _OWNER_BANDS
            if (band[1] is None or labor >= band[1]) and (band[2] is None or labor < band[2])
        )
        market_value = values["shares_outstanding"] * values["last_close"]
        current_pgp = _q(market_value / values["gross_profit"], 4)
        target_pgp = _q((target_lower + target_upper) / 2, 4)
        gap = _q(target_pgp / current_pgp - 1, 4)
        rejected = current_pgp > target_upper
        expected[issuer] = {
            "capital_adjusted_labor_efficiency": labor,
            "tier": tier,
            "current_price_to_sales": _q(market_value / values["revenue"], 4),
            "target_price_to_sales": None,
            "current_price_to_gross_profit": current_pgp,
            "target_price_to_gross_profit": target_pgp,
            "valuation_gap": gap,
            "outcome": "rejected_valuation_above_tier_band" if rejected else None,
            "exclusion_reason": None,
            "rank": None,
            "target_weight": None,
        }
        if not rejected:
            candidates.append((gap, issuer))
    ranked = sorted(candidates, key=lambda item: (-item[0], item[1]))
    weight = _q(Decimal(1) / Decimal(max(min(len(ranked), _SELECTION_COUNT), 1)), 6)
    for position, (_gap, issuer) in enumerate(ranked, start=1):
        selected = position <= _SELECTION_COUNT
        expected[issuer]["rank"] = position
        expected[issuer]["outcome"] = "selected" if selected else "ranked_beyond_selection_count"
        expected[issuer]["target_weight"] = weight if selected else None
    return expected


def test_the_frozen_definition_is_version_1_0_0_and_is_not_provisional() -> None:
    definition = load_strategy_definition(_FROZEN_RESOURCE)
    assert definition.strategy_id == "large_model_value_v0"
    assert definition.definition_version == "1.0.0"
    assert definition.provisional_semantics is False
    assert definition.tier_valuation.provisional_thresholds is False
    assert definition.valuation_basis is ValuationBasis.PRICE_TO_GROSS_PROFIT
    assert definition.content_sha256 == _V1_SHA256
    assert definition.strategy_definition_id == f"strategy-definition:{_V1_SHA256}"
    assert _corpus()["expected"]["strategy_definition_sha256"] == _V1_SHA256


def test_the_bands_are_the_owner_confirmed_values() -> None:
    bands = load_strategy_definition(_FROZEN_RESOURCE).tier_valuation.bands
    assert [
        (
            band.tier.value,
            band.labor_efficiency_lower_bound,
            band.labor_efficiency_upper_bound,
            band.target_pgp_lower_bound,
            band.target_pgp_upper_bound,
        )
        for band in bands
    ] == list(_OWNER_BANDS)


def test_the_bands_follow_the_recorded_derivation() -> None:
    """vision.md P/S anchor / reference gross margin, rounded to 2 dp half-even."""
    for tier, _lower, _upper, target_lower, target_upper in _OWNER_BANDS:
        ps_lower, ps_upper, margin = _VISION_PS_ANCHORS[tier]
        assert _q(ps_lower / margin, 2) == target_lower
        assert _q(ps_upper / margin, 2) == target_upper


def test_definition_1_0_0_tier_cuts_and_valuation_contracts() -> None:
    """The labor-efficiency cuts are 1,000,000 / 3,000,000 and the valuation basis is P/GP."""
    v1 = load_strategy_definition(_FROZEN_RESOURCE)
    v1_cuts = [band.labor_efficiency_upper_bound for band in v1.tier_valuation.bands[:2]]
    assert v1_cuts == [Decimal("1000000"), Decimal("3000000")]
    assert v1.content_sha256 == _V1_SHA256
    assert (v1.valuation_gap.rule_id, v1.valuation_gap.formula) == (
        "target_pgp_over_current_pgp_minus_one",
        "target_pgp/current_pgp-1",
    )
    assert v1.price_to_gross_profit is not None
    assert v1.price_to_gross_profit.quantization == v1.price_to_sales.quantization
    assert v1.valuation_basis is ValuationBasis.PRICE_TO_GROSS_PROFIT


def test_the_golden_expectations_equal_the_independent_oracle() -> None:
    corpus = _corpus()
    golden = GoldenDecisionSet.model_validate_json(json.dumps(corpus["golden_decision_set"]))
    assert golden.golden_decision_set_id == corpus["expected"]["golden_decision_set_id"]
    assert golden.strategy_definition_sha256 == _V1_SHA256
    rates = {
        item["cutoff_at"]: Decimal(item["annualized_rate"]) for item in corpus["golden_decision_set"]["risk_free_rates"]
    }
    by_cutoff: dict[str, list[dict[str, Any]]] = {}
    for decision in corpus["golden_decision_set"]["decisions"]:
        by_cutoff.setdefault(decision["cutoff_at"], []).append(decision)

    checked = 0
    outcomes: set[str] = set()
    for cutoff, decisions in by_cutoff.items():
        oracle = _oracle(decisions, rates[cutoff])
        for decision in decisions:
            want = oracle[decision["issuer"]["id"]]
            got = decision["expected"]
            for key, value in want.items():
                # The file stores decimals as exact text, so the oracle's quantized value must
                # print to the same text: "0.2590" and "0.259" are different claims of precision.
                oracle_text = str(value) if isinstance(value, Decimal) else value
                assert got.get(key) == oracle_text, f"{decision['decision_key']}.{key}: file {got.get(key)!r} != oracle"
            outcomes.add(got["outcome"])
            checked += 1
    assert checked == 10
    assert outcomes == {"selected", "ranked_beyond_selection_count", "rejected_valuation_above_tier_band", "excluded"}


def test_a_pgp_band_set_requires_the_pgp_factor_definition() -> None:
    payload = _definition_payload()
    payload.pop("price_to_gross_profit")
    with pytest.raises(ValidationError, match="price_to_gross_profit factor definition"):
        _validate(payload)


def test_a_pgp_band_set_refuses_the_ps_gap_rule() -> None:
    payload = _definition_payload()
    payload["valuation_gap"]["rule_id"] = "target_ps_over_current_ps_minus_one"
    payload["valuation_gap"]["formula"] = "target_ps/current_ps-1"
    with pytest.raises(ValidationError, match="multiple of the tier bands"):
        _validate(payload)


def test_provisional_semantics_must_match_the_band_review_state() -> None:
    payload = _definition_payload()
    payload["provisional_semantics"] = True
    with pytest.raises(ValidationError, match="provisional_thresholds"):
        _validate(payload)


def test_a_ps_definition_refuses_a_pgp_factor_definition() -> None:
    payload = _definition_payload()
    payload["provisional_semantics"] = True
    payload["tier_valuation"] = {
        "factor_key": "three_tier_valuation",
        "factor_version": "0.1.0",
        "boundary_rule": "lower_inclusive_upper_exclusive",
        "target_ps_selection": "band_midpoint",
        "provisional_thresholds": True,
        "unit": {"dimension": "dimensionless", "unit_code": "ratio"},
        "quantization": {"decimal_places": 4, "rounding": "half_even"},
        "bands": [
            {
                "tier": "traditional",
                "labor_efficiency_lower_bound": None,
                "labor_efficiency_upper_bound": "1000000",
                "target_ps_lower_bound": "3.00",
                "target_ps_upper_bound": "4.00",
            },
            {
                "tier": "tech",
                "labor_efficiency_lower_bound": "1000000",
                "labor_efficiency_upper_bound": "3000000",
                "target_ps_lower_bound": "8.00",
                "target_ps_upper_bound": "10.00",
            },
            {
                "tier": "large_model_native",
                "labor_efficiency_lower_bound": "3000000",
                "labor_efficiency_upper_bound": None,
                "target_ps_lower_bound": "20.00",
                "target_ps_upper_bound": "30.00",
            },
        ],
    }
    payload["valuation_gap"]["rule_id"] = "target_ps_over_current_ps_minus_one"
    payload["valuation_gap"]["formula"] = "target_ps/current_ps-1"
    with pytest.raises(
        ValidationError, match="a P/GP tier valuation requires exactly the price_to_gross_profit factor definition"
    ):
        _validate(payload)


def test_bands_that_leave_a_gap_in_the_labor_efficiency_domain_fail_closed() -> None:
    payload = _definition_payload()
    payload["tier_valuation"]["bands"][1]["labor_efficiency_lower_bound"] = "1000001"
    with pytest.raises(ValidationError, match="contiguous"):
        _validate(payload)


def test_every_band_value_is_part_of_the_content_hash() -> None:
    mutations: tuple[tuple[tuple[int, str, str], ...], ...] = (
        ((0, "target_pgp_lower_bound", "7.49"),),
        ((2, "target_pgp_upper_bound", "40.01"),),
        ((0, "labor_efficiency_upper_bound", "999999"), (1, "labor_efficiency_lower_bound", "999999")),
    )
    seen = {_V1_SHA256}
    for mutation in mutations:
        payload = _definition_payload()
        for index, field, value in mutation:
            payload["tier_valuation"]["bands"][index][field] = value
        changed = _validate(payload).content_sha256
        assert changed not in seen, mutation
        seen.add(changed)


def test_a_golden_expectation_names_exactly_one_target_basis() -> None:
    both = {
        "capital_adjusted_labor_efficiency": "1",
        "tier": "traditional",
        "current_price_to_sales": "1",
        "target_price_to_sales": "1",
        "current_price_to_gross_profit": "1",
        "target_price_to_gross_profit": "1",
        "valuation_gap": "0",
        "eligible": True,
        "outcome": "rejected_valuation_above_tier_band",
        "exclusion_reason": None,
        "rank": None,
        "target_weight": None,
    }
    with pytest.raises(ValidationError, match="exactly one valuation basis"):
        GoldenExpectation.model_validate_json(json.dumps(both))


# --- definition 1.1.0 (#1518, owner decision D2 of 2026-10-26) ---------------------------------

_LIVE_PINS = {
    "financial": ("gppe_banking_tce", "v2"),
    "non_financial": ("gppe", "production-topt-v0.2.0"),
    "insurance": ("gppe", "production-topt-v0.2.0"),
}
#: The fields that definition 1.0.0 holds and definition 1.1.0 leaves out: the formula and the reason code of each input.
_FORMULA_FIELDS = {
    "numerator": "gross_profit",
    "capital_charge_base": "total_assets",
    "denominator": "headcount",
    "formula": "(numerator-capital_charge_base*risk_free_rate)/denominator",
    "missing_numerator_reason": "missing_gross_profit_fact",
    "missing_capital_base_reason": "missing_total_assets_fact",
    "missing_denominator_reason": "missing_headcount_disclosure",
    "nonpositive_denominator_reason": "nonpositive_headcount",
}


def _live_payload() -> dict[str, Any]:
    return copy.deepcopy(load_strategy_definition_corpus(LIVE_STRATEGY_DEFINITION_RESOURCE)["strategy_definition"])


def test_the_live_definition_is_1_1_0_and_changes_only_the_owner_of_labor_efficiency() -> None:
    live = load_live_strategy_definition()
    frozen = load_strategy_definition(_FROZEN_RESOURCE)
    # The exact difference in `labor_efficiency` besides the formula fields: the factor version.
    assert (frozen.labor_efficiency.factor_version, live.labor_efficiency.factor_version) == ("0.1.0", "0.2.0")
    for name in ("factor_key", "risk_free", "unit", "quantization"):
        assert getattr(live.labor_efficiency, name) == getattr(frozen.labor_efficiency, name), name
    assert live.definition_version == "1.1.0"
    assert live.provisional_semantics is False
    assert live.content_sha256 != frozen.content_sha256
    for name in (
        "schedule",
        "price_to_sales",
        "price_to_gross_profit",
        "tier_valuation",
        "valuation_gap",
        "eligibility",
        "selection",
        "sizing",
        "execution",
        "transaction_cost",
        "total_return",
    ):
        assert getattr(live, name) == getattr(frozen, name), f"definition 1.1.0 changed {name}"


def test_the_live_definition_states_no_formula_and_pins_one_tree_per_class() -> None:
    live = load_live_strategy_definition()
    labor = live.labor_efficiency
    assert [getattr(labor, name) for name in _FORMULA_FIELDS] == [None] * len(_FORMULA_FIELDS)
    assert not set(_FORMULA_FIELDS) & set(json.loads(labor.model_dump_json())), "an absent formula is not dumped"
    # The frozen 1.0.0 holds all eight, so its content hash is the one that its stored runs carry.
    frozen_labor = load_strategy_definition(_FROZEN_RESOURCE).labor_efficiency
    assert set(_FORMULA_FIELDS) <= set(json.loads(frozen_labor.model_dump_json()))
    pins = live.labor_efficiency_tree_keys
    assert pins is not None
    assert {name: (pin.tree_key, pin.tree_version) for name, pin in pins.items()} == _LIVE_PINS


def test_the_formula_fields_are_all_present_or_all_absent() -> None:
    payload = _live_payload()
    payload["labor_efficiency"]["numerator"] = "gross_profit"
    with pytest.raises(ValidationError, match="all present or all absent"):
        _validate(payload)


def test_a_definition_names_exactly_one_owner_of_labor_efficiency() -> None:
    both = _live_payload()
    both["labor_efficiency"].update(_FORMULA_FIELDS)
    with pytest.raises(ValidationError, match="one owner"):
        _validate(both)
    neither = _live_payload()
    del neither["labor_efficiency_tree_keys"]
    with pytest.raises(ValidationError, match="one owner"):
        _validate(neither)


def test_a_tree_pin_is_part_of_the_content_hash() -> None:
    changed = _live_payload()
    changed["labor_efficiency_tree_keys"]["financial"]["tree_sha256"] = "0" * 64
    assert _validate(changed).content_sha256 != load_live_strategy_definition().content_sha256


def test_the_authoring_record_of_1_1_0_names_the_hash_and_the_pins_of_the_live_file() -> None:
    """The record states the identity of the file. A change of the file without the record turns this red."""
    from pathlib import Path

    record = (
        Path(__file__).resolve().parents[3] / "governance" / "reviews" / "large_model_value_v0-definition-1.1.0.md"
    ).read_text(encoding="utf-8")
    live = load_live_strategy_definition()
    assert live.content_sha256 in record
    assert live.strategy_definition_id in record
    assert live.labor_efficiency_tree_keys is not None
    for pin in live.labor_efficiency_tree_keys.values():
        assert pin.tree_sha256 in record and pin.tree_version in record
    assert "not a review" in record, "the record must not claim an owner review that did not happen"
