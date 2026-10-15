-- #1241: the input values behind a served number reach mart.topt_core_meta_info.
--
-- 20260910T0930 put the filing behind each input (`vintage`) on every lineage item. It did not
-- put the input values there. The values (gross_profit, total_assets, headcount, revenue,
-- shares_outstanding and the price close) exist only in
-- `staging.capture_observation_payloads.normalized_payload`, and the App reads mart only
-- (AGENTS.md). So /research/trace could name a document and not the number read from it.
--
-- This replacement adds ONE key to every lineage item: `inputs`. It is a jsonb object copied
-- from `normalized_payload` through a fixed whitelist of keys. A whitelisted key that the
-- payload does not hold stays absent. Nothing is defaulted. A key that the payload holds as
-- JSON null stays null: the payload asserts "no value", and the reader renders "not recorded".
-- An observation with none of the keys gets the empty object.
--
-- The whitelist names the five financial inputs and the price close, the `currency` that
-- prices them, and the `listing_id` that owns them. A core result covers one issuer. An issuer
-- with two listings (dual-class) has two financial-fact and two market-price items in one
-- lineage, and only `listing_id` tells them apart (#1241 audit).
--
-- Every column and every other lineage key is identical to 20260910T0930. Nothing stored
-- changes. The privileges of the view stay as they are: `create or replace view` keeps the
-- owner and the ACL, and db/roles.sql grants select on every mart table to mart_readonly.
--
-- Replayed on every boot (2026-09-17 lock incident). `create or replace view` takes ACCESS
-- EXCLUSIVE even when nothing changes. 20261015T0720 (#1331) now owns the definition, so this
-- file creates the view only on a database that has none.
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
    select jsonb_agg(
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
        ) order by observation.observation_id
    ) as items
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
    join raw.capture_source_requests request
      on request.source_request_id = vintage.source_request_id
     and request.source_request_id = work.source_request_id
) lineage on true
$view$;
begin
    -- Superseded: 20261015T0720_web_topt_core_meta_info_reused_inputs.sql (#1331) redefines this
    -- view later in the chain and owns its definition. A boot replays this file first. A
    -- replacement here would put this older definition back (ACCESS EXCLUSIVE on the view) on
    -- every replay, only for that file to replace it again. So this file creates the view only
    -- on a database that has none.
    if to_regclass('mart.topt_core_meta_info') is null then
        execute 'create or replace view mart.topt_core_meta_info as ' || wanted;
    end if;
end
$$;
