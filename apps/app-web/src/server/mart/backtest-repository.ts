/**
 * #104: Postgres-backed repository for /research/backtest.
 * Reads mart.backtest_runs, mart.backtest_valuations, and mart.backtest_trades
 * through the mart_readonly role via withMartReadonly.
 *
 * Rule 2 compliance: Pure SELECT queries only. Precomputed metrics are read directly
 * from the mart tables without SQL aggregation (no sum, avg, over partition).
 * Server-only; never import into client components.
 */

import type { PoolClient } from "pg";
import { withMartReadonly } from "./db";

export interface BacktestRunRecord {
  run_id: string;
  strategy_key: string;
  strategy_version: string;
  universe_id: string;
  start_date: string;
  end_date: string;
  status: "pending" | "running" | "succeeded" | "failed";
  cagr_monthly: number | null;
  sharpe_daily: number | null;
  max_dd_daily: number | null;
  vol_daily: number | null;
  turnover_monthly: number | null;
  calmar_daily: number | null;
  metrics_payload: Record<string, unknown>;
  error_message: string | null;
  executed_at: string;
  created_at: string;
}

export interface BacktestValuationRecord {
  run_id: string;
  resolution: "1M" | "1D";
  valuation_date: string;
  cum_nav: number;
  drawdown: number;
  gross_exposure: number;
  cash_weight: number;
}

export interface BacktestTradeRecord {
  trade_id: number;
  run_id: string;
  trade_date: string;
  symbol: string;
  side: "BUY" | "SELL";
  shares: number;
  execution_price: number;
  trade_value: number;
  weight_before: number;
  weight_after: number;
  fee_paid: number;
}

export async function listBacktestRuns(limit: number = 20): Promise<BacktestRunRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select run_id, strategy_key, strategy_version, universe_id,
              start_date::text, end_date::text, status,
              cagr_monthly::float, sharpe_daily::float, max_dd_daily::float,
              vol_daily::float, turnover_monthly::float, calmar_daily::float,
              metrics_payload, error_message, executed_at::text, created_at::text
       from mart.backtest_runs
       order by created_at desc
       limit $1`,
      [limit]
    );
    return result.rows as BacktestRunRecord[];
  });
}

export async function getBacktestRun(runId: string): Promise<BacktestRunRecord | null> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select run_id, strategy_key, strategy_version, universe_id,
              start_date::text, end_date::text, status,
              cagr_monthly::float, sharpe_daily::float, max_dd_daily::float,
              vol_daily::float, turnover_monthly::float, calmar_daily::float,
              metrics_payload, error_message, executed_at::text, created_at::text
       from mart.backtest_runs
       where run_id = $1`,
      [runId]
    );
    if (result.rows.length === 0) return null;
    return result.rows[0] as BacktestRunRecord;
  });
}

export async function getBacktestValuations(
  runId: string,
  resolution?: "1M" | "1D"
): Promise<BacktestValuationRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const query = resolution
      ? `select run_id, resolution, valuation_date::text,
                cum_nav::float, drawdown::float, gross_exposure::float, cash_weight::float
         from mart.backtest_valuations
         where run_id = $1 and resolution = $2
         order by valuation_date asc`
      : `select run_id, resolution, valuation_date::text,
                cum_nav::float, drawdown::float, gross_exposure::float, cash_weight::float
         from mart.backtest_valuations
         where run_id = $1
         order by valuation_date asc`;

    const params = resolution ? [runId, resolution] : [runId];
    const result = await client.query(query, params);
    return result.rows as BacktestValuationRecord[];
  });
}

export async function getBacktestTrades(
  runId: string,
  limit: number = 100
): Promise<BacktestTradeRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select trade_id, run_id, trade_date::text, symbol, side,
              shares::float, execution_price::float, trade_value::float,
              weight_before::float, weight_after::float, fee_paid::float
       from mart.backtest_trades
       where run_id = $1
       order by trade_date asc, trade_id asc
       limit $2`,
      [runId, limit]
    );
    return result.rows as BacktestTradeRecord[];
  });
}
