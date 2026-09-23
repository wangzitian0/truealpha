-- #756: Environment identity SSOT.
--
-- The governed pointer's environment column was hardcoded to 'production' even on staging;
-- every consumer hardcoded the same literal to resolve a head.
--
-- This migration establishes mart.environment_identity as the single authoritative source
-- for the database's environment identity, written by data-engine at boot from
-- settings.capture_environment.
--
-- All head readers filter `environment = (select environment from mart.environment_identity)`
-- instead of a hardcoded literal.

create table if not exists mart.environment_identity (
    singleton boolean primary key default true check (singleton),
    environment text not null,
    declared_at timestamptz not null default clock_timestamp()
);

comment on table mart.environment_identity is
    '#756: The single authoritative environment declared for this database by data-engine on boot.';

-- Seed with 'production' initially if empty, so that existing production/staging readers continue
-- working until data-engine declares the true capture environment.
insert into mart.environment_identity (singleton, environment)
values (true, 'production')
on conflict (singleton) do nothing;

-- Redefine mart.governed_strategy_run to read from mart.environment_identity instead of 'production' literal.
do $$
declare
    wanted constant text := $view$
with head as (
    select environment, universe_id, universe_version, factor_id, target_run_id, sequence, advanced_at
    from mart.current_pointer_head
    where environment = (select environment from mart.environment_identity)
      and factor_id = 'gross_profit_per_employee'
      and universe_id like 'universe:topt-%'
    order by advanced_at desc
    limit 1
)
select head.environment,
       head.universe_id,
       head.universe_version,
       head.factor_id,
       head.target_run_id,
       head.sequence,
       head.advanced_at,
       status.cutoff,
       run.strategy_run_id,
       run.strategy_key,
       run.executed_at
from head
join mart.topt_capture_status status on status.run_id = head.target_run_id
join lateral (
    select distinct on (candidate.strategy_key)
           candidate.strategy_run_id, candidate.strategy_key, candidate.executed_at
    from mart.strategy_run_capture_bindings binding
    join mart.strategy_runs candidate on candidate.strategy_run_id = binding.strategy_run_id
    where binding.capture_run_id = head.target_run_id
    order by candidate.strategy_key, binding.bound_at desc, candidate.strategy_run_id desc
) run on true
$view$;
begin
    -- Superseded: 20260923T1600_datahub_performance_indices_and_entity_display_resolution.sql redefines mart.governed_strategy_run
    -- later in the chain and owns its definition. Replacing it here would put this
    -- older definition back (ACCESS EXCLUSIVE on the view) on every replay, only for
    -- that file to replace it again, so this definition creates the view only on a
    -- database that has none.
    if to_regclass('mart.governed_strategy_run') is null then
        execute 'create or replace view mart.governed_strategy_run as ' || wanted;
    end if;
end
$$;

-- Redefine staging.validate_topt_core_snapshot to compare against mart.environment_identity.
create or replace function staging.validate_topt_core_snapshot()
returns trigger language plpgsql as $$
declare
    capture_status mart.topt_capture_status%rowtype;
    release_exists boolean;
    release_matches_plan boolean;
begin
    select * into capture_status from mart.topt_capture_status where run_id = new.run_id;
    if capture_status.run_id is null
       or capture_status.environment <> (select environment from mart.environment_identity)
       or capture_status.obligation_count <> new.observation_count
       or capture_status.terminal_count <> capture_status.obligation_count
       or capture_status.success_count + capture_status.unchanged_count <> capture_status.obligation_count
       or capture_status.unavailable_count <> 0
       or capture_status.skipped_count <> 0
       or capture_status.failed_count <> 0
       or not capture_status.complete
       or capture_status.universe_id <> new.universe_id
       or capture_status.universe_version <> new.universe_version
       or capture_status.universe_sha256 <> new.universe_sha256
       or capture_status.cutoff <> new.cutoff then
        raise check_violation using message = 'core snapshot requires one complete exact capture run matching its own obligation count';
    end if;
    select exists (
        select 1 from staging.contract_objects
         where contract_id = new.release_manifest_id and contract_kind = 'release_manifest'
    ) into release_exists;
    select exists (
        select 1 from raw.production_topt_run_plans
         where run_id = new.run_id and release_manifest_id = new.release_manifest_id
    ) into release_matches_plan;
    if not release_exists or not release_matches_plan then
        raise check_violation using message = 'core snapshot release is not durable or does not match its run plan';
    end if;
    if raw.canonical_sha256(new.payload) <> new.content_sha256 then
        raise check_violation using message = 'core snapshot payload hash does not match';
    end if;
    return new;
end;
$$;
