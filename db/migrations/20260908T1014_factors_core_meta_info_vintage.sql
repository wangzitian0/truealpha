-- #530 item 4 / #747: the served row names the filing behind each financial input.
-- Same view as 0039, plus `vintage` (observation.payload -> 'vintage': per input name
-- {accession, form, fy, fp, filed, period_end}) in every lineage item. Views are
-- replaced in place; nothing stored changes.
create or replace view mart.topt_core_meta_info as
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
            'vintage', observation.payload -> 'vintage'
        ) order by observation.observation_id
    ) as items
    from unnest(result.input_observation_ids) selected(observation_id)
    join staging.capture_normalized_observations observation using (observation_id)
    join staging.capture_observation_obligations usage using (observation_id)
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
) lineage on true;
