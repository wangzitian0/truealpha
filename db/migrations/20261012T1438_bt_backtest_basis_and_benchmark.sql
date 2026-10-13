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
--
-- Replayed on every boot (2026-09-17 lock incident). `alter table` takes ACCESS EXCLUSIVE
-- even when `if not exists` makes it a no-op, so each change runs only when the catalog
-- shows it is absent. A replay of this file only reads the catalog and takes no table lock.
do $$
declare
    wanted record;
begin
    for wanted in
        select *
          from (values
              ('backtest_runs', 'definition_content_sha256', 'text'),
              ('backtest_runs', 'price_basis', 'text'),
              ('backtest_runs', 'claim_ceiling', 'text'),
              ('backtest_runs', 'benchmark_rule', 'text'),
              ('backtest_runs', 'benchmark_cagr', 'numeric'),
              ('backtest_runs', 'benchmark_max_dd', 'numeric'),
              ('backtest_valuations', 'benchmark_cum_nav', 'numeric')
          ) as columns (table_name, column_name, column_type)
    loop
        if not exists (
            select 1
              from pg_attribute as a
              join pg_class as c on c.oid = a.attrelid
              join pg_namespace as n on n.oid = c.relnamespace
             where n.nspname = 'mart'
               and c.relname = wanted.table_name
               and a.attname = wanted.column_name
               and not a.attisdropped
        ) then
            execute format(
                'alter table mart.%I add column %I %s',
                wanted.table_name, wanted.column_name, wanted.column_type
            );
        end if;
    end loop;

    if not exists (
        select 1
          from pg_constraint as k
          join pg_class as c on c.oid = k.conrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = 'mart'
           and c.relname = 'backtest_runs'
           and k.conname = 'backtest_runs_definition_sha256_shape'
    ) then
        alter table mart.backtest_runs
            add constraint backtest_runs_definition_sha256_shape
            check (definition_content_sha256 is null or definition_content_sha256 ~ '^[0-9a-f]{64}$');
    end if;

    if not exists (
        select 1
          from pg_constraint as k
          join pg_class as c on c.oid = k.conrelid
          join pg_namespace as n on n.oid = c.relnamespace
         where n.nspname = 'mart'
           and c.relname = 'backtest_runs'
           and k.conname = 'backtest_runs_succeeded_declares_basis'
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
