-- Durable runtime records and read-only projections for manual TOPT capture.

create table if not exists raw.capture_schedule_policies (
    schedule_policy_id             text primary key check (schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'),
    content_sha256                 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version                 text not null,
    demanded_cadence               interval not null check (demanded_cadence > interval '0'),
    provider_availability_cadence  text not null,
    freshness_max_age              interval not null check (freshness_max_age > interval '0'),
    retry_policy                   jsonb not null check (jsonb_typeof(retry_policy) = 'object'),
    payload                        jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                     timestamptz not null default now()
);

create table if not exists raw.capture_source_requests (
    source_request_id              text primary key check (source_request_id ~ '^source-request:[0-9a-f]{64}$'),
    content_sha256                 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    source_registry_entry_id       text not null check (source_registry_entry_id ~ '^source-registry-entry:[0-9a-f]{64}$'),
    source_policy_id               text not null,
    request_fingerprint_version    text not null,
    canonical_request_sha256       text not null check (canonical_request_sha256 ~ '^[0-9a-f]{64}$'),
    subject_refs                   jsonb not null check (raw.has_canonical_subjects(subject_refs)),
    capture_requirement_ids        text[] not null,
    partition_key                  text not null,
    payload                        jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                     timestamptz not null default now()
);

alter table raw.capture_work_items
    drop constraint if exists capture_work_items_source_request_id_fkey;
alter table raw.capture_work_items
    add constraint capture_work_items_source_request_id_fkey
    foreign key (source_request_id) references raw.capture_source_requests(source_request_id)
    not valid;
alter table raw.capture_runs
    drop constraint if exists capture_runs_schedule_policy_id_fkey;
alter table raw.capture_runs
    add constraint capture_runs_schedule_policy_id_fkey
    foreign key (schedule_policy_id) references raw.capture_schedule_policies(schedule_policy_id)
    not valid;

create table if not exists raw.capture_source_vintages (
    source_vintage_id              text primary key check (source_vintage_id ~ '^source-vintage:[0-9a-f]{64}$'),
    content_sha256                 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    source_request_id              text not null references raw.capture_source_requests(source_request_id),
    source_record_id               text not null,
    source_published_at            timestamptz,
    raw_object_id                  text not null check (raw_object_id ~ '^raw-object:[0-9a-f]{64}$'),
    raw_fetch_id                   bigint references raw.fetches(id),
    payload                        jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                     timestamptz not null default now()
);

alter table raw.capture_attempt_results
    drop constraint if exists capture_attempt_results_source_vintage_id_fkey;
alter table raw.capture_attempt_results
    add constraint capture_attempt_results_source_vintage_id_fkey
    foreign key (source_vintage_id) references raw.capture_source_vintages(source_vintage_id)
    not valid;
alter table raw.capture_attempt_results
    drop constraint if exists capture_attempt_results_reused_source_vintage_id_fkey;
alter table raw.capture_attempt_results
    add constraint capture_attempt_results_reused_source_vintage_id_fkey
    foreign key (reused_source_vintage_id) references raw.capture_source_vintages(source_vintage_id)
    not valid;

create table if not exists staging.capture_normalized_observations (
    observation_id                 text primary key check (observation_id ~ '^normalized-observation:[0-9a-f]{64}$'),
    content_sha256                 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    capture_obligation_id          text not null references raw.capture_obligations(obligation_id),
    source_vintage_id              text not null references raw.capture_source_vintages(source_vintage_id),
    semantic_type                  text not null,
    semantic_version               text not null,
    subject_kind                   text not null,
    subject_id                     text not null,
    valid_from                     timestamptz not null,
    valid_to                       timestamptz,
    knowable_at                    timestamptz not null,
    parser_version                 text not null,
    mapping_version                text not null,
    normalized_payload_sha256      text not null check (normalized_payload_sha256 ~ '^[0-9a-f]{64}$'),
    is_restatement                 boolean not null,
    supersedes_observation_id      text references staging.capture_normalized_observations(observation_id),
    confidence                     numeric check (confidence between 0 and 1),
    freshness_state                text not null default 'unknown' check (freshness_state in ('fresh', 'stale', 'unknown')),
    payload                        jsonb not null check (jsonb_typeof(payload) = 'object'),
    recorded_at                    timestamptz not null default now(),
    check (valid_to is null or valid_to >= valid_from),
    check (recorded_at >= knowable_at)
);

create table if not exists raw.capture_obligation_results (
    result_id                      text primary key check (result_id ~ '^list-obligation-result:[0-9a-f]{64}$'),
    content_sha256                 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    capture_obligation_id          text not null unique references raw.capture_obligations(obligation_id),
    logical_obligation_id          text not null unique check (logical_obligation_id ~ '^list-obligation:[0-9a-f]{64}$'),
    terminal_state                 text not null check (terminal_state in ('success', 'unchanged', 'unavailable', 'skipped_by_policy', 'failed')),
    completed_at                   timestamptz not null,
    final_attempt_id               text references raw.capture_attempts(attempt_id),
    reason_codes                   text[] not null check (raw.has_canonical_reason_codes(reason_codes)),
    payload                        jsonb not null check (jsonb_typeof(payload) = 'object'),
    created_at                     timestamptz not null default now(),
    check ((terminal_state = 'skipped_by_policy') = (final_attempt_id is null))
);

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'capture_schedule_policies',
        'capture_source_requests',
        'capture_source_vintages',
        'capture_obligation_results'
    ] loop
        execute format('drop trigger if exists reject_mutation on raw.%I', table_name);
        execute format(
            'create trigger reject_mutation before update or delete on raw.%I '
            'for each row execute function raw.reject_capture_control_mutation()',
            table_name
        );
    end loop;
