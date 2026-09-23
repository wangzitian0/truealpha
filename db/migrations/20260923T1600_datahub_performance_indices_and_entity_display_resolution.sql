-- #987: performance indices, fast governed strategy run, and UUID-aware entity display resolution.
--
-- 1. Performance indexes for /admin and /research query paths:
--    - mart.strategy_runs (strategy_key, executed_at desc, created_at desc, strategy_run_id desc)
--    - mart.topt_core_results (run_id, issuer_id, cutoff)
--    - staging.kg_identifiers (identifier_type, identifier_value)
--    - staging.kg_identifiers (entity_id, identifier_type)
--
-- 2. Fast mart.governed_strategy_run: avoids 24k-row capture_obligations scan in mart.topt_capture_status
--    by joining raw.capture_runs directly to raw.capture_campaigns for the cutoff.
--
-- 3. UUID-aware mart.entity_display_resolution: resolves tickers via staging.entity_aliases for modern UUID-keyed
--    snapshot members, while maintaining full backward-compatibility with legacy listing:<mic>:<ticker> formats.

-- 1. Indexes (guarded with to_regclass so replay never acquires SHARE locks on populated tables)
do $$
begin
    if to_regclass('mart.idx_strategy_runs_strategy_key_order') is null then
        create index idx_strategy_runs_strategy_key_order
            on mart.strategy_runs (strategy_key, executed_at desc, created_at desc, strategy_run_id desc);
    end if;
    if to_regclass('mart.idx_topt_core_results_lookup') is null then
        create index idx_topt_core_results_lookup
            on mart.topt_core_results (run_id, issuer_id, cutoff);
    end if;
    if to_regclass('staging.idx_kg_identifiers_type_value') is null then
        create index idx_kg_identifiers_type_value
            on staging.kg_identifiers (identifier_type, identifier_value);
    end if;
    if to_regclass('staging.idx_kg_identifiers_entity_type') is null then
        create index idx_kg_identifiers_entity_type
            on staging.kg_identifiers (entity_id, identifier_type);
    end if;
end
$$;

-- 2. Redefine mart.governed_strategy_run
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
       campaign.cutoff,
       run.strategy_run_id,
       run.strategy_key,
       run.executed_at
from head
join raw.capture_runs cr on cr.run_id = head.target_run_id
join raw.capture_campaigns campaign using (campaign_id)
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
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.governed_strategy_run') is null
       or pg_get_viewdef(to_regclass('mart.governed_strategy_run'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.governed_strategy_run as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.governed_strategy_run is
    '#987: fast join of current pointer head, capture campaign, and bound strategy run.';

-- 3. Redefine mart.entity_display_resolution
do $$
declare
    wanted constant text := $view$
with latest_members as (
    select distinct on (m.issuer_id)
        m.issuer_id,
        m.listing_id,
        case when m.listing_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             then m.listing_id::uuid else null end as listing_uuid,
        case when m.issuer_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             then m.issuer_id::uuid else null end as issuer_uuid
    from staging.topt_core_snapshot_members m
    order by m.issuer_id, m.created_at desc
),
listing_tickers as (
    select distinct on (entity_id)
        entity_id,
        case
            when scheme = 'mic-ticker' then split_part(value, ':', 2)
            when scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
            then upper(split_part(value, ':', 3))
        end as ticker
    from staging.entity_aliases
    where scheme in ('mic-ticker', 'legacy-id')
    order by entity_id, (scheme = 'mic-ticker') desc
),
issuer_tickers as (
    select distinct on (entity_id)
        entity_id,
        case
            when scheme = 'mic-ticker' then split_part(value, ':', 2)
            when scheme = 'legacy-id' and value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
            then upper(split_part(value, ':', 3))
        end as ticker
    from staging.entity_aliases
    where scheme in ('mic-ticker', 'legacy-id')
    order by entity_id, (scheme = 'mic-ticker') desc
)
select
    lm.issuer_id,
    lm.listing_id,
    coalesce(
        lt.ticker,
        it.ticker,
        case
            when lm.listing_id ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'
            then upper(split_part(lm.listing_id, ':', 3))
            else null
        end
    ) as ticker,
    e.display_name
from latest_members lm
left join listing_tickers lt on lt.entity_id = lm.listing_uuid
left join issuer_tickers it on it.entity_id = lm.issuer_uuid
left join staging.kg_entities e on e.id = lm.issuer_id
order by lm.issuer_id;
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.entity_display_resolution') is null
       or pg_get_viewdef(to_regclass('mart.entity_display_resolution'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.entity_display_resolution as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;

comment on view mart.entity_display_resolution is
    '#987: issuer -> ticker/display_name for consumer rendering with fast entity_aliases lookup supporting both UUID entities and legacy string IDs.';
