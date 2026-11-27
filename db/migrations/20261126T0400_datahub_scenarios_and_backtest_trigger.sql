-- Add scenario outputs (M4 target_price/track_record_score, M5 virtual fundamentals)
-- and governed backtest trigger support to staging.pipeline_trigger_requests (#1742).
-- Guard every DDL statement via catalog checks so replays under boot lock_timeout take zero locks.

do $$
declare
    rel oid := to_regclass('mart.issuer_analyst_ratings');
begin
    if exists (select 1 from pg_class where oid = rel and relkind = 'r') then
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'target_price' and not attisdropped) then
            alter table mart.issuer_analyst_ratings add column target_price numeric;
        end if;
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'target_high' and not attisdropped) then
            alter table mart.issuer_analyst_ratings add column target_high numeric;
        end if;
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'target_low' and not attisdropped) then
            alter table mart.issuer_analyst_ratings add column target_low numeric;
        end if;
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'track_record_score' and not attisdropped) then
            alter table mart.issuer_analyst_ratings add column track_record_score numeric;
        end if;
    end if;
end $$;

do $$
declare
    rel oid := to_regclass('mart.fund_virtual_company');
begin
    if exists (select 1 from pg_class where oid = rel and relkind = 'r') then
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'virtual_revenue' and not attisdropped) then
            alter table mart.fund_virtual_company add column virtual_revenue numeric;
        end if;
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'virtual_gross_profit' and not attisdropped) then
            alter table mart.fund_virtual_company add column virtual_gross_profit numeric;
        end if;
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'virtual_employees' and not attisdropped) then
            alter table mart.fund_virtual_company add column virtual_employees numeric;
        end if;
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'virtual_gppe' and not attisdropped) then
            alter table mart.fund_virtual_company add column virtual_gppe numeric;
        end if;
    end if;
end $$;

do $$
declare
    rel oid := to_regclass('staging.pipeline_trigger_requests');
    cdef text;
begin
    if exists (select 1 from pg_class where oid = rel and relkind = 'r') then
        if not exists (select 1 from pg_attribute where attrelid = rel and attname = 'config_json' and not attisdropped) then
            alter table staging.pipeline_trigger_requests add column config_json jsonb default '{}'::jsonb not null;
        end if;
        select pg_get_constraintdef(c.oid) into cdef
        from pg_constraint c
        where c.conrelid = rel and c.conname = 'pipeline_trigger_requests_job_name_check';
        if cdef is null or position('nightly_backtest_pipeline' in cdef) = 0 then
            alter table staging.pipeline_trigger_requests drop constraint if exists pipeline_trigger_requests_job_name_check;
            alter table staging.pipeline_trigger_requests
                add constraint pipeline_trigger_requests_job_name_check
                check (job_name = any (array[
                    'topt_live_pipeline'::text,
                    'qqq_live_pipeline'::text,
                    'sp500_live_pipeline'::text,
                    'canary_live_pipeline'::text,
                    'nightly_backtest_pipeline'::text
                ]));
        end if;
    end if;
    if to_regprocedure('staging.validate_pipeline_trigger_update()') is not null
       and position('config_json' in pg_get_functiondef('staging.validate_pipeline_trigger_update()'::regprocedure)) = 0 then
        create or replace function staging.validate_pipeline_trigger_update()
         returns trigger
         language plpgsql
        as $function$
        begin
            if old.consumed_at is not null then
                raise check_violation using message = 'pipeline trigger request already consumed';
            end if;
            if new.request_id <> old.request_id
               or new.job_name <> old.job_name
               or new.executed_at <> old.executed_at
               or new.requested_by <> old.requested_by
               or new.dedupe_key <> old.dedupe_key
               or new.requested_at <> old.requested_at
               or new.force_fetch <> old.force_fetch
               or to_jsonb(new)->'config_json' is distinct from to_jsonb(old)->'config_json' then
                raise check_violation using message = 'pipeline trigger request fields are immutable';
            end if;
            return new;
        end;
        $function$;
    end if;
end $$;
