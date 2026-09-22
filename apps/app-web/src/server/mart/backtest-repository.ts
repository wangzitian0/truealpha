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
  cagr_monthly: string | null;
  sharpe_daily: string | null;
  max_dd_daily: string | null;
  vol_daily: string | null;
  turnover_monthly: string | null;
  calmar_daily: string | null;
  metrics_payload: Record<string, unknown>;
  error_message: string | null;
  executed_at: string;
  created_at: string;
}

export interface BacktestValuationRecord {
  run_id: string;
  resolution: "1M" | "1D";
  valuation_date: string;
  cum_nav: string | null;
  drawdown: string | null;
  gross_exposure: string | null;
  cash_weight: string | null;
}

export interface BacktestTradeRecord {
  trade_id: number;
  run_id: string;
  trade_date: string;
  symbol: string;
  side: "BUY" | "SELL";
  shares: string | null;
  execution_price: string | null;
  trade_value: string | null;
  weight_before: string | null;
  weight_after: string | null;
  fee_paid: string | null;
}

/** `numeric` comes back from node-pg as a precision-preserving string; keep it
 * verbatim (never coerce through a JS number). */
function decimalString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  return String(value);
}

function mapBacktestRun(row: Record<string, unknown>): BacktestRunRecord {
  return {
    run_id: String(row.run_id),
    strategy_key: String(row.strategy_key),
    strategy_version: String(row.strategy_version),
    universe_id: String(row.universe_id),
    start_date: String(row.start_date),
    end_date: String(row.end_date),
    status: row.status as BacktestRunRecord["status"],
    cagr_monthly: decimalString(row.cagr_monthly),
    sharpe_daily: decimalString(row.sharpe_daily),
    max_dd_daily: decimalString(row.max_dd_daily),
    vol_daily: decimalString(row.vol_daily),
    turnover_monthly: decimalString(row.turnover_monthly),
    calmar_daily: decimalString(row.calmar_daily),
    metrics_payload: (row.metrics_payload as Record<string, unknown>) ?? {},
    error_message: row.error_message ? String(row.error_message) : null,
    executed_at: String(row.executed_at),
    created_at: String(row.created_at),
  };
}

function mapBacktestValuation(row: Record<string, unknown>): BacktestValuationRecord {
  return {
    run_id: String(row.run_id),
    resolution: row.resolution as "1M" | "1D",
    valuation_date: String(row.valuation_date),
    cum_nav: decimalString(row.cum_nav),
    drawdown: decimalString(row.drawdown),
    gross_exposure: decimalString(row.gross_exposure),
    cash_weight: decimalString(row.cash_weight),
  };
}

function mapBacktestTrade(row: Record<string, unknown>): BacktestTradeRecord {
  return {
    trade_id: Number(row.trade_id),
    run_id: String(row.run_id),
    trade_date: String(row.trade_date),
    symbol: String(row.symbol),
    side: row.side as "BUY" | "SELL",
    shares: decimalString(row.shares),
    execution_price: decimalString(row.execution_price),
    trade_value: decimalString(row.trade_value),
    weight_before: decimalString(row.weight_before),
    weight_after: decimalString(row.weight_after),
    fee_paid: decimalString(row.fee_paid),
  };
}

export async function listBacktestRuns(limit: number = 20): Promise<BacktestRunRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select run_id, strategy_key, strategy_version, universe_id,
              start_date::text, end_date::text, status,
              cagr_monthly, sharpe_daily, max_dd_daily,
              vol_daily, turnover_monthly, calmar_daily,
              metrics_payload, error_message, executed_at::text, created_at::text
       from mart.backtest_runs
       order by created_at desc
       limit $1`,
      [limit]
    );
    return result.rows.map(mapBacktestRun);
  });
}

export async function getBacktestRun(runId: string): Promise<BacktestRunRecord | null> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select run_id, strategy_key, strategy_version, universe_id,
              start_date::text, end_date::text, status,
              cagr_monthly, sharpe_daily, max_dd_daily,
              vol_daily, turnover_monthly, calmar_daily,
              metrics_payload, error_message, executed_at::text, created_at::text
       from mart.backtest_runs
       where run_id = $1`,
      [runId]
    );
    if (result.rows.length === 0) return null;
    return mapBacktestRun(result.rows[0]);
  });
}

export async function getBacktestValuations(
  runId: string,
  resolution?: "1M" | "1D"
): Promise<BacktestValuationRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const query = resolution
      ? `select run_id, resolution, valuation_date::text,
                cum_nav, drawdown, gross_exposure, cash_weight
         from mart.backtest_valuations
         where run_id = $1 and resolution = $2
         order by valuation_date asc`
      : `select run_id, resolution, valuation_date::text,
                cum_nav, drawdown, gross_exposure, cash_weight
         from mart.backtest_valuations
         where run_id = $1
         order by valuation_date asc`;

    const params = resolution ? [runId, resolution] : [runId];
    const result = await client.query(query, params);
    return result.rows.map(mapBacktestValuation);
  });
}

export async function getBacktestTrades(
  runId: string,
  limit: number = 100
): Promise<BacktestTradeRecord[]> {
  return withMartReadonly(async (client: PoolClient) => {
    const result = await client.query(
      `select trade_id, run_id, trade_date::text, symbol, side,
              shares, execution_price, trade_value,
              weight_before, weight_after, fee_paid
       from mart.backtest_trades
       where run_id = $1
       order by trade_date asc, trade_id asc
       limit $2`,
      [runId, limit]
    );
    return result.rows.map(mapBacktestTrade);
  });
}
