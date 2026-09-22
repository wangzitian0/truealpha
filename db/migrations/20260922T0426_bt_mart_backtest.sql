-- #758 / #103: Materialized Mart tables for VectorBT Backtest Engine
-- Dual-resolution valuations (1M for 10y CAGR / 1D for 3y MaxDD & Sharpe),
-- run metadata with precomputed metrics (Rule 2: zero App-layer aggregation),
-- and execution trade ledger.

create table if not exists mart.backtest_runs (
    run_id           text primary key check (run_id ~ '^backtest-run:[0-9a-f]{64}$'),
    strategy_key     text not null,
    strategy_version text not null,
    universe_id      text not null,
    start_date       date not null,
    end_date         date not null,
    status           text not null check (status in ('pending', 'running', 'succeeded', 'failed')),
    -- Precomputed metrics (read by Web UI with zero aggregation)
    cagr_monthly     numeric,
    sharpe_daily     numeric,
    max_dd_daily     numeric,
    vol_daily        numeric,
    turnover_monthly numeric,
    calmar_daily     numeric,
    metrics_payload  jsonb not null default '{}'::jsonb,
    error_message    text,
    executed_at      timestamptz not null,
    created_at       timestamptz not null default now()
);

create table if not exists mart.backtest_valuations (
    run_id           text not null references mart.backtest_runs(run_id) on delete cascade,
    resolution       text not null check (resolution in ('1M', '1D')),
    valuation_date   date not null,
    cum_nav          numeric not null,
    drawdown         numeric not null,
    gross_exposure   numeric not null,
    cash_weight      numeric not null,
    primary key (run_id, resolution, valuation_date)
);

create table if not exists mart.backtest_trades (
    trade_id         bigint generated always as identity,
    run_id           text not null references mart.backtest_runs(run_id) on delete cascade,
    trade_date       date not null,
    symbol           text not null,
    side             text not null check (side in ('BUY', 'SELL')),
    shares           numeric not null,
    execution_price  numeric not null,
    trade_value      numeric not null,
    weight_before    numeric not null,
    weight_after     numeric not null,
    fee_paid         numeric not null default 0,
    primary key (run_id, trade_id)
);

do $$
begin
    if to_regclass('mart.idx_backtest_runs_strategy') is null then
        create index if not exists idx_backtest_runs_strategy on mart.backtest_runs(strategy_key, executed_at desc);
    end if;
end
$$;

do $$
begin
    if to_regclass('mart.idx_backtest_val_run_res') is null then
        create index if not exists idx_backtest_val_run_res on mart.backtest_valuations(run_id, resolution, valuation_date);
    end if;
end
$$;

do $$
begin
    if to_regclass('mart.idx_backtest_trades_run') is null then
        create index if not exists idx_backtest_trades_run on mart.backtest_trades(run_id, trade_date);
    end if;
end
$$;
