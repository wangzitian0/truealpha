-- #1444: staging.api_call_ledger records the output tokens of a model attempt, and the reasoning
-- tokens when the response reports them, so the effect of a decoding version can be read from
-- the ledger (mean and p95 of duration_ms, failure rate, tokens per attempt).
--
--   completion_tokens  usage.completion_tokens of the response; null when the attempt failed.
--   reasoning_tokens   usage.completion_tokens_details.reasoning_tokens; null when not reported.
--
-- Backward compatible and replay-safe: nullable adds, guarded so a replay on a settled table
-- takes no ACCESS EXCLUSIVE lock (boot-lock guard, 2026-09-17).

do $$
begin
    if exists (
        select 1 from unnest(array['completion_tokens', 'reasoning_tokens']) as wanted(column_name)
        where not exists (
            select 1 from pg_attribute
            where attrelid = 'staging.api_call_ledger'::regclass and attname = wanted.column_name and not attisdropped
        )
    ) then
        alter table staging.api_call_ledger
            add column if not exists completion_tokens integer,
            add column if not exists reasoning_tokens  integer;
    end if;
end
$$;

comment on column staging.api_call_ledger.completion_tokens is
    '#1444: output tokens of the response, reasoning included; null when the attempt failed.';
comment on column staging.api_call_ledger.reasoning_tokens is
    '#1444: reasoning tokens when the response reports them; null otherwise.';
