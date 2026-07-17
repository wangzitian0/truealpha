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


def test_missing_gross_profit_issuer_is_excluded_with_exact_reason() -> None:
    decisions, _ = RUNNER.run()

    jpm = [item for item in decisions if item.issuer_id == "issuer:jpm"]
    assert len(jpm) == 2
    for decision in jpm:
        assert decision.outcome == "excluded"
        assert decision.exclusion_reason == "missing_gross_profit_fact"


def test_below_confidence_floor_issuer_is_excluded_despite_complete_inputs() -> None:
    decisions, _ = RUNNER.run()

    ddog = [item for item in decisions if item.issuer_id == "issuer:ddog"]
    assert len(ddog) == 2
    for decision in ddog:
        assert decision.outcome == "excluded"
        assert decision.exclusion_reason == "below_confidence_floor"
        # GPPE itself was computable (headcount confidence 0.65 drags the
        # composite below the 0.70 floor, but the value is not missing).
        assert decision.capital_adjusted_labor_efficiency is not None


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


def _eligible_decision(issuer_id: str, valuation_gap: str):
    return RUNNER.Decision(
        issuer_id,
        "2026-03-31T23:59:59Z",
        Decimal("100000"),
        "tech",
        Decimal("1.0"),
        Decimal("1.5"),
        Decimal(valuation_gap),
        True,
        "ranked_beyond_selection_count",
        None,
    )


def test_candidates_beyond_selection_count_are_ranked_but_not_selected() -> None:
    """The golden fixture happens to have exactly `selection_count` eligible
    candidates, so this branch is never exercised by test_reproduces_all_ten_
    golden_decisions_exactly -- covered directly here with a synthetic third
    candidate."""

    decisions = [
        _eligible_decision("issuer:a", "0.50"),
        _eligible_decision("issuer:b", "0.30"),
        _eligible_decision("issuer:c", "0.10"),
    ]

    resolved = RUNNER._rank_and_select(decisions, selection_count=2)
    by_issuer = {item.issuer_id: item for item in resolved}

    assert by_issuer["issuer:a"].outcome == "selected"
    assert by_issuer["issuer:a"].rank == 1
    assert by_issuer["issuer:a"].target_weight == Decimal("0.500000")
    assert by_issuer["issuer:b"].outcome == "selected"
    assert by_issuer["issuer:b"].rank == 2
    assert by_issuer["issuer:b"].target_weight == Decimal("0.500000")
    assert by_issuer["issuer:c"].outcome == "ranked_beyond_selection_count"
    assert by_issuer["issuer:c"].rank == 3
    assert by_issuer["issuer:c"].target_weight is None


def test_ranking_ties_break_by_ascending_issuer_id() -> None:
    decisions = [
        _eligible_decision("issuer:zeta", "0.20"),
        _eligible_decision("issuer:alpha", "0.20"),
    ]

    resolved = RUNNER._rank_and_select(decisions, selection_count=1)
    by_issuer = {item.issuer_id: item for item in resolved}

    assert by_issuer["issuer:alpha"].outcome == "selected"
    assert by_issuer["issuer:alpha"].rank == 1
    assert by_issuer["issuer:zeta"].outcome == "ranked_beyond_selection_count"
    assert by_issuer["issuer:zeta"].rank == 2
