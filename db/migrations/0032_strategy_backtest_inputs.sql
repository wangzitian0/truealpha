-- 0032: Strategy-lane provenance-neutral factor inputs, per (issuer, cutoff) (#395).
--
-- The BacktestDataGateway reads the base strategy's factor inputs from here instead
-- of the checked-in golden JSON, so the replay/backtest runs on captured data through
-- a real data boundary. Rows are the provenance-neutral projection the strategy
-- consumes: subject, cutoff, one input key, its value + confidence, and the time it
-- was knowable (must be at or before the cutoff -- no look-ahead). No source, vendor,
-- raw ref, or extractor metadata (init.md: factor inputs are provenance-neutral).

create table if not exists staging.strategy_backtest_inputs (
    id                bigint generated always as identity primary key,
    issuer_id         text not null,
    cutoff_at         timestamptz not null,
    input_key         text not null
        check (input_key in ('gross_profit', 'total_assets', 'headcount', 'revenue',
                             'shares_outstanding', 'last_close')),
    value             numeric not null,
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    knowable_at       timestamptz not null,
    recorded_at       timestamptz not null default now(),
    -- point-in-time: an input can only inform a decision if it was knowable at or
    -- before that decision's cutoff.
    constraint strategy_backtest_inputs_pit check (knowable_at <= cutoff_at),
    -- one value per (issuer, cutoff, input key); a restatement lands a new row and
    -- supersedes by recorded_at, it never overwrites.
    unique (issuer_id, cutoff_at, input_key, recorded_at)
);

create index if not exists idx_strategy_backtest_inputs_asof
    on staging.strategy_backtest_inputs (cutoff_at, issuer_id, input_key, recorded_at desc);
