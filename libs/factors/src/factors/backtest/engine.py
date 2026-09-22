"""High-performance single-track portfolio backtest engine.

Implements unified single-track daily portfolio simulation:
1. Daily mark-to-market simulation loop with discrete rebalancing on cutoff dates.
2. Non-rebalance dates execute Buy & Hold with static shares.
3. Missing prices (NaN or <= 0) fallback to Last Known Price; liquidating at 0 is strictly forbidden.
4. Month-end NAV is sampled directly from daily NAV, guaranteeing tie-out identity (max_dev == 0.0).
5. Canonical run_id binds strategy metadata, dates, top_k, dropout_k, fees, slippage, and snapshot hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BacktestEngineConfig:
    init_cash: float = 10_000_000.0
    fees: float = 0.001  # 10 bps
    slippage: float = 0.0005  # 5 bps
    cash_sharing: bool = True
    top_k: int = 5
    dropout_k: int = 2
    data_snapshot_hash: str = "default_snapshot"


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
    valuations_monthly: pd.DataFrame  # valuation_date, cum_nav, drawdown, gross_exposure, cash_weight
    valuations_daily: pd.DataFrame  # valuation_date, cum_nav, drawdown, gross_exposure, cash_weight
    trades: list[dict[str, Any]]
    error_message: str | None = None


def canonical_run_id(
    strategy_key: str,
    strategy_version: str,
    universe_id: str,
    start_date: str,
    end_date: str,
    top_k: int = 5,
    dropout_k: int = 2,
    fees: float = 0.001,
    slippage: float = 0.0005,
    data_snapshot_hash: str = "default_snapshot",
) -> str:
    """Deterministic hash identifier binding all strategy, model, and execution parameters."""
    seed = (
        f"{strategy_key}:{strategy_version}:{universe_id}:{start_date}:{end_date}:"
        f"{top_k}:{dropout_k}:{fees:.6f}:{slippage:.6f}:{data_snapshot_hash}"
    )
    return f"backtest-run:{sha256(seed.encode()).hexdigest()}"


class VectorBTBacktestEngine:
    """Unified single-track daily portfolio backtest engine."""

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
        top_k: int | None = None,
        dropout_k: int | None = None,
        data_snapshot_hash: str | None = None,
    ) -> BacktestResult:
        """Execute single-track daily portfolio simulation.

        - If close_daily is not provided, close_monthly is treated as the discrete market track.
        - Rebalancing occurs only on dates present in weights_monthly.
        - Non-rebalance days hold positions steady (Buy & Hold).
        - Month-end NAV is sampled directly from the daily mark-to-market series (max_dev == 0.0).
        """
        # 1. Validation
        if list(close_monthly.columns) != list(weights_monthly.columns):
            raise ValueError(f"Asset mismatch: {list(close_monthly.columns)} vs {list(weights_monthly.columns)}")
        if not close_monthly.index.is_monotonic_increasing:
            raise ValueError("Monthly index must be sorted")
        if not weights_monthly.index.is_monotonic_increasing:
            raise ValueError("Weights index must be sorted")

        eff_top_k = top_k if top_k is not None else self.config.top_k
        eff_dropout_k = dropout_k if dropout_k is not None else self.config.dropout_k
        eff_snapshot = data_snapshot_hash if data_snapshot_hash is not None else self.config.data_snapshot_hash

        # Determine daily simulation matrix and rebalancing cutoffs
        if close_daily is not None and not close_daily.empty:
            if list(close_daily.columns) != list(weights_monthly.columns):
                raise ValueError(
                    f"Daily columns mismatch: {list(close_daily.columns)} vs {list(weights_monthly.columns)}"
                )
            if not close_daily.index.is_monotonic_increasing:
                raise ValueError("Daily index must be sorted")
            sim_close = close_daily.copy()
        else:
            sim_close = close_monthly.copy()

        # Rebalance dates set
        rebalance_dates = set(pd.to_datetime(weights_monthly.index).date)
        sim_dates = pd.to_datetime(sim_close.index)

        start_date = str(sim_dates[0].date())
        end_date = str(sim_dates[-1].date())

        run_id = canonical_run_id(
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            universe_id=universe_id,
            start_date=start_date,
            end_date=end_date,
            top_k=eff_top_k,
            dropout_k=eff_dropout_k,
            fees=self.config.fees,
            slippage=self.config.slippage,
            data_snapshot_hash=eff_snapshot,
        )

        # 2. Run single-track simulation
        nav_series, dd_series, gross_exp_series, cash_wt_series, trades, total_turnover = self._simulate_single_track(
            sim_close, weights_monthly, rebalance_dates
        )

        # 3. Build daily valuations
        val_d_df = pd.DataFrame(
            {
                "valuation_date": [str(d.date()) for d in sim_dates],
                "cum_nav": nav_series.to_numpy(dtype=np.float64),
                "drawdown": dd_series.to_numpy(dtype=np.float64),
                "gross_exposure": gross_exp_series.to_numpy(dtype=np.float64),
                "cash_weight": cash_wt_series.to_numpy(dtype=np.float64),
            },
            index=sim_close.index,
        )

        # 4. Sample monthly valuations directly from daily valuations (Zero deviation identity!)
        # Align each monthly cutoff with its corresponding date in daily valuations
        monthly_dates = pd.to_datetime(close_monthly.index)
        m_indices = []
        for m_date in monthly_dates:
            target_d = m_date.date()
            matching_mask = [d.date() == target_d for d in sim_dates]
            if any(matching_mask):
                # Exact date match
                m_indices.append(np.where(matching_mask)[0][-1])
            else:
                # Latest available date prior to or at m_date
                prior_mask = [d.date() <= target_d for d in sim_dates]
                if any(prior_mask):
                    m_indices.append(np.where(prior_mask)[0][-1])
                else:
                    m_indices.append(0)

        sampled_monthly_val = val_d_df.iloc[m_indices].copy()
        sampled_monthly_val["valuation_date"] = [str(d.date()) for d in monthly_dates]
        val_m_df = sampled_monthly_val.reset_index(drop=True)

        # 5. Precompute metrics
        total_years_m = max((monthly_dates[-1] - monthly_dates[0]).days / 365.25, 0.1)
        final_nav_m = float(val_m_df["cum_nav"].iloc[-1])
        cagr_monthly = float((final_nav_m / 1.0) ** (1.0 / total_years_m) - 1.0)

        d_returns = nav_series.pct_change().dropna()
        vol_daily = float(d_returns.std() * np.sqrt(252)) if len(d_returns) > 1 else 0.0
        mean_ret = float(d_returns.mean() * 252) if len(d_returns) > 1 else 0.0
        sharpe_daily = float(mean_ret / vol_daily) if vol_daily > 1e-8 else 0.0
        max_dd_daily = float(dd_series.max())
        calmar_daily = float(cagr_monthly / max_dd_daily) if max_dd_daily > 1e-6 else 0.0
        turnover_monthly = total_turnover / max(len(monthly_dates) - 1, 1)

        metrics_payload = {
            "cagr_monthly": round(cagr_monthly, 6),
            "sharpe_daily": round(sharpe_daily, 4),
            "max_dd_daily": round(max_dd_daily, 6),
            "vol_daily": round(vol_daily, 6),
            "turnover_monthly": round(turnover_monthly, 6),
            "calmar_daily": round(calmar_daily, 4),
            "initial_cash": self.config.init_cash,
            "fees_bps": self.config.fees * 10000,
            "slippage_bps": self.config.slippage * 10000,
            "tie_out_max_deviation": 0.0,
        }

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
            turnover_monthly=turnover_monthly,
            calmar_daily=calmar_daily,
            metrics_payload=metrics_payload,
            valuations_monthly=val_m_df,
            valuations_daily=val_d_df.reset_index(drop=True),
            trades=trades,
        )

    def _simulate_single_track(
        self,
        close: pd.DataFrame,
        weights_monthly: pd.DataFrame,
        rebalance_dates: set[Any],
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, list[dict[str, Any]], float]:
        """Pure numpy/pandas deterministic single-track simulator.

        Features:
        - Daily mark-to-market valuation.
        - Rebalancing execution strictly on rebalance cutoff dates.
        - Non-rebalance days hold positions unchanged (Buy & Hold).
        - Last Known Price fallback on missing (NaN or <= 0) prices; strictly never liquidates at 0.
        """
        n_periods, n_assets = close.shape
        cash = float(self.config.init_cash)
        shares = np.zeros(n_assets, dtype=np.float64)
        last_known_prices = np.zeros(n_assets, dtype=np.float64)

        nav_values = np.zeros(n_periods, dtype=np.float64)
        gross_exposures = np.zeros(n_periods, dtype=np.float64)
        cash_weights = np.zeros(n_periods, dtype=np.float64)

        trades: list[dict[str, Any]] = []
        total_turnover = 0.0

        # Precompute rebalance weight rows mapping
        w_dates = [pd.to_datetime(d).date() for d in weights_monthly.index]
        w_dict = {w_dates[i]: weights_monthly.iloc[i].to_numpy(dtype=np.float64) for i in range(len(w_dates))}

        for t in range(n_periods):
            t_dt = pd.to_datetime(close.index[t]).date()
            p_t = close.iloc[t].to_numpy(dtype=np.float64)

            # Update last known prices for valid positive quotes
            valid_mask = ~np.isnan(p_t) & (p_t > 0)
            last_known_prices = np.where(valid_mask, p_t, last_known_prices)

            # Effective valuation price uses today's quote if valid, else fallback to last known price
            cur_p = np.where(valid_mask, p_t, last_known_prices)

            # Portfolio value before rebalance
            port_val = cash + np.sum(shares * cur_p)

            # Check if today is a rebalance cutoff date
            if t_dt in rebalance_dates and t_dt in w_dict:
                target_w = w_dict[t_dt]
                target_dollars = port_val * target_w

                for i in range(n_assets):
                    # If asset has no price quote today and no last known price, cannot trade
                    if cur_p[i] <= 0:
                        continue

                    # If quote is missing today (e.g. trading halt), DO NOT liquidate at 0! Keep position steady.
                    if not valid_mask[i]:
                        continue

                    target_s = target_dollars[i] / cur_p[i]
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
                                "trade_date": str(t_dt),
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

            # Mark to market post-rebalance / end-of-day valuation
            market_val = np.sum(shares * cur_p)
            port_val_post = cash + market_val

            nav_values[t] = port_val_post / self.config.init_cash
            gross_exposures[t] = market_val / max(port_val_post, 1e-6)
            cash_weights[t] = cash / max(port_val_post, 1e-6)

        nav_series = pd.Series(nav_values, index=close.index)
        cum_max = nav_series.cummax()
        dd_series: pd.Series = (cum_max - nav_series) / np.where(cum_max > 0, cum_max, 1.0)
        gross_exp_series = pd.Series(gross_exposures, index=close.index)
        cash_wt_series = pd.Series(cash_weights, index=close.index)

        return nav_series, dd_series, gross_exp_series, cash_wt_series, trades, float(total_turnover)


__all__ = [
    "BacktestEngineConfig",
    "BacktestResult",
    "VectorBTBacktestEngine",
    "canonical_run_id",
]
