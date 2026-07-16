-- D5 E0: additive, append-only storage for list capture control identities.

create or replace function raw.has_canonical_obligation_ids(ids text[], allow_empty boolean)
returns boolean language plpgsql immutable strict as $$
declare
    item_index integer;
begin
    if cardinality(ids) = 0 then
        return allow_empty;
    end if;
    for item_index in 1..cardinality(ids) loop
        if ids[item_index] is null or ids[item_index] !~ '^capture-list-obligation:[0-9a-f]{64}$' then
            return false;
        end if;
        if item_index > 1 and ids[item_index - 1] >= ids[item_index] then
            return false;
        end if;
    end loop;
    return true;
end;
$$;

create table if not exists raw.capture_campaigns (
    campaign_id       text primary key check (campaign_id ~ '^capture-campaign:[0-9a-f]{64}$'),
    content_sha256    text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    policy_id         text not null,
    environment       text not null,
    cutoff            timestamptz not null,
    created_at        timestamptz not null default now()
);

create table if not exists raw.capture_list_versions (
    list_version_id        text primary key check (list_version_id ~ '^list-version:[0-9a-f]{64}$'),
    universe_id            text not null,
    universe_version       text not null,
    universe_sha256        text not null check (universe_sha256 ~ '^[0-9a-f]{64}$'),
    effective_at           timestamptz not null,
    member_count           integer not null check (member_count > 0),
    content_sha256         text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at             timestamptz not null default now()
);

create table if not exists raw.capture_list_version_members (
    list_version_id        text not null references raw.capture_list_versions(list_version_id),
    member_ordinal         integer not null check (member_ordinal > 0),
    subject_kind           text not null,
    subject_id             text not null,
    created_at             timestamptz not null default now(),
    primary key (list_version_id, member_ordinal),
    unique (list_version_id, subject_kind, subject_id)
);

create table if not exists raw.capture_obligations (
    obligation_id          text primary key check (obligation_id ~ '^capture-list-obligation:[0-9a-f]{64}$'),
    campaign_id            text not null references raw.capture_campaigns(campaign_id),
    run_id                 text not null check (run_id ~ '^capture-run:[0-9a-f]{64}$'),
    list_version_id        text not null check (list_version_id ~ '^list-version:[0-9a-f]{64}$'),
    subject_kind           text not null,
    subject_id             text not null,
    capture_requirement_id text not null,
    partition_key          text not null,
    content_sha256         text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at             timestamptz not null default now(),
    unique (run_id, list_version_id, subject_kind, subject_id, capture_requirement_id, partition_key),
    foreign key (list_version_id) references raw.capture_list_versions(list_version_id),
    foreign key (list_version_id, subject_kind, subject_id)
        references raw.capture_list_version_members(list_version_id, subject_kind, subject_id)
);

