from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[3]
SCRIPT_PATH = REPOSITORY_ROOT / "apps/data-engine/scripts/run_strategy_smoke.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("strategy_smoke_runner", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


def test_reproduces_all_ten_golden_decisions_exactly() -> None:
    decisions, definition = RUNNER.run()
    corpus = RUNNER._load_corpus()

    assert len(decisions) == 10
    assert definition.strategy_id == "large_model_value_v0"

    mismatches = RUNNER._compare_against_golden(decisions, corpus)
    assert mismatches == []


def test_selected_issuers_get_equal_weight_summing_to_one_per_cutoff() -> None:
    decisions, _ = RUNNER.run()

    by_cutoff: dict[str, list] = {}
    for decision in decisions:
        by_cutoff.setdefault(decision.cutoff_at, []).append(decision)

    for group in by_cutoff.values():
        selected = [item for item in group if item.outcome == "selected"]
        assert len(selected) == 2
        assert sum((item.target_weight for item in selected), start=Decimal(0)) == Decimal("1.000000")


def test_excluded_and_rejected_issuers_carry_no_rank_or_weight() -> None:
    decisions, _ = RUNNER.run()

    for decision in decisions:
        if decision.outcome in ("excluded", "rejected_valuation_above_tier_band"):
            assert decision.rank is None
            assert decision.target_weight is None


def test_financial_issuer_flows_through_the_uniform_formula_and_is_rejected() -> None:
    decisions, _ = RUNNER.run()

    jpm = [item for item in decisions if item.issuer_id == "issuer:jpm"]
    assert len(jpm) == 2
    for decision in jpm:
        # The v0 formula is uniform (2026-07-18 owner decision): JPM takes the
        # identical capital-adjusted path. Its large balance sheet drives labor
        # efficiency negative -> traditional tier -> P/S above that band, so it
        # is rejected exactly like any other over-valued issuer, not sector-excluded.
        assert decision.capital_adjusted_labor_efficiency is not None
        assert decision.capital_adjusted_labor_efficiency < 0
        assert decision.tier == "traditional"
        assert decision.outcome == "rejected_valuation_above_tier_band"
        assert decision.exclusion_reason is None


def test_below_confidence_floor_issuer_is_excluded_despite_complete_inputs() -> None:
    decisions, _ = RUNNER.run()

    ddog = [item for item in decisions if item.issuer_id == "issuer:ddog"]
    assert len(ddog) == 2
    for decision in ddog:
        assert decision.outcome == "excluded"
        assert decision.exclusion_reason == "below_confidence_floor"
        # Evaluation order (#21): missing inputs -> confidence floor -> factor
        # computation. DDOG's headcount confidence 0.65 fails the 0.70 floor
        # *before* any factor runs, so no factor outputs are recorded — matching
        # the golden (labor_eff is null for below-floor exclusions).
        assert decision.capital_adjusted_labor_efficiency is None


def test_main_writes_json_and_markdown_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_strategy_smoke.py", "--output-dir", str(tmp_path)])

    exit_code = RUNNER.main()

    assert exit_code == 0
    assert (tmp_path / RUNNER.OUTPUT_JSON).exists()
    assert (tmp_path / RUNNER.OUTPUT_MARKDOWN).exists()
    report = (tmp_path / RUNNER.OUTPUT_JSON).read_text()
    assert '"golden_mismatches": []' in report


def test_render_markdown_names_the_strategy_and_a_known_issuer() -> None:
    decisions, _ = RUNNER.run()

    markdown = RUNNER.render_markdown(decisions)

    assert "large_model_value_v0" in markdown
    assert "issuer:adm" in markdown
