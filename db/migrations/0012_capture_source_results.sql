-- Source assets persist independently, then one finalizer fuses only approved
-- routes into the row-complete capture observation. This permits field-level
-- fallbacks without mutating an already-written final result.
create table if not exists staging.capture_source_results (
    id                    bigint generated always as identity primary key,
    run_id                text not null,
    subject_id            text not null,
    domain                text not null,
    partition_key         text not null,
    source                text not null,
    outcome               text not null check (outcome in ('success', 'failed')),
    raw_refs              jsonb not null,
    domain_record_ids     jsonb not null,
    observed_fields       jsonb not null,
    min_knowable_at       timestamptz,
    max_knowable_at       timestamptz,
    observed_at           timestamptz not null,
    valid_time            daterange not null,
    transaction_time      timestamptz not null,
    recorded_at           timestamptz not null default now(),
    confidence            numeric not null check (confidence between 0 and 1),
    mapping_version       text not null,
    detail                text,
    unique (run_id, subject_id, domain, partition_key, source),
    check (jsonb_typeof(raw_refs) = 'array'),
    check (jsonb_array_length(raw_refs) > 0),
    check (jsonb_typeof(domain_record_ids) = 'array'),
    check (jsonb_typeof(observed_fields) = 'array'),
    check (max_knowable_at is null or max_knowable_at <= observed_at),
    check (recorded_at >= transaction_time),
    check (outcome = 'success' or detail is not null)
);

create index if not exists idx_capture_source_results_run
    on staging.capture_source_results (run_id, subject_id, domain);

-- Capture observations are semantic staging records too. The table was empty
-- before scheduled capture exists; refuse to guess axes for any surprise row.
do $$
begin
    if exists (select 1 from staging.capture_observations limit 1) then
        raise exception 'capture observations exist without full time axes; replay before migration 0012';
    end if;
end $$;

alter table staging.capture_observations add column if not exists valid_time daterange;
alter table staging.capture_observations add column if not exists transaction_time timestamptz;
alter table staging.capture_observations alter column valid_time set not null;
alter table staging.capture_observations alter column transaction_time set not null;

drop trigger if exists trg_capture_source_results_append_only on staging.capture_source_results;
create trigger trg_capture_source_results_append_only before update or delete on staging.capture_source_results
for each row execute function staging.reject_point_in_time_mutation();
