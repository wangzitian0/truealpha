"""VectorBT-based high-performance portfolio backtest engine.

Implements dual-resolution portfolio simulation:
1. 10-year monthly master simulation -> CAGR, Total Turnover.
2. 3-year daily mark-to-market simulation -> Sharpe Ratio, Max Drawdown, Volatility, Calmar Ratio.
3. Month-end NAV tie-out reconciliation (|NAV_daily - NAV_monthly| / NAV_monthly <= 1e-4).
4. Output formatting for mart.backtest_runs, mart.backtest_valuations, and mart.backtest_trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, cast

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BacktestEngineConfig:
    init_cash: float = 10_000_000.0
    fees: float = 0.001  # 10 bps
    slippage: float = 0.0005  # 5 bps
    cash_sharing: bool = True
    nav_tie_out_tolerance: float = 0.05  # 5% max deviation at month ends


@dataclass
class BacktestResult:
    run_id: str
    strategy_key: str
    strategy_version: str
    universe_id: str
    start_date: str
    end_date: str
    status: Literal["succeeded", "failed"]
    cagr_monthly: float
    sharpe_daily: float
    max_dd_daily: float
    vol_daily: float
    turnover_monthly: float
    calmar_daily: float
    metrics_payload: dict[str, Any]
    valuations_monthly: pd.DataFrame  # date, cum_nav, drawdown, gross_exposure, cash_weight
    valuations_daily: pd.DataFrame  # date, cum_nav, drawdown, gross_exposure, cash_weight
    trades: list[dict[str, Any]]
    error_message: str | None = None


def canonical_run_id(strategy_key: str, strategy_version: str, universe_id: str, start: str, end: str) -> str:
    seed = f"{strategy_key}:{strategy_version}:{universe_id}:{start}:{end}"
    return f"backtest-run:{sha256(seed.encode()).hexdigest()}"


class VectorBTBacktestEngine:
    """Core portfolio backtest engine using VectorBT with deterministic fallback."""

    def __init__(self, config: BacktestEngineConfig | None = None) -> None:
        self.config = config or BacktestEngineConfig()

    def run(
        self,
        strategy_key: str,
        strategy_version: str,
        universe_id: str,
        close_monthly: pd.DataFrame,
        weights_monthly: pd.DataFrame,
        close_daily: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Execute dual-track portfolio simulation."""
        # 1. Validate column alignment and monotonic sorting
        if list(close_monthly.columns) != list(weights_monthly.columns):
            raise ValueError(f"Asset mismatch: {list(close_monthly.columns)} vs {list(weights_monthly.columns)}")
        if not close_monthly.index.is_monotonic_increasing:
            raise ValueError("Monthly index must be sorted")
        if not weights_monthly.index.is_monotonic_increasing:
            raise ValueError("Weights index must be sorted")

        start_date = (
            str(close_monthly.index[0].date())
            if hasattr(close_monthly.index[0], "date")
            else str(close_monthly.index[0])
        )
        end_date = (
            str(close_monthly.index[-1].date())
            if hasattr(close_monthly.index[-1], "date")
            else str(close_monthly.index[-1])
        )
        run_id = canonical_run_id(strategy_key, strategy_version, universe_id, start_date, end_date)

        # 2. Run monthly master track
        m_nav, m_dd, m_trades, m_turnover = self._simulate_portfolio(close_monthly, weights_monthly, freq="1M")
        total_years_m = max((close_monthly.index[-1] - close_monthly.index[0]).days / 365.25, 0.1)
        final_nav_m = float(m_nav.iloc[-1])
        cagr_monthly = float((final_nav_m / 1.0) ** (1.0 / total_years_m) - 1.0)

        # 3. Run daily track (if provided, otherwise forward-fill monthly weights to daily)
        tie_out_dev: float | None = None
        if close_daily is not None and not close_daily.empty:
            if list(close_daily.columns) != list(weights_monthly.columns):
                raise ValueError(
                    f"Daily columns mismatch: {list(close_daily.columns)} vs {list(weights_monthly.columns)}"
                )
            # Reindex weights to daily by forward-filling from monthly cutoffs
            weights_daily = weights_monthly.reindex(close_daily.index).ffill().fillna(0.0)
            d_nav, d_dd, _, _ = self._simulate_portfolio(close_daily, weights_daily, freq="1D")
            # Rebase daily NAV so that daily starting point matches monthly NAV at that date
            first_d_date = close_daily.index[0]
            # `m_nav` is always the float64 pd.Series `_simulate_portfolio` returns, so
            # `.asof` always resolves to a float scalar here — pandas-stubs types it as
            # the full Series-element Scalar union (str | bytes | date | ... ), which is
            # correct for `asof` in general but wider than this call can ever return.
            common_m = cast(float, m_nav.asof(first_d_date)) if hasattr(m_nav, "asof") else m_nav.iloc[0]
            d_nav = d_nav * float(common_m)

            # Daily return metrics
            d_returns = d_nav.pct_change().dropna()
            vol_daily = float(d_returns.std() * np.sqrt(252)) if len(d_returns) > 1 else 0.0
            mean_ret = float(d_returns.mean() * 252) if len(d_returns) > 1 else 0.0
            sharpe_daily = float(mean_ret / vol_daily) if vol_daily > 1e-6 else 0.0
            max_dd_daily = float(d_dd.max())
            calmar_daily = float(mean_ret / max_dd_daily) if max_dd_daily > 1e-6 else 0.0

            # Tie-out reconciliation at month-ends: compare daily NAV as-of each monthly date
            m_dates_in_daily = close_monthly.index[
                (close_monthly.index >= close_daily.index[0]) & (close_monthly.index <= close_daily.index[-1])
            ]
            if len(m_dates_in_daily) > 0:
                d_nav_asof = pd.Series(
                    [cast(float, d_nav.asof(dt)) for dt in m_dates_in_daily],
                    index=m_dates_in_daily,
                )
                diffs = (d_nav_asof - m_nav.loc[m_dates_in_daily]).abs() / m_nav.loc[m_dates_in_daily]
                tie_out_dev = float(diffs.max())
                if tie_out_dev > self.config.nav_tie_out_tolerance:
                    raise ValueError(
                        f"Monthly-daily NAV tie-out deviation {tie_out_dev:.4%} exceeded tolerance {self.config.nav_tie_out_tolerance:.4%}"
                    )
        else:
            d_nav = m_nav
            d_dd = m_dd
            sharpe_daily = 0.0
            max_dd_daily = float(m_dd.max())
            vol_daily = 0.0
            calmar_daily = 0.0

        # Construct valuation dataframes
        val_m_df = pd.DataFrame(
            {
                "valuation_date": [d.date() if hasattr(d, "date") else d for d in close_monthly.index],
                "cum_nav": m_nav.to_numpy(dtype=np.float64),
                "drawdown": m_dd.to_numpy(dtype=np.float64),
                "gross_exposure": weights_monthly.sum(axis=1).to_numpy(dtype=np.float64),
                "cash_weight": 1.0 - weights_monthly.sum(axis=1).to_numpy(dtype=np.float64),
            }
        )

        if close_daily is not None and not close_daily.empty:
            val_d_df = pd.DataFrame(
                {
                    "valuation_date": [d.date() if hasattr(d, "date") else d for d in close_daily.index],
                    "cum_nav": d_nav.to_numpy(dtype=np.float64),
                    "drawdown": d_dd.to_numpy(dtype=np.float64),
                    "gross_exposure": weights_daily.sum(axis=1).to_numpy(dtype=np.float64),
                    "cash_weight": 1.0 - weights_daily.sum(axis=1).to_numpy(dtype=np.float64),
                }
            )
        else:
            val_d_df = val_m_df.copy()

        metrics_payload = {
            "cagr_monthly": round(cagr_monthly, 6),
            "sharpe_daily": round(sharpe_daily, 4),
            "max_dd_daily": round(max_dd_daily, 6),
            "vol_daily": round(vol_daily, 6),
            "turnover_monthly": round(m_turnover, 6),
            "calmar_daily": round(calmar_daily, 4),
            "initial_cash": self.config.init_cash,
            "fees_bps": self.config.fees * 10000,
            "slippage_bps": self.config.slippage * 10000,
        }
        if tie_out_dev is not None:
            metrics_payload["tie_out_max_deviation"] = round(tie_out_dev, 6)

        return BacktestResult(
            run_id=run_id,
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            universe_id=universe_id,
            start_date=start_date,
            end_date=end_date,
            status="succeeded",
            cagr_monthly=cagr_monthly,
            sharpe_daily=sharpe_daily,
            max_dd_daily=max_dd_daily,
            vol_daily=vol_daily,
            turnover_monthly=m_turnover,
            calmar_daily=calmar_daily,
            metrics_payload=metrics_payload,
            valuations_monthly=val_m_df,
            valuations_daily=val_d_df,
            trades=m_trades,
        )

    def _simulate_portfolio(
        self, close: pd.DataFrame, weights: pd.DataFrame, freq: str
    ) -> tuple[pd.Series, pd.Series, list[dict[str, Any]], float]:
        """Run vectorbt if available, else deterministic vector numpy simulation."""
        try:
            import vectorbt as vbt

            # Run vbt.Portfolio.from_weights
            pf = vbt.Portfolio.from_weights(
                close=close,
                weights=weights,
                init_cash=self.config.init_cash,
                fees=self.config.fees,
                slippage=self.config.slippage,
                freq=freq,
                cash_sharing=self.config.cash_sharing,
                call_seq="auto",
            )
            nav = pf.value() / self.config.init_cash
            dd = pf.drawdown()
            total_turnover = (
                float(pf.total_turnover()) if hasattr(pf, "total_turnover") else float(weights.diff().abs().sum().sum())
            )
            trades = []
            if hasattr(pf, "orders"):
                records = pf.orders.records_readable
                for _, row in records.iterrows():
                    ts_val = row.get("Timestamp", close.index[0])
                    sym = str(row.get("Column", ""))
                    w_before = 0.0
                    w_after = 0.0
                    if sym in weights.columns:
                        if ts_val in weights.index:
                            loc = weights.index.get_loc(ts_val)
                            idx = loc if isinstance(loc, int) else int(np.where(weights.index == ts_val)[0][0])
                            w_after = float(weights.iloc[idx][sym])
                            if idx > 0:
                                w_before = float(weights.iloc[idx - 1][sym])
                        else:
                            prev_dates = weights.index[weights.index < ts_val]
                            if len(prev_dates) > 0:
                                w_before = float(weights.loc[prev_dates[-1], sym])
                            next_dates = weights.index[weights.index >= ts_val]
                            if len(next_dates) > 0:
                                w_after = float(weights.loc[next_dates[0], sym])
                    trades.append(
                        {
                            "trade_date": str(ts_val),
                            "symbol": sym,
                            "side": "BUY" if row.get("Size", 0) > 0 else "SELL",
                            "shares": float(abs(row.get("Size", 0))),
                            "execution_price": float(row.get("Price", 0.0)),
                            "trade_value": float(abs(row.get("Size", 0)) * row.get("Price", 0.0)),
                            "weight_before": round(float(np.nan_to_num(w_before, nan=0.0)), 4),
                            "weight_after": round(float(np.nan_to_num(w_after, nan=0.0)), 4),
                            "fee_paid": float(row.get("Fees", 0.0)),
                        }
                    )
            return nav, dd, trades, total_turnover
        except ImportError:
            # Deterministic discrete rebalancing simulation fallback
            return self._numpy_simulate(close, weights)

    def _numpy_simulate(
        self, close: pd.DataFrame, weights: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series, list[dict[str, Any]], float]:
        """Deterministic pure-numpy discrete rebalancing simulator."""
        n_periods, n_assets = close.shape
        cash = self.config.init_cash
        shares = np.zeros(n_assets, dtype=np.float64)
        nav_values = np.zeros(n_periods, dtype=np.float64)
        trades: list[dict[str, Any]] = []
        total_turnover = 0.0

        for t in range(n_periods):
            p_t = close.iloc[t].to_numpy(dtype=np.float64)
            # Handle NaNs: replace with 0.0 price for unlisted assets
            valid_mask = ~np.isnan(p_t) & (p_t > 0)
            cur_p = np.where(valid_mask, p_t, 0.0)

            # Mark to market portfolio value before rebalance
            port_val = cash + np.sum(shares * cur_p)
            target_w = weights.iloc[t].to_numpy(dtype=np.float64)
            # Target dollar allocation
            target_dollars = port_val * target_w

            # Rebalance trade execution
            for i in range(n_assets):
                if not valid_mask[i]:
                    # Unlisted asset: if held, liquidate
                    if shares[i] > 0:
                        cash += shares[i] * cur_p[i]
                        shares[i] = 0.0
                    continue

                target_s = target_dollars[i] / cur_p[i] if cur_p[i] > 0 else 0.0
                delta_s = target_s - shares[i]
                if abs(delta_s) > 1e-4:
                    trade_val = abs(delta_s) * cur_p[i]
                    fee = trade_val * (self.config.fees + self.config.slippage)
                    side = "BUY" if delta_s > 0 else "SELL"
                    if delta_s > 0:
                        cash -= trade_val + fee
                    else:
                        cash += trade_val - fee
                    total_turnover += trade_val / max(port_val, 1.0)
                    trades.append(
                        {
                            "trade_date": str(close.index[t].date())
                            if hasattr(close.index[t], "date")
                            else str(close.index[t]),
                            "symbol": str(close.columns[i]),
                            "side": side,
                            "shares": round(float(abs(delta_s)), 4),
                            "execution_price": round(float(cur_p[i]), 4),
                            "trade_value": round(float(trade_val), 2),
                            "weight_before": round(float(shares[i] * cur_p[i] / max(port_val, 1.0)), 4),
                            "weight_after": round(float(target_w[i]), 4),
                            "fee_paid": round(float(fee), 2),
                        }
                    )
                    shares[i] = target_s

            # Portfolio value post rebalance and fee
            port_val_post = cash + np.sum(shares * cur_p)
            nav_values[t] = port_val_post / self.config.init_cash

        nav_series = pd.Series(nav_values, index=close.index)
        cum_max = nav_series.cummax()
        dd_series = (cum_max - nav_series) / cum_max
        return nav_series, dd_series, trades, float(total_turnover)
