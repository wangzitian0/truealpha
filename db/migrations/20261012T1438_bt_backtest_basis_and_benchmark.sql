-- #758 (M3): a backtest run states what it can claim and how it compares.
--
-- The nightly lane backtests large_model_value_v0 on unadjusted bars with split events
-- derived from the two stored price series. No dividend source exists, so a run is a price
-- return. Each run records:
--   definition_content_sha256  the strategy definition the run evaluated
--   price_basis                how returns were priced ('unadjusted_open_explicit_splits')
--   claim_ceiling              the strongest claim the run supports ('price_return_no_dividends')
--   benchmark_rule             the benchmark the run computed beside the strategy
--   benchmark_cagr, benchmark_max_dd   the benchmark's statistics on the same window
-- Each valuation row also carries benchmark_cum_nav on the same session.
--
-- Additive only. A succeeded run written after this migration must declare its basis, its
-- ceiling and its definition; the NOT VALID constraint leaves rows written before it alone.

alter table mart.backtest_runs
    add column if not exists definition_content_sha256 text,
    add column if not exists price_basis text,
    add column if not exists claim_ceiling text,
    add column if not exists benchmark_rule text,
    add column if not exists benchmark_cagr numeric,
    add column if not exists benchmark_max_dd numeric;

alter table mart.backtest_valuations
    add column if not exists benchmark_cum_nav numeric;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'backtest_runs_definition_sha256_shape'
          and conrelid = 'mart.backtest_runs'::regclass
    ) then
        alter table mart.backtest_runs
            add constraint backtest_runs_definition_sha256_shape
            check (definition_content_sha256 is null or definition_content_sha256 ~ '^[0-9a-f]{64}$');
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'backtest_runs_succeeded_declares_basis'
          and conrelid = 'mart.backtest_runs'::regclass
    ) then
        alter table mart.backtest_runs
            add constraint backtest_runs_succeeded_declares_basis
            check (
                status <> 'succeeded'
                or (
                    definition_content_sha256 is not null
                    and price_basis is not null
                    and claim_ceiling is not null
                    and benchmark_rule is not null
                )
            ) not valid;
    end if;
end
$$;
