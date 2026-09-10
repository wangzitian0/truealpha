-- #530 item 4: the vintage projection reads the payload table, not the envelope.
--
-- 20260908T1014 added `'vintage', observation.payload -> 'vintage'` to every lineage item so
-- a served number could name its filing. It read the wrong column.
-- `staging.capture_normalized_observations.payload` is the observation ENVELOPE — measured on
-- production, its keys are content_sha256, is_restatement, knowable_at, mapping_version,
-- normalized_payload_sha256, observation_id, parser_version, semantic_type, semantic_version,
-- source_vintage_id, subject, supersedes_observation_id. No business field is in it at all,
-- so `-> 'vintage'` was always NULL and no join objected.
--
-- The business payload lives in `staging.capture_observation_payloads.normalized_payload`,
-- which the view never joined. Measured on the 2026-09-09 governed TOPT head: 0 of 84 lineage
-- items carried a vintage, while the payload table had one on every financial-fact
-- observation ({accession, form, fy, fp, filed, period_end} per input name).
--
-- A NULL under a key that exists is worse than an absent key: a consumer checking
-- `lineage ? 'vintage'` finds it present and reads null as "this filing is unknown" rather
-- than "this projection is broken". Nothing stored changes; the view is replaced in place and
-- every historical run's lineage gains its vintage on the next read.
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
            'vintage', payload.normalized_payload -> 'vintage'
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
) lineage on true;
