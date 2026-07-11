from pathlib import Path

from data_engine.quality.strategy_samples import audit_strategy_samples
from truealpha_contracts import ReadinessLevel

SAMPLE_ROOT = Path(__file__).parents[1] / "samples"


def test_current_samples_are_ready_for_toolchain_development_only():
    report = audit_strategy_samples(SAMPLE_ROOT)

    assert report.assessment(ReadinessLevel.TOOLCHAIN).ready
    assert not report.assessment(ReadinessLevel.LOCAL_BACKTEST).ready
    assert not report.assessment(ReadinessLevel.STRATEGY_EVALUATION).ready


def test_current_backtest_blockers_are_explicit():
    report = audit_strategy_samples(SAMPLE_ROOT)
    blockers = set(report.assessment(ReadinessLevel.LOCAL_BACKTEST).blockers)

    assert {
        "prices.history",
        "corporate_actions.total_return",
        "universe.membership_history",
        "financial.restatement_vintages",
        "graph.supply_chain_history",
        "analyst.knowability",
        "universe.strategy_diversity",
        "factors.point_in_time_outputs",
    } <= blockers
