-- #1060: mart.topt_capture_meta_info judges valid time at the run's cutoff day.
--
-- 0039 compared observation valid_from and valid_to with obligation.partition_key. The
-- partition_key is the universe anchor. Since #1016 the capture sink writes valid_from as the
-- date of the fact itself, and that date can follow the anchor. The view then hid the
-- selected observation of every financial-fact and market-price cell: observation_id and
-- freshness_state were null. The materializer freezes the same observation with the window
-- judged at the cutoff day, and this definition applies the same rule through campaign.cutoff.
-- The valid-time clauses stay, because knowable_at does not bound valid_from or valid_to.
--
-- The columns are the same as in 0039, so create or replace view is valid.
-- This is the last migration that defines this view, so it owns the shape: the
-- replacement (ACCESS EXCLUSIVE on the view) runs only when the stored definition
-- differs from this one, compared through an identical temporary view.

do $$
declare
    wanted constant text := $view$
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
          and (candidate.valid_from at time zone 'UTC')::date <= (campaign.cutoff at time zone 'UTC')::date
          and (
              candidate.valid_to is null
              or (candidate.valid_to at time zone 'UTC')::date >= (campaign.cutoff at time zone 'UTC')::date
          )
          and candidate.knowable_at <= campaign.cutoff
    ) selected
    where selected.selection_count = 1
) observation on true
$view$;
begin
    execute 'create temp view boot_guard_candidate as ' || wanted;
    if to_regclass('mart.topt_capture_meta_info') is null
       or pg_get_viewdef(to_regclass('mart.topt_capture_meta_info'))
          is distinct from pg_get_viewdef(to_regclass('pg_temp.boot_guard_candidate'))
    then
        execute 'create or replace view mart.topt_capture_meta_info as ' || wanted;
    end if;
    drop view pg_temp.boot_guard_candidate;
end
$$;
