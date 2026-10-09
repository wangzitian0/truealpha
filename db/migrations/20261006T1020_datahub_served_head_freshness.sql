-- #1062: The served head ages at read time.
--
-- Problem: a head stamps `freshness = 'fresh'` when it is published and never again.
-- A head that stopped advancing 13 days ago still reads fresh.
--
-- This file adds five objects. It changes no existing table, view or column.
--   mart.freshness_limit          the limits, in hours, one row per cadence
--   mart.served_artifact          the registry: which cadence each served artifact has
--   mart.head_freshness           the one function that turns a refresh time into an age and a label
--   mart.served_head              the one view consumers read instead of mart.current_pointer_head
--   mart.served_head_environments the environments that hold heads, and whether this database serves them
--
-- Owner limits (2026-10-06): 3 days for daily data and 14 days for weekly data.
-- Quarterly data is checked monthly, with a limit of 30 days. No limit is longer than 30 days.
-- Past its limit, a value is served with freshness 'stale'. Past 30 days it is withheld.
--
-- Git is the authority for the rows that the seed below lists. A boot replays the seed. The
-- replay restores any listed value that was changed by hand. A row that is removed from the
-- seed stays in deployed databases. Only a migration that deletes it removes it.
-- To change a listed row, edit the seed in this file in a reviewed change. Do not add a second
-- migration for the same row: the two files would overwrite each other on every boot.
--
-- Boot-lock rules (libs/runtime/tests/test_migration_boot_locks.py): on a settled database,
-- every statement here takes only three lock modes on a mart relation.
-- They are ACCESS SHARE, ROW EXCLUSIVE and SHARE UPDATE EXCLUSIVE. COMMENT ON takes the last one.
-- A seed row is updated only when it differs. Postgres locks the conflicting row before it
-- tests the WHERE clause. That lock is a row lock, not a table lock.
-- A view is replaced only when its definition differs.

-- 1. The limits.
create table if not exists mart.freshness_limit (
    limit_key text primary key,
    -- 720 hours are 30 days. The check makes "no limit is longer than 30 days" a structure.
    hours integer not null check (hours between 1 and 720)
);

comment on table mart.freshness_limit is
    '#1062: Read-time age limits in hours, one row per cadence. Seeded by git. A replay restores each listed value.';

insert into mart.freshness_limit (limit_key, hours)
values
    ('daily', 72),
    ('weekly', 336),
    ('quarterly', 720),
    ('withhold', 720)
on conflict (limit_key) do update
    set hours = excluded.hours
    where mart.freshness_limit.hours is distinct from excluded.hours;

-- 2. The registry: one row per served artifact, each with one cadence.
-- `wired` is a readiness flag. It is true when an age source is wired for the artifact.
-- Today the only age source is the pointer's `advanced_at`, reached through `universe_like`.
-- A false row has no wired age source yet. Other tables hold a time, for example
-- `staging.accepted_rulesets.advanced_at` and `mart.nightly_verdicts.ran_at`. No row uses them.
-- Nothing in the database reads `wired`, and nothing enforces it.
-- `served_to` names the audience: consumers (Web App, MCP, chat), operators (admin pages
-- and deploy checks) or internal.
-- Each schedule appears in exactly one row. The registry test checks that over this seed.
-- A unique constraint on `schedule_name` would fail a boot after a rename. The upsert below
-- names `artifact_key` as its only arbiter, so a renamed key with the same schedule collides.
create table if not exists mart.served_artifact (
    artifact_key text primary key,
    lane text not null,
    schedule_name text,
    universe_like text,
    family text not null references mart.freshness_limit (limit_key),
    served_to text not null,
    wired boolean not null,
    constraint served_artifact_family_is_a_cadence check (family <> 'withhold')
);

comment on table mart.served_artifact is
    '#1062: Registry of served artifacts. Each row names the lane that refreshes it and its cadence family.';

