-- #1320: A universe roll retires the older version from mart.served_head.
--
-- Problem: 20261006T1020 keeps one served row per governed key. A universe roll adds a new
-- universe_id, for example universe:qqq-us-2026-06-30 -> universe:qqq-us-2026-09-30.
-- The old head never advances again. Past its limit it reads stale, for ever.
-- /api/health, deploy-freshness, Gate #54 and the nightly checks then report a frozen lane.
--
-- Rule: the family of a head is its universe_id without a trailing -YYYY-MM-DD.
-- A universe_id with no trailing date is its own family.
-- Per environment, family and factor, the view keeps the head that advanced last.
-- On equal advanced_at, the larger universe_id wins, then the larger universe_version.
-- So a family with one head keeps that head, also when it is stale or withheld.
-- An older version that advanced last (a rollback) is the live head.
-- No row is deleted. mart.current_pointer_head keeps every version.
--
-- The columns, their names and their order are the same as in 20261006T1020.
-- This is the last migration that defines the view, so it owns the shape and the comment.
-- 20261006T1020 creates the view only on a database that has none.
--
-- Boot-lock guard: `create or replace view` takes ACCESS EXCLUSIVE on the view.
-- The view is replaced only when its stored definition differs from the one below.
-- A temporary view with the same text gives the comparison through pg_get_viewdef.
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
from (
    select p.environment,
           p.universe_id,
           p.universe_version,
           p.factor_id,
           p.sequence,
           p.advanced_at,
           p.target_run_id,
           row_number() over (
               partition by p.environment,
                            regexp_replace(p.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''),
                            p.factor_id
               order by p.advanced_at desc, p.universe_id desc, p.universe_version desc
           ) as version_rank
    from mart.current_pointer_head p
) h
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
  and h.version_rank = 1
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
    '#1062, #1320: The live governed head of each universe family and factor, with its age, limit, label and availability. The one read point for consumers.';

-- `mart_readonly` and `app_ops_reader` keep select on the view. `create or replace view` keeps
-- the owner and the privileges. db/roles.sql grants them again after the migrations.
