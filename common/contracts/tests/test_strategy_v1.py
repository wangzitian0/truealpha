from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from truealpha_contracts import strategy as _st

pytestmark = pytest.mark.contract

_S = importlib.util.spec_from_file_location("ch", Path(__file__).parent / "fixtures/contract_helpers.py")
assert _S and _S.loader
_H = importlib.util.module_from_spec(_S)
_S.loader.exec_module(_H)
_GS, _V1, _apply = _H.load_fixture_mod("golden_strategy.py"), _H.CASES["strategy_v1"], _H.apply_ops
_FROZEN, _CF, _SEL = "large_model_value_v0_definition_1_0_0.json", Decimal("0.70"), 2
_SHA = "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803"
_CORPUS_SHA = "75b2c9f36abbdd0ba7237d079252f70d67186adbff3003a2e9fa6922b6e2985e"


def _q(v: Decimal, n: int) -> Decimal:
    return v.quantize(Decimal(1).scaleb(-n), ROUND_HALF_EVEN)


def _val(p: dict[str, Any]) -> _st.LargeModelValueV0Definition:
    return _st.LargeModelValueV0Definition.model_validate_json(json.dumps(p))


def test_frozen_1_0_0_and_live_1_1_0() -> None:
    raw = resources.files("truealpha_contracts.data").joinpath(_FROZEN).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _CORPUS_SHA
    d, corpus = _st.load_strategy_definition(_FROZEN), _st.load_strategy_definition_corpus(_FROZEN)
    bands = [
        (t, None if lo is None else Decimal(lo), None if u is None else Decimal(u), Decimal(tl), Decimal(tu))
        for t, lo, u, tl, tu in _V1["bands"]
    ]
    ps = {k: tuple(Decimal(x) for x in v) for k, v in _V1["ps"].items()}
    assert (d.strategy_id, d.definition_version, d.provisional_semantics) == ("large_model_value_v0", "1.0.0", False)
    assert not d.tier_valuation.provisional_thresholds and d.valuation_basis is _st.ValuationBasis.PRICE_TO_GROSS_PROFIT
    assert d.content_sha256 == _SHA and d.strategy_definition_id == f"strategy-definition:{_SHA}"
    assert _val(json.loads(d.model_dump_json())).strategy_definition_id == d.strategy_definition_id
    assert all(
        f.factor_definition_id == f"factor-definition:{f.content_sha256}"
        for f in (d.labor_efficiency, d.price_to_sales, d.tier_valuation)
    )
    assert d.labor_efficiency.unit.dimension is _st.FactorDimension.REPORTING_CURRENCY_PER_EMPLOYEE
    assert d.price_to_sales.unit.dimension is _st.FactorDimension.DIMENSIONLESS
    assert d.tier_valuation.unit.dimension is _st.FactorDimension.DIMENSIONLESS
    assert (
        d.price_to_gross_profit is not None
        and d.price_to_gross_profit.unit.dimension is _st.FactorDimension.DIMENSIONLESS
    )
    tv = d.tier_valuation
    got_b = [
        (
            b.tier.value,
            b.labor_efficiency_lower_bound,
            b.labor_efficiency_upper_bound,
            b.target_pgp_lower_bound,
            b.target_pgp_upper_bound,
        )
        for b in tv.bands
    ]
    assert got_b == bands and [b.labor_efficiency_upper_bound for b in tv.bands[:2]] == [
        Decimal("1000000"),
        Decimal("3000000"),
    ]
    assert all((_q(ps[t][0] / ps[t][2], 2), _q(ps[t][1] / ps[t][2], 2)) == (tl, tu) for t, _l, _u, tl, tu in bands)
    assert (d.valuation_gap.rule_id, d.valuation_gap.formula) == (
        "target_pgp_over_current_pgp_minus_one",
        "target_pgp/current_pgp-1",
    )
    assert d.price_to_gross_profit.quantization == d.price_to_sales.quantization
    gc = json.loads((Path(__file__).parent / "fixtures/large_model_value_v0_golden_1_0_0.json").read_text("utf-8"))
    g = _GS.GoldenDecisionSet.model_validate_json(json.dumps(gc["golden_decision_set"]))
    assert g.golden_decision_set_id == gc["expected"]["golden_decision_set_id"]
    assert (g.strategy_definition_id, g.strategy_definition_sha256) == (d.strategy_definition_id, _SHA)
    assert len(g.decisions) == 10 and {x.issuer.id for x in g.decisions} == set(_V1["issuers"])
    assert {x.cutoff_at.isoformat() for x in g.decisions} == {"2026-03-31T23:59:59+00:00", "2026-06-30T23:59:59+00:00"}
    g_out = [x.expected.outcome for x in g.decisions]
    assert tuple(g_out.count(k) for k in _st.GoldenDecisionOutcome) == (4, 1, 3, 2)
    assert {x.expected.exclusion_reason for x in g.decisions if x.issuer.id == "issuer:ddog"} == {
        _st.ExclusionReason.BELOW_CONFIDENCE_FLOOR
    }
    assert {x.expected.exclusion_reason for x in g.decisions if x.issuer.id == "issuer:jpm"} == {None}
    assert all(
        r.grounding.startswith("apps/data-engine/samples/") and r.knowable_at <= x.cutoff_at
        for x in g.decisions
        for r in x.inputs
    )
    rates = {x["cutoff_at"]: Decimal(x["annualized_rate"]) for x in gc["golden_decision_set"]["risk_free_rates"]}
    by_cut: dict[str, list[dict[str, Any]]] = {}
    for dec in gc["golden_decision_set"]["decisions"]:
        by_cut.setdefault(dec["cutoff_at"], []).append(dec)
    out, checked = set(), 0
    for c, decs in by_cut.items():
        ora, cand, rate = {}, [], rates[c]
        for x in decs:
            iid, vals = x["issuer"]["id"], {k["input_key"]: Decimal(k["value"]) for k in x["inputs"]}
            if min(Decimal(k["confidence"]) for k in x["inputs"]) < _CF:
                ora[iid] = {"outcome": "excluded", "exclusion_reason": "below_confidence_floor", "tier": None}
                continue
            le = _q((vals["gross_profit"] - vals["total_assets"] * rate) / vals["headcount"], 2)
            t, _l, _u, tl, tu = next(b for b in bands if (b[1] is None or le >= b[1]) and (b[2] is None or le < b[2]))
            mv = vals["shares_outstanding"] * vals["last_close"]
            pgp, tpgp = _q(mv / vals["gross_profit"], 4), _q((tl + tu) / 2, 4)
            gap, rej = _q(tpgp / pgp - 1, 4), pgp > tu
            row = [
                le,
                t,
                _q(mv / vals["revenue"], 4),
                None,
                pgp,
                tpgp,
                gap,
                "rejected_valuation_above_tier_band" if rej else None,
                None,
                None,
                None,
            ]
            ora[iid] = dict(zip(_V1["ora_keys"], row, strict=True))
            if not rej:
                cand.append((gap, iid))
        ranked = sorted(cand, key=lambda item: (-item[0], item[1]))
        w = _q(Decimal(1) / Decimal(max(min(len(ranked), _SEL), 1)), 6)
        for pos, (_g, iid) in enumerate(ranked, start=1):
            ora[iid].update(
                rank=pos,
                outcome="selected" if pos <= _SEL else "ranked_beyond_selection_count",
                target_weight=w if pos <= _SEL else None,
            )
        for dec in decs:
            assert all(
                dec["expected"].get(k) == (str(v) if isinstance(v, Decimal) else v)
                for k, v in ora[dec["issuer"]["id"]].items()
            )
            out.add(dec["expected"]["outcome"])
            checked += 1
    assert checked == 10 and out == {o.value for o in _st.GoldenDecisionOutcome}
    for ops, msg in _V1["frozen_bad"]:
        p = copy.deepcopy(corpus["strategy_definition"])
        _apply(p, ops)
        with pytest.raises(ValidationError, match=msg):
            _val(p)
    seen = {_SHA}
    for mut in _V1["hash_variants"]:
        pm = copy.deepcopy(corpus["strategy_definition"])
        _apply(pm, mut)
        assert (ch := _val(pm).content_sha256) not in seen
        seen.add(ch)
    assert _val(pm).labor_efficiency.unit.dimension is _st.FactorDimension.DIMENSIONLESS
    with pytest.raises(ValidationError):
        _st.ConfidenceEligibilityRule(
            rule_id="minimum_consumed_confidence_floor",
            minimum_confidence=0.7,
            confidence_semantics="minimum_consumed_input_confidence",
            maximum_input_age_days=455,
            below_floor_reason=_st.ExclusionReason.BELOW_CONFIDENCE_FLOOR,
            stale_input_reason=_st.ExclusionReason.STALE_REQUIRED_INPUT,
        )  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _GS.GoldenInputRecord(
            input_key="gross_profit",
            value=1956145000.0,
            unit_code="usd",
            confidence=Decimal("0.90"),
            knowable_at=datetime(2026, 2, 26, tzinfo=UTC),
            grounding="apps/data-engine/samples/sec/NICE_CIK0001003935.json",
        )
    with pytest.raises(ValidationError, match="exactly one valuation basis|exact one basis"):
        _GS.GoldenExpectation.model_validate_json(json.dumps(_V1["golden_dual_basis"]))
    live, live_raw = (
        _st.load_live_strategy_definition(),
        _st.load_strategy_definition_corpus(_st.LIVE_STRATEGY_DEFINITION_RESOURCE)["strategy_definition"],
    )
    assert (d.labor_efficiency.factor_version, live.labor_efficiency.factor_version, live.definition_version) == (
        "0.1.0",
        "0.2.0",
        "1.1.0",
    )
    assert not live.provisional_semantics and live.content_sha256 != d.content_sha256
    assert all(
        getattr(live.labor_efficiency, n) == getattr(d.labor_efficiency, n)
        for n in ("factor_key", "risk_free", "unit", "quantization")
    )
    assert all(
        getattr(live.eligibility, n) == getattr(d.eligibility, n)
        for n in ("maximum_input_age_days", "stale_input_reason")
    )
    assert [getattr(live.eligibility, n) for n in _V1["cff"]] == [None] * 4
    assert not set(_V1["cff"]) & set(json.loads(live.eligibility.model_dump_json())) and set(_V1["cff"]) <= set(
        json.loads(d.eligibility.model_dump_json())
    )
    assert (
        all(getattr(live, n) == getattr(d, n) for n in _V1["shared_attrs"])
        and [getattr(live.labor_efficiency, n) for n in _V1["ff"]] == [None] * 8
    )
    assert not set(_V1["ff"]) & set(json.loads(live.labor_efficiency.model_dump_json())) and set(_V1["ff"]) <= set(
        json.loads(d.labor_efficiency.model_dump_json())
    )
    assert (
        live.labor_efficiency_tree_keys is not None
        and {n: [p.tree_key, p.tree_version] for n, p in live.labor_efficiency_tree_keys.items()} == _V1["pins"]
    )
    for ops, msg in _V1["live_bad"]:
        p = copy.deepcopy(live_raw)
        _apply(p, ops)
        with pytest.raises(ValidationError, match=msg):
            _val(p)
    c5 = copy.deepcopy(live_raw)
    c5["labor_efficiency_tree_keys"]["financial"]["tree_sha256"] = "0" * 64
    assert _val(c5).content_sha256 != live.content_sha256