-- One row per line. The data-engine registry test reads these lines from this file.
insert into mart.served_artifact (artifact_key, lane, schedule_name, universe_like, family, served_to, wired)
values
    ('head:topt', 'capture', 'topt_live_schedule', 'universe:topt-%', 'daily', 'consumers', true),
    ('head:qqq', 'capture', 'qqq_live_schedule', 'universe:qqq-%', 'daily', 'consumers', true),
    ('head:canary', 'capture', 'canary_daily_schedule', 'universe:canary-%', 'daily', 'operators', true),
    ('head-reports', 'standards', 'head_reports_schedule', null, 'daily', 'operators', false),
    ('standards-backfill', 'standards', 'standard_backfill_schedule', null, 'weekly', 'consumers', false),
    ('standards-history', 'standards', 'standard_history_schedule', null, 'weekly', 'consumers', false),
    ('universe-refresh', 'universe_refresh', 'universe_refresh_schedule', null, 'weekly', 'consumers', false),
    -- market-data: the lane runs on weekdays only (cron `15 21 * * 1-5`).
    -- After a long weekend the age can pass 72 hours. Decide the family again when it is wired.
    ('market-data', 'market_data', 'market_data_refresh_schedule', null, 'daily', 'internal', false),
    -- strategy-history: the lane runs on Sundays (cron `7 10 * * 0`) and reads stored bytes only.
    ('strategy-history', 'strategy_history', 'strategy_history_schedule', null, 'weekly', 'internal', false),
    -- backtest: the lane runs on weekdays (cron `30 21 * * 1-5`) and simulates daily/monthly portfolios.
    ('nightly-backtest', 'backtest', 'nightly_backtest_schedule', null, 'daily', 'internal', false),
    ('output-invariants', 'quality', 'output_invariants_schedule', null, 'daily', 'operators', false),
    ('datahub-confidence-report', 'quality', 'datahub_confidence_report_schedule', null, 'daily', 'operators', false),
    ('model-key-health', 'quality', 'model_key_health_schedule', null, 'daily', 'operators', false),
    ('release-fetch-proof', 'quality', 'release_fetch_proof_schedule', null, 'daily', 'operators', false),
    ('entity-identity', 'entity_identity', null, null, 'weekly', 'consumers', false),
    ('triggers', 'triggers', null, null, 'daily', 'internal', false),
    ('facts:filing-derived', 'capture', null, null, 'quarterly', 'consumers', false)
on conflict (artifact_key) do update
    set lane = excluded.lane,
        schedule_name = excluded.schedule_name,
        universe_like = excluded.universe_like,
        family = excluded.family,
        served_to = excluded.served_to,
        wired = excluded.wired
    where (mart.served_artifact.lane, mart.served_artifact.schedule_name, mart.served_artifact.universe_like,
           mart.served_artifact.family, mart.served_artifact.served_to, mart.served_artifact.wired)
          is distinct from
          (excluded.lane, excluded.schedule_name, excluded.universe_like,
           excluded.family, excluded.served_to, excluded.wired);

