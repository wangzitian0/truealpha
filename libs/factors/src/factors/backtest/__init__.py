"""Backtest module unifying adapter and VectorBT engine."""

from factors.backtest.adapter import (
    compile_factor_panel,
    compute_topk_dropout_weights,
    pivot_to_vbt_matrices,
)
from factors.backtest.engine import (
    BacktestEngineConfig,
    BacktestResult,
    VectorBTBacktestEngine,
    canonical_run_id,
)

__all__ = [
    "BacktestEngineConfig",
    "BacktestResult",
    "VectorBTBacktestEngine",
    "canonical_run_id",
    "compile_factor_panel",
    "compute_topk_dropout_weights",
    "pivot_to_vbt_matrices",
]