end $$;

drop trigger if exists reject_mutation on staging.capture_normalized_observations;
create trigger reject_mutation
before update or delete on staging.capture_normalized_observations
for each row execute function staging.reject_point_in_time_mutation();

create or replace view mart.topt_capture_status as
select
    run.run_id,
    run.campaign_id,
    campaign.environment,
    campaign.cutoff,
    list_version.universe_id,
    list_version.universe_version,
    list_version.universe_sha256,
    count(obligation.obligation_id)::integer as obligation_count,
    count(result.result_id)::integer as terminal_count,
    count(*) filter (where result.terminal_state = 'success')::integer as success_count,
    count(*) filter (where result.terminal_state = 'unchanged')::integer as unchanged_count,
    count(*) filter (where result.terminal_state = 'unavailable')::integer as unavailable_count,
    count(*) filter (where result.terminal_state = 'skipped_by_policy')::integer as skipped_count,
    count(*) filter (where result.terminal_state = 'failed')::integer as failed_count,
    count(result.result_id) = count(obligation.obligation_id) as complete
from raw.capture_runs run
join raw.capture_campaigns campaign using (campaign_id)
join raw.capture_obligations obligation using (run_id)
join raw.capture_list_versions list_version using (list_version_id)
left join raw.capture_obligation_results result
    on result.capture_obligation_id = obligation.obligation_id
group by
    run.run_id, run.campaign_id, campaign.environment, campaign.cutoff,
    list_version.universe_id, list_version.universe_version, list_version.universe_sha256;

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
    observation.freshness_state,
    observation.knowable_at,
    observation.recorded_at
from raw.capture_obligations obligation
left join raw.capture_obligation_work_bindings binding
    on binding.obligation_id = obligation.obligation_id
left join raw.capture_work_items work using (work_item_id)
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
    select candidate.*
    from staging.capture_normalized_observations candidate
    where candidate.capture_obligation_id = obligation.obligation_id
    order by candidate.recorded_at desc, candidate.observation_id desc
    limit 1
) observation on true;
