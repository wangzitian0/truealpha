-- #1346 / #1295: Database-level schedule predicate for governed heads and operator read grant
-- on mart.nightly_verdicts so /admin/datahub can display in-environment check verdicts.
--
-- Mirrors `is_head_scheduled` in `apps/data-engine/src/data_engine/lanes/capture.py`:
--   * `sp500` schedule is stopped in every environment;
--   * `qqq` schedule runs in production only (manual in staging, #1310 / #1256);
--   * `topt` and `canary` schedules run in both staging and production;
--   * unrecognised environments or universe families fail closed (`true`).
create or replace function mart.is_head_scheduled(
    p_universe_id text,
    p_environment text
)
returns boolean
language sql
immutable
as $$
    select case
        when lower(trim(coalesce(p_environment, ''))) not in ('staging', 'production', 'prod') then true
        when coalesce(p_universe_id, '') like 'universe:sp500-%' then false
        when lower(trim(coalesce(p_environment, ''))) = 'staging'
             and coalesce(p_universe_id, '') like 'universe:qqq-%' then false
        else true
    end
$$;

comment on function mart.is_head_scheduled(text, text) is
    '#1346, #1256: Whether a governed head universe has an active schedule in the given environment.';

-- Grant app_ops_reader SELECT on mart.nightly_verdicts (#876) so /admin/datahub renders
-- the in-environment check verdicts directly. Guarded by has_table_privilege so a replay
-- on a settled database takes no lock.
do $$
begin
    if exists (select from pg_roles where rolname = 'app_ops_reader')
       and to_regclass('mart.nightly_verdicts') is not null
       and not has_table_privilege('app_ops_reader', 'mart.nightly_verdicts', 'SELECT')
    then
        grant select on mart.nightly_verdicts to app_ops_reader;
    end if;
end;
$$;
