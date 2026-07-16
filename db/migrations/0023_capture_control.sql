-- D5 E0: additive, append-only storage for list capture control identities.

create table if not exists raw.capture_campaigns (
    campaign_id       text primary key check (campaign_id ~ '^capture-campaign:[0-9a-f]{64}$'),
    content_sha256    text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    policy_id         text not null,
    environment       text not null,
    cutoff            timestamptz not null,
    created_at        timestamptz not null default now()
);

create table if not exists raw.capture_obligations (
    obligation_id          text primary key check (obligation_id ~ '^list-obligation:[0-9a-f]{64}$'),
    campaign_id            text not null references raw.capture_campaigns(campaign_id),
    list_version_id        text not null,
    subject_kind           text not null,
    subject_id             text not null,
    capture_requirement_id text not null,
    partition_key          text not null,
    content_sha256         text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at             timestamptz not null default now(),
    unique (campaign_id, list_version_id, subject_kind, subject_id, capture_requirement_id, partition_key)
);

create table if not exists raw.capture_work_items (
    work_item_id       text primary key check (work_item_id ~ '^capture-work-item:[0-9a-f]{64}$'),
    campaign_id        text not null references raw.capture_campaigns(campaign_id),
    request_id         text not null,
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at         timestamptz not null default now()
);

create table if not exists raw.capture_obligation_work_bindings (
    binding_id         text primary key,
    obligation_id      text not null references raw.capture_obligations(obligation_id),
    work_item_id       text not null references raw.capture_work_items(work_item_id),
    created_at         timestamptz not null default now(),
    unique (obligation_id, work_item_id)
);

create table if not exists raw.capture_attempts (
    attempt_id         text primary key check (attempt_id ~ '^fetch-attempt:[0-9a-f]{64}$'),
    work_item_id       text not null references raw.capture_work_items(work_item_id),
    attempt_number     integer not null check (attempt_number > 0),
    started_at         timestamptz not null,
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    unique (work_item_id, attempt_number)
);

create table if not exists raw.capture_attempt_results (
    attempt_result_id  text primary key,
    attempt_id         text not null unique references raw.capture_attempts(attempt_id),
    completed_at       timestamptz not null,
    outcome            text not null check (outcome in (
        'rate_limited', 'transport_error', 'server_error', 'interrupted',
        'success', 'unchanged', 'unavailable', 'failed'
    )),
    reason_codes       text[] not null check (cardinality(reason_codes) > 0),
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$')
);

create table if not exists raw.capture_checkpoints (
    checkpoint_id              text primary key check (checkpoint_id ~ '^capture-checkpoint:[0-9a-f]{64}$'),
    run_id                     text not null check (run_id ~ '^capture-run:[0-9a-f]{64}$'),
    sequence                   integer not null check (sequence > 0),
    phase                      text not null check (phase in ('planned', 'raw_landed', 'normalized', 'manifest_persisted')),
    completed_obligation_ids   text[] not null,
    recorded_at                timestamptz not null,
    content_sha256             text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    unique (run_id, sequence)
);

create table if not exists raw.recapture_plans (
    plan_id                    text primary key check (plan_id ~ '^recapture-plan:[0-9a-f]{64}$'),
    selection_cutoff           timestamptz not null,
    predicate_sha256           text not null check (predicate_sha256 ~ '^[0-9a-f]{64}$'),
    selected_obligation_ids    text[] not null check (cardinality(selected_obligation_ids) > 0),
    planner_version            text not null,
    content_sha256             text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at                 timestamptz not null default now()
);

create or replace function raw.enforce_capture_attempt_sequence()
returns trigger language plpgsql as $$
declare
    expected_attempt integer;
    previous_outcome text;
begin
    perform 1 from raw.capture_work_items where work_item_id = new.work_item_id for update;
    select coalesce(max(attempt_number), 0) + 1
      into expected_attempt
      from raw.capture_attempts
     where work_item_id = new.work_item_id;
    if new.attempt_number <> expected_attempt then
        raise exception 'capture attempts must be contiguous';
    end if;
    if expected_attempt > 1 then
        select result.outcome
          into previous_outcome
          from raw.capture_attempts attempt
          left join raw.capture_attempt_results result using (attempt_id)
         where attempt.work_item_id = new.work_item_id
         order by attempt.attempt_number desc
         limit 1;
        if previous_outcome is null then
            raise exception 'previous capture attempt has no result';
        end if;
        if previous_outcome in ('success', 'unchanged', 'unavailable', 'failed') then
            raise exception 'capture attempt after terminal outcome';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists enforce_attempt_sequence on raw.capture_attempts;
create trigger enforce_attempt_sequence
before insert on raw.capture_attempts
for each row execute function raw.enforce_capture_attempt_sequence();

create or replace function raw.validate_capture_attempt_result()
returns trigger language plpgsql as $$
declare
    dispatch_started_at timestamptz;
begin
    select started_at into dispatch_started_at
      from raw.capture_attempts
     where attempt_id = new.attempt_id;
    if dispatch_started_at is null then
        raise exception 'attempt result has no persisted dispatch';
    end if;
    if new.completed_at < dispatch_started_at then
        raise exception 'attempt result completion precedes dispatch';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_attempt_result on raw.capture_attempt_results;
create trigger validate_attempt_result
before insert on raw.capture_attempt_results
for each row execute function raw.validate_capture_attempt_result();

create or replace function raw.reject_capture_control_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'capture control records are append-only';
end;
$$;

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'capture_campaigns', 'capture_obligations', 'capture_work_items',
        'capture_obligation_work_bindings', 'capture_attempts',
        'capture_attempt_results', 'capture_checkpoints', 'recapture_plans'
    ] loop
        execute format('drop trigger if exists reject_mutation on raw.%I', table_name);
        execute format(
            'create trigger reject_mutation before update or delete on raw.%I '
            'for each row execute function raw.reject_capture_control_mutation()',
            table_name
        );
    end loop;
end;
$$;