create table if not exists raw.capture_work_items (
    work_item_id       text primary key check (work_item_id ~ '^capture-work-item:[0-9a-f]{64}$'),
    campaign_id        text not null references raw.capture_campaigns(campaign_id),
    source_request_id  text not null check (source_request_id ~ '^source-request:[0-9a-f]{64}$'),
    schedule_policy_id text not null check (schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'),
    maximum_attempts   integer not null check (maximum_attempts > 0),
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at         timestamptz not null default now(),
    unique (campaign_id, source_request_id, schedule_policy_id)
);

create table if not exists raw.capture_obligation_work_bindings (
    binding_id         text primary key check (binding_id ~ '^obligation-work-binding:[0-9a-f]{64}$'),
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
    attempt_result_id  text primary key check (attempt_result_id ~ '^fetch-attempt-result:[0-9a-f]{64}$'),
    attempt_id         text not null unique references raw.capture_attempts(attempt_id),
    completed_at       timestamptz not null,
    outcome            text not null check (outcome in (
        'rate_limited', 'transport_error', 'server_error', 'interrupted',
        'success', 'unchanged', 'unavailable', 'failed'
    )),
    reason_codes       text[] not null check (cardinality(reason_codes) > 0),
    source_vintage_id  text check (source_vintage_id is null or source_vintage_id ~ '^source-vintage:[0-9a-f]{64}$'),
    reused_source_vintage_id text check (
        reused_source_vintage_id is null or reused_source_vintage_id ~ '^source-vintage:[0-9a-f]{64}$'
    ),
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$')
    ,check (
        (outcome = 'success' and source_vintage_id is not null and reused_source_vintage_id is null)
        or (outcome = 'unchanged' and reused_source_vintage_id is not null and source_vintage_id is null)
        or (outcome not in ('success', 'unchanged') and source_vintage_id is null and reused_source_vintage_id is null)
    )
);

create table if not exists raw.capture_checkpoints (
    checkpoint_id              text primary key check (checkpoint_id ~ '^capture-checkpoint:[0-9a-f]{64}$'),
    run_id                     text not null check (run_id ~ '^capture-run:[0-9a-f]{64}$'),
    sequence                   integer not null check (sequence > 0),
    phase                      text not null check (phase in ('planned', 'raw_landed', 'normalized', 'manifest_persisted')),
    completed_obligation_ids   text[] not null check (
        raw.has_canonical_obligation_ids(completed_obligation_ids, true)
    ),
    recorded_at                timestamptz not null,
    content_sha256             text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    unique (run_id, sequence)
);

create table if not exists raw.recapture_plans (
    plan_id                    text primary key check (plan_id ~ '^recapture-plan:[0-9a-f]{64}$'),
    selection_cutoff           timestamptz not null,
    predicate_sha256           text not null check (predicate_sha256 ~ '^[0-9a-f]{64}$'),
    selected_obligation_ids    text[] not null check (
        raw.has_canonical_obligation_ids(selected_obligation_ids, false)
    ),
    planner_version            text not null,
    content_sha256             text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at                 timestamptz not null default now()
);

create or replace function raw.validate_capture_list_member()
returns trigger language plpgsql as $$
declare
    expected_members integer;
begin
    select member_count into expected_members
      from raw.capture_list_versions
     where list_version_id = new.list_version_id
       for update;
    if new.member_ordinal > expected_members then
        raise exception 'list member ordinal exceeds frozen member count';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_list_member on raw.capture_list_version_members;
create trigger validate_list_member
before insert on raw.capture_list_version_members
for each row execute function raw.validate_capture_list_member();

create or replace function raw.validate_capture_obligation_list()
returns trigger language plpgsql as $$
declare
    expected_members integer;
    persisted_members integer;
begin
    select member_count into expected_members
      from raw.capture_list_versions
     where list_version_id = new.list_version_id
       for share;
    select count(*) into persisted_members
      from raw.capture_list_version_members
     where list_version_id = new.list_version_id;
    if expected_members is null then
        return new;
    end if;
    if persisted_members <> expected_members then
        raise exception 'capture obligation requires a complete frozen list version';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_obligation_list on raw.capture_obligations;
create trigger validate_obligation_list
before insert on raw.capture_obligations
for each row execute function raw.validate_capture_obligation_list();

create or replace function raw.enforce_capture_checkpoint_progress()
returns trigger language plpgsql as $$
declare
    previous_sequence integer;
    previous_phase text;
    previous_completed text[];
    previous_phase_rank integer;
    new_phase_rank integer;
begin
    perform pg_advisory_xact_lock(hashtextextended(new.run_id, 0));
    select sequence, phase, completed_obligation_ids
      into previous_sequence, previous_phase, previous_completed
      from raw.capture_checkpoints
     where run_id = new.run_id
     order by sequence desc
     limit 1;
    if previous_sequence is null then
        if new.sequence <> 1 then
            raise exception 'first capture checkpoint sequence must be one';
        end if;
        return new;
    end if;
    if new.sequence <> previous_sequence + 1 then
        raise exception 'capture checkpoint sequences must be contiguous';
    end if;
    previous_phase_rank := array_position(
        array['planned', 'raw_landed', 'normalized', 'manifest_persisted'], previous_phase
    );
    new_phase_rank := array_position(
        array['planned', 'raw_landed', 'normalized', 'manifest_persisted'], new.phase
    );
    if new_phase_rank < previous_phase_rank then
        raise exception 'capture checkpoint phase cannot regress';
    end if;
    if not previous_completed <@ new.completed_obligation_ids then
        raise exception 'capture checkpoint obligations cannot regress';
    end if;
    return new;
end;
$$;

drop trigger if exists enforce_checkpoint_progress on raw.capture_checkpoints;
create trigger enforce_checkpoint_progress
before insert on raw.capture_checkpoints
for each row execute function raw.enforce_capture_checkpoint_progress();

create or replace function raw.validate_checkpoint_obligation_refs()
returns trigger language plpgsql as $$
declare
    persisted_count integer;
begin
    if not raw.has_canonical_obligation_ids(new.completed_obligation_ids, true) then
        return new;
    end if;
    select count(*) into persisted_count
      from raw.capture_obligations
     where run_id = new.run_id
       and obligation_id = any(new.completed_obligation_ids);
    if persisted_count <> cardinality(new.completed_obligation_ids) then
        raise exception 'capture checkpoint references an unknown or cross-run obligation';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_checkpoint_obligation_refs on raw.capture_checkpoints;
create trigger validate_checkpoint_obligation_refs
before insert on raw.capture_checkpoints
for each row execute function raw.validate_checkpoint_obligation_refs();

create or replace function raw.validate_recapture_obligation_refs()
returns trigger language plpgsql as $$
declare
    persisted_count integer;
begin
    if not raw.has_canonical_obligation_ids(new.selected_obligation_ids, false) then
        return new;
    end if;
    select count(*) into persisted_count
      from raw.capture_obligations
     where obligation_id = any(new.selected_obligation_ids);
    if persisted_count <> cardinality(new.selected_obligation_ids) then
        raise exception 'recapture plan references an unknown obligation';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_recapture_obligation_refs on raw.recapture_plans;
create trigger validate_recapture_obligation_refs
before insert on raw.recapture_plans
for each row execute function raw.validate_recapture_obligation_refs();

create or replace function raw.enforce_capture_attempt_sequence()
returns trigger language plpgsql as $$
declare
    expected_attempt integer;
    allowed_attempts integer;
    previous_outcome text;
begin
    select maximum_attempts
      into allowed_attempts
      from raw.capture_work_items
     where work_item_id = new.work_item_id
       for update;
    select coalesce(max(attempt_number), 0) + 1
      into expected_attempt
      from raw.capture_attempts
     where work_item_id = new.work_item_id;
    if new.attempt_number <> expected_attempt then
        raise exception 'capture attempts must be contiguous';
    end if;
    if new.attempt_number > allowed_attempts then
        raise exception 'capture attempt exceeds maximum attempts';
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
        'capture_campaigns', 'capture_list_versions', 'capture_list_version_members',
        'capture_obligations', 'capture_work_items',
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
