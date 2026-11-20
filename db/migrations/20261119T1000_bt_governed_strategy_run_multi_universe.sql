-- Expand mart.governed_strategy_run from TOPT-only LIMIT 1 to one latest governed
-- strategy run per scheduled strategy universe family (#1651, #1661).
do $$
begin
    if to_regclass('mart.governed_strategy_run') is null
       or pg_get_viewdef('mart.governed_strategy_run'::regclass, true) like '%universe:topt-%'
    then
        execute $vc$create or replace view mart.governed_strategy_run as
            with head as (
                select distinct on (regexp_replace(current_pointer_head.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''))
                    current_pointer_head.environment,
                    current_pointer_head.universe_id,
                    current_pointer_head.universe_version,
                    current_pointer_head.factor_id,
                    current_pointer_head.target_run_id,
                    current_pointer_head.sequence,
                    current_pointer_head.advanced_at
                from mart.current_pointer_head
                where current_pointer_head.environment = ((
                    select environment_identity.environment
                    from mart.environment_identity
                ))
                  and current_pointer_head.factor_id = 'gross_profit_per_employee'::text
                  and current_pointer_head.universe_id not like 'universe:canary-%'::text
                order by regexp_replace(current_pointer_head.universe_id, '-[0-9]{4}-[0-9]{2}-[0-9]{2}$', ''),
                         current_pointer_head.advanced_at desc
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
                    candidate.strategy_run_id,
                    candidate.strategy_key,
                    candidate.executed_at
                from mart.strategy_run_capture_bindings binding
                join mart.strategy_runs candidate on candidate.strategy_run_id = binding.strategy_run_id
                where binding.capture_run_id = head.target_run_id
                order by candidate.strategy_key, binding.bound_at desc, candidate.strategy_run_id desc
            ) run on true$vc$;
    end if;
end $$;