-- 3. The one definition of age and label.
-- Inputs: the time of the last good refresh, the cadence family and the clock.
-- A family that is null or unknown gets the strictest limit. No limit is longer than the
-- `withhold` limit. A null refresh time fails closed: unavailable.
-- Equality with a limit is fresh: only an age above the limit is stale.
-- The label of a withheld row is 'stale', because the age is above the family limit.
create or replace function mart.head_freshness(
    p_refreshed_at timestamptz,
    p_family text,
    p_as_of timestamptz default now()
)
returns table (
    age_hours numeric,
    limit_hours integer,
    freshness text,
    availability text,
    staleness_reason text
)
language sql
stable
as $$
    with aged as (
        -- A refresh time in the future counts as age 0. A null refresh time has no age.
        select case
                   when p_refreshed_at is null then null
                   else greatest(0, extract(epoch from p_as_of - p_refreshed_at)::numeric / 3600)
               end as hours
    ),
    cap as (
        select (select l.hours from mart.freshness_limit l where l.limit_key = 'withhold') as hours
    ),
    lim as (
        select least(
                   coalesce(
                       (select l.hours from mart.freshness_limit l
                         where l.limit_key = p_family and l.limit_key <> 'withhold'),
                       (select min(l.hours) from mart.freshness_limit l where l.limit_key <> 'withhold')),
                   (select hours from cap)) as hours
    ),
    binding as (
        -- The limit that the age passed. The withhold limit wins when both are passed.
        select case
                   when p_refreshed_at is null then null
                   when aged.hours > cap.hours then cap.hours
                   when aged.hours > lim.hours then lim.hours
               end as hours
        from aged, cap, lim
    )
    select aged.hours as age_hours,
           lim.hours as limit_hours,
           case
               when p_refreshed_at is null then 'unknown'
               when lim.hours is null or aged.hours > lim.hours then 'stale'
               else 'fresh'
           end as freshness,
           case
               when p_refreshed_at is not null and aged.hours <= cap.hours then 'available'
               else 'unavailable'
           end as availability,
           case
               when p_refreshed_at is null then 'refresh_time_unknown'
               when binding.hours is null then null
               when binding.hours % 24 = 0 then 'older_than_' || (binding.hours / 24) || 'd'
               else 'older_than_' || binding.hours || 'h'
           end as staleness_reason
    from aged, cap, lim, binding
$$;

comment on function mart.head_freshness(timestamptz, text, timestamptz) is
    '#1062: Age in hours, limit, label, availability and reason for one refresh time and cadence family.';

-- 4. The one view consumers read.
-- Boot-lock guard: `create or replace view` takes ACCESS EXCLUSIVE on the view, which
-- queues behind every open reader. Replace it only when the definition differs; the
-- comparison normalizes both sides through pg_get_viewdef, so no literal can drift.
--
-- Rows: the newest head per governed key, in this database's own environment.
-- `head_run_id` is always set. `run_id` is null when the head is withheld.
-- A head that matches no registry row gets the strictest limit.
-- The lateral join picks one registry row per head, so a head never repeats.
do $$
declare
    wanted constant text := $view$
select h.environment,
       h.universe_id,
       h.universe_version,
       h.factor_id,
       h.sequence,
       h.advanced_at,
       h.target_run_id as head_run_id,
       case when f.availability = 'unavailable' then null else h.target_run_id end as run_id,
       a.artifact_key,
       a.family,
       f.age_hours,
       f.limit_hours,
       f.freshness,
       f.availability,
       f.staleness_reason
from mart.current_pointer_head h
left join lateral (
    select s.artifact_key, s.family
    from mart.served_artifact s
    join mart.freshness_limit l on l.limit_key = s.family
    where s.universe_like is not null
      and h.universe_id like s.universe_like
    order by l.hours, s.artifact_key
    limit 1
) a on true
cross join lateral mart.head_freshness(h.advanced_at, a.family) f
where h.environment = (select environment from mart.environment_identity)
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.served_head') is null
       or pg_get_viewdef(to_regclass('mart.served_head'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.served_head as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.served_head is
    '#1062: The newest governed head per key with its age, limit, label and availability. The one read point for consumers.';

-- 5. The environments that hold heads.
-- `mart.served_head` shows only the heads of this database's own environment. A database can
-- hold heads of other environments, or have no environment identity row. Then the view is
-- empty although heads exist. A reader that sees no rows needs to tell that case apart from a
-- database with no head at all. This view says which environments hold heads and which one
-- this database serves. It holds counts only and no run id.
do $$
declare
    wanted constant text := $view$
select h.environment,
       count(*) as heads,
       h.environment is not distinct from (select environment from mart.environment_identity) as served
from mart.current_pointer_head h
group by h.environment
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.served_head_environments') is null
       or pg_get_viewdef(to_regclass('mart.served_head_environments'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.served_head_environments as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.served_head_environments is
    '#1062: Per environment, the number of governed heads and whether this database serves them.';

-- `mart_readonly` and `app_ops_reader` receive select on these relations from db/roles.sql,
-- which runs after migrations. No grant here: the roles do not exist yet during a fresh pass.
