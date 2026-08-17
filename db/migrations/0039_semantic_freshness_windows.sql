-- 0039: per-semantic freshness windows (#530 slice 2).
--
-- One 2-day window graded all four semantics, which was survivable only because
-- knowable_at was fabricated as cutoff-58min (#530). Real source time needs real
-- windows per semantic: a Friday price bar is the freshest possible price at a
-- Sunday or Monday-holiday tick (needs ~5 days), a filed 10-K is the freshest
-- possible financial fact for months (the factor's own 730-day vintage bound is
-- the matching axis), and identity/universe cells are release-frozen
-- configuration. The map is keyed by observation.semantic_type; an absent key
-- falls back to the policy's single freshness_max_age, so existing policies keep
-- their exact behavior.

alter table raw.capture_schedule_policies
    add column if not exists semantic_freshness_max_age jsonb not null default '{}'::jsonb;

-- Both freshness-grading views, redefined verbatim from 0026 with the window
-- expression swapped for the per-semantic coalesce.

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
            'recorded_at', observation.recorded_at
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

create or replace view mart.topt_capture_meta_info as
select
    obligation.run_id,
    obligation.obligation_id,
    result.logical_obligation_id,
    obligation.subject_kind,
    obligation.subject_id,
    obligation.capture_requirement_id,
    obligation.partition_key,
    binding.work_item_id,
    work.source_request_id,
    request.source_registry_entry_id,
    request.source_policy_id,
    request.request_fingerprint_version,
    result.terminal_state,
    result.reason_codes,
    result.completed_at,
    coalesce(attempts.attempt_count, 0)::integer as attempt_count,
    final_attempt_result.status_code as final_status_code,
    observation.observation_id,
    observation.semantic_version,
    observation.parser_version,
    observation.mapping_version,
    observation.confidence,
    case
        when observation.observation_id is null then null
        when campaign.cutoff - observation.knowable_at <= coalesce(nullif(policy.semantic_freshness_max_age->>observation.semantic_type, '')::interval, policy.freshness_max_age) then 'fresh'
        else 'stale'
    end as freshness_state,
    observation.knowable_at,
    observation.recorded_at
from raw.capture_obligations obligation
join raw.capture_campaigns campaign using (campaign_id)
left join raw.capture_obligation_work_bindings binding
    on binding.obligation_id = obligation.obligation_id
left join raw.capture_work_items work using (work_item_id)
left join raw.capture_schedule_policies policy using (schedule_policy_id)
left join raw.capture_source_requests request using (source_request_id)
left join raw.capture_obligation_results result
    on result.capture_obligation_id = obligation.obligation_id
left join raw.capture_attempt_results final_attempt_result
    on final_attempt_result.attempt_id = result.final_attempt_id
left join lateral (
    select count(*) as attempt_count
    from raw.capture_attempts attempt
    where attempt.work_item_id = work.work_item_id
) attempts on true
left join lateral (
    select selected.*
    from (
        select candidate.*, count(*) over () as selection_count
        from staging.capture_observation_obligations usage
        join staging.capture_normalized_observations candidate using (observation_id)
        join raw.capture_source_vintages vintage
          on vintage.source_vintage_id = candidate.source_vintage_id
         and vintage.source_request_id = work.source_request_id
        where usage.capture_obligation_id = obligation.obligation_id
          and candidate.source_vintage_id = coalesce(
              final_attempt_result.source_vintage_id,
              final_attempt_result.reused_source_vintage_id
          )
          and candidate.subject_kind = obligation.subject_kind
          and candidate.subject_id = obligation.subject_id
          and candidate.semantic_type = regexp_replace(obligation.capture_requirement_id, ':v1$', '')
          and obligation.partition_key ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
          and (candidate.valid_from at time zone 'UTC')::date <= obligation.partition_key::date
          and (
              candidate.valid_to is null
              or (candidate.valid_to at time zone 'UTC')::date >= obligation.partition_key::date
          )
          and candidate.knowable_at <= campaign.cutoff
    ) selected
    where selected.selection_count = 1
) observation on true;
