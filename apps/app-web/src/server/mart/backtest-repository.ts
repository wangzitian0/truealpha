/** Postgres-backed read-only repository for `/research/backtest` (#104). */

import type { PoolClient } from "pg";
import { withMartReadonly } from "./db";

export const SERVED_HEAD_SQL = "select run_id, freshness, availability from mart.served_head";

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
  definition_content_sha256: string | null;
  price_basis: string | null;
  claim_ceiling: string | null;
  benchmark_rule: string | null;
  benchmark_cagr: number | null;
  benchmark_max_dd: number | null;
}

export interface BacktestValuationRecord {
  run_id: string;
  resolution: "1M" | "1D";
  valuation_date: string;
  cum_nav: number;
  drawdown: number;
  gross_exposure: number;
  cash_weight: number;
  benchmark_cum_nav: number | null;
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

const RUN_SELECT = `select run_id, strategy_key, strategy_version, universe_id,
  start_date::text, end_date::text, status,
  cagr_monthly::float, sharpe_daily::float, max_dd_daily::float,
  vol_daily::float, turnover_monthly::float, calmar_daily::float,
  metrics_payload, error_message, executed_at::text, created_at::text,
  definition_content_sha256, price_basis, claim_ceiling,
  benchmark_rule, benchmark_cagr::float, benchmark_max_dd::float
from mart.backtest_runs`;

export async function listBacktestRuns(limit = 20): Promise<BacktestRunRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(`${RUN_SELECT} order by created_at desc limit $1`, [limit]);
    return result.rows as BacktestRunRecord[];
  });
}

export async function getBacktestRun(runId: string): Promise<BacktestRunRecord | null> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(`${RUN_SELECT} where run_id = $1`, [runId]);
    return result.rows.length === 0 ? null : (result.rows[0] as BacktestRunRecord);
  });
}

export async function getBacktestValuations(runId: string, resolution?: "1M" | "1D"): Promise<BacktestValuationRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const where = resolution ? "where run_id = $1 and resolution = $2" : "where run_id = $1";
    const result = await client.query(
      `select run_id, resolution, valuation_date::text,
              cum_nav::float, drawdown::float, gross_exposure::float, cash_weight::float,
              benchmark_cum_nav::float
       from mart.backtest_valuations
       ${where}
       order by valuation_date asc`,
      resolution ? [runId, resolution] : [runId],
    );
    return result.rows as BacktestValuationRecord[];
  });
}

export async function getBacktestTrades(runId: string, limit = 100): Promise<BacktestTradeRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select trade_id, run_id, trade_date::text, symbol, side,
              shares::float, execution_price::float, trade_value::float,
              weight_before::float, weight_after::float, fee_paid::float
       from mart.backtest_trades
       where run_id = $1
       order by trade_date asc, trade_id asc
       limit $2`,
      [runId, limit],
    );
    return result.rows as BacktestTradeRecord[];
  });
}
