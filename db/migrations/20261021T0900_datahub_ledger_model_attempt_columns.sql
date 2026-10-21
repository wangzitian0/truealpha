-- #1444: staging.api_call_ledger records each model attempt with its provider code and the
-- request parameters that matter for the model plan.
--
--   attempt         the 1-based attempt number of one logical model call. A call that the
--                   retry policy repeats writes one row per attempt. Null for other sources.
--   provider_code   the code in the error body of the provider (for example '1302'). Null when
--                   the provider sent none.
--   request_params  the request parameters without any secret: model, thinking type sent,
--                   max_tokens, temperature, response format. Null for other sources.
--
-- Backward compatible and replay-safe: nullable adds, guarded so a replay on a settled table
-- takes no ACCESS EXCLUSIVE lock (boot-lock guard, 2026-09-17).

do $$
begin
    if exists (
        select 1 from unnest(array['attempt', 'provider_code', 'request_params']) as wanted(column_name)
        where not exists (
            select 1 from pg_attribute
            where attrelid = 'staging.api_call_ledger'::regclass and attname = wanted.column_name and not attisdropped
        )
    ) then
        alter table staging.api_call_ledger
            add column if not exists attempt        smallint,
            add column if not exists provider_code  text,
            add column if not exists request_params jsonb;
    end if;
end
$$;

comment on column staging.api_call_ledger.attempt is
    '#1444: 1-based attempt number of one logical model call; null for sources without retry.';
comment on column staging.api_call_ledger.provider_code is
    '#1444: provider error code from the response body (for example 1302); null when absent.';
comment on column staging.api_call_ledger.request_params is
    '#1444: request parameters without secrets (model, thinking type, max_tokens); null when not recorded.';
