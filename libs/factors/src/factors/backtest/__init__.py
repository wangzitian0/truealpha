"""Backtest module containing factor adapters, high-performance engines, and storage."""

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
from factors.backtest.storage import persist_backtest_result

__all__ = [
    "BacktestEngineConfig",
    "BacktestResult",
    "VectorBTBacktestEngine",
    "canonical_run_id",
    "compile_factor_panel",
    "compute_topk_dropout_weights",
    "persist_backtest_result",
    "pivot_to_vbt_matrices",
]
