-- #1331: a reused observation keeps its lineage item in mart.topt_core_meta_info.
--
-- A TOPT tick reuses the observations that another run committed in the last 12 hours (#635).
-- The obligation of the reuse run then ends `unchanged`. Its work item names a source request of
-- the reuse run. The reused observation keeps the vintage of the run that fetched it, and so the
-- source request of that run.
--
-- 20261012T1130 joined the source request on two equalities: the request of the vintage and the
-- request of the work item. For a reused observation the two differ, so the inner join dropped the
-- item. On staging, the two reuse runs of 2026-10-15 lost all financial-fact and market-price
-- items. /research/trace then showed "6 input rows, 0 with a value".
--
-- This definition joins the source request through the vintage only. So `source_request_id`,
-- `source_registry_entry_id` and `source_policy_id` name the request that fetched the bytes.
-- The obligation of THIS run still gives the work item and its schedule policy, for the freshness
-- label. The freeze (`_load_observations`, with the reuse arm of #635) already selected each input
-- observation. So the view does not test the request identity again.
--
-- The view gives one item for each input observation. 20261012T1130 got this from the second
-- equality: each obligation of a run has its own source request. Without that equality, an
-- observation that two obligations of one run link gives two rows. `distinct on (observation_id)`
-- keeps one row. The order by obligation_id makes the choice deterministic.
--
-- A fresh run keeps the same lineage: the same items, keys, values and order. Every column, its name
-- and its order stay as in 20261012T1130. Nothing stored changes. The privileges of the view stay.
-- `create or replace view` keeps the owner and the ACL. db/roles.sql grants select on every mart
-- table to mart_readonly.
--
-- This is the last migration that defines the view, so it owns the shape. 20261012T1130 creates
-- the view only on a database that has none.
--
-- Boot-lock guard: `create or replace view` takes ACCESS EXCLUSIVE on the view. This file replaces
-- the view only when its stored definition differs from the one below. A temporary view with the
-- same text gives the comparison through pg_get_viewdef.
do $$
declare
    wanted constant text := $view$
select
    result.result_id,
    result.invocation_id,
    result.snapshot_id,
    result.run_id,
    result.release_manifest_id,
    result.universe_id,
    result.universe_version,
    result.universe_sha256,
    result.cutoff,
    result.issuer_id,
    result.instrument_id,
    result.listing_id,
    result.input_observation_ids,
    result.gppe_invocation_id,
    result.gppe_result_id,
    result.gppe_definition_id,
    result.gppe_definition_sha256,
    result.tier_definition_id,
    result.tier_definition_sha256,
    result.confidence,
    result.freshness,
    result.created_at,
    lineage.items as lineage
from mart.topt_core_results result
join lateral (
    select jsonb_agg(item.entry order by item.observation_id) as items
    from (
        -- #1331: one row for each input observation, also when two obligations of the run link it.
        select distinct on (observation.observation_id)
            observation.observation_id,
            jsonb_build_object(
                'observation_id', observation.observation_id,
                'semantic_type', observation.semantic_type,
                'semantic_version', observation.semantic_version,
                'source_vintage_id', observation.source_vintage_id,
                'source_request_id', vintage.source_request_id,
                'source_registry_entry_id', request.source_registry_entry_id,
                'source_policy_id', request.source_policy_id,
                'parser_version', observation.parser_version,
                'mapping_version', observation.mapping_version,
                'normalized_payload_sha256', observation.normalized_payload_sha256,
                'confidence', observation.confidence,
                'freshness', case
                    when result.cutoff - observation.knowable_at <= coalesce(nullif(policy.semantic_freshness_max_age->>observation.semantic_type, '')::interval, policy.freshness_max_age) then 'fresh'
                    else 'stale'
                end,
                'knowable_at', observation.knowable_at,
                'recorded_at', observation.recorded_at,
                -- #530 item 4: the filing behind each input (accession/form/period per input
                -- name), null for semantics that carry none (prices, identity).
                'vintage', payload.normalized_payload -> 'vintage',
                -- #1241: the input values, from a fixed whitelist. Absent stays absent.
                'inputs', (
                    select coalesce(jsonb_object_agg(field.key, field.value), '{}'::jsonb)
                    from jsonb_each(payload.normalized_payload) field
                    where field.key in (
                        'gross_profit', 'total_assets', 'headcount', 'revenue',
                        'shares_outstanding', 'close', 'currency', 'listing_id'
                    )
                )
            ) as entry
        from unnest(result.input_observation_ids) selected(observation_id)
        join staging.capture_normalized_observations observation using (observation_id)
        join staging.capture_observation_obligations usage using (observation_id)
        join staging.capture_observation_payloads payload using (observation_id)
        join raw.capture_obligations obligation
          on obligation.obligation_id = usage.capture_obligation_id
         and obligation.run_id = result.run_id
        join raw.capture_obligation_work_bindings binding
          on binding.obligation_id = obligation.obligation_id
        join raw.capture_work_items work using (work_item_id)
        join raw.capture_schedule_policies policy using (schedule_policy_id)
        join raw.capture_source_vintages vintage using (source_vintage_id)
        -- #1331: the request of the vintage. A reused observation keeps the request that fetched it.
        join raw.capture_source_requests request
          on request.source_request_id = vintage.source_request_id
        order by observation.observation_id, obligation.obligation_id
    ) item
) lineage on true
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.topt_core_meta_info') is null
       or pg_get_viewdef(to_regclass('mart.topt_core_meta_info'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.topt_core_meta_info as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;
