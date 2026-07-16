-- D5 E0: additive, append-only storage for list capture control identities.

create extension if not exists pgcrypto;

create or replace function raw.canonical_json_string(value text)
returns text language plpgsql immutable strict as $$
declare
    encoded text := to_jsonb(value)::text;
    result text := '';
    character text;
    codepoint integer;
    offset_codepoint integer;
    item_index integer;
begin
    for item_index in 1..char_length(encoded) loop
        character := substr(encoded, item_index, 1);
        codepoint := ascii(character);
        if codepoint <= 127 then
            result := result || character;
        elsif codepoint <= 65535 then
            result := result || '\u' || lpad(to_hex(codepoint), 4, '0');
        else
            offset_codepoint := codepoint - 65536;
            result := result || '\u' || lpad(to_hex(55296 + (offset_codepoint >> 10)), 4, '0')
                || '\u' || lpad(to_hex(56320 + (offset_codepoint & 1023)), 4, '0');
        end if;
    end loop;
    return result;
end;
$$;

create or replace function raw.canonical_json(value jsonb)
returns text language plpgsql immutable strict as $$
declare
    result text;
begin
    case jsonb_typeof(value)
        when 'object' then
            select '{' || coalesce(string_agg(raw.canonical_json_string(key) || ':' || raw.canonical_json(item), ',' order by key collate "C"), '') || '}'
              into result
              from jsonb_each(value) as entry(key, item);
        when 'array' then
            select '[' || coalesce(string_agg(raw.canonical_json(item), ',' order by ordinal), '') || ']'
              into result
              from jsonb_array_elements(value) with ordinality as entry(item, ordinal);
        when 'string' then
            result := raw.canonical_json_string(value#>>'{}');
        else
            result := value::text;
    end case;
    return result;
end;
$$;

create or replace function raw.canonical_sha256(value jsonb)
returns text language sql immutable strict as $$
    select encode(digest(convert_to(raw.canonical_json(value), 'UTF8'), 'sha256'), 'hex')
$$;

create or replace function raw.canonical_timestamp(value timestamptz)
returns text language plpgsql immutable strict as $$
declare
    fraction text;
begin
    fraction := to_char(value at time zone 'UTC', 'US');
    return to_char(value at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || case when fraction = '000000' then '' else '.' || fraction end || 'Z';
end;
$$;

create or replace function raw.persisted_canonical_timestamp(value timestamptz, canonical text)
returns text language plpgsql immutable as $$
declare
    persisted text := coalesce(canonical, raw.canonical_timestamp(value));
begin
    if persisted !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$'
       or persisted::timestamptz <> value then
        raise check_violation using message = 'canonical timestamp does not match persisted instant';
    end if;
    return persisted;
end;
$$;

create or replace function raw.assert_content_address(
    actual_id text,
    id_prefix text,
    identity_payload jsonb,
    actual_content_sha256 text,
    content_payload jsonb
)
returns void language plpgsql immutable strict as $$
declare
    expected_id text := id_prefix || ':' || raw.canonical_sha256(identity_payload);
    expected_content text := raw.canonical_sha256(content_payload);
begin
    if actual_id <> expected_id then
        raise check_violation using message = id_prefix || ' identity does not match canonical payload';
    end if;
    if actual_content_sha256 <> expected_content then
        raise check_violation using message = id_prefix || ' content hash does not match canonical payload';
    end if;
end;
$$;

create or replace function raw.has_canonical_subjects(subjects jsonb)
returns boolean language plpgsql immutable strict as $$
declare
    item jsonb;
    previous_key text;
    current_key text;
begin
    if jsonb_typeof(subjects) <> 'array' or jsonb_array_length(subjects) = 0 then
        return false;
    end if;
    for item in select value from jsonb_array_elements(subjects) loop
        if jsonb_typeof(item) <> 'object'
           or (select count(*) from jsonb_object_keys(item)) <> 2
           or not item ?& array['kind', 'id']
           or jsonb_typeof(item->'kind') <> 'string'
           or jsonb_typeof(item->'id') <> 'string'
           or item->>'kind' not in ('issuer', 'security', 'listing', 'fund', 'analyst', 'universe', 'theme')
           or item->>'id' !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
           or lower(item->>'id') ~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])' then
            return false;
        end if;
        current_key := (item->>'kind') || E'\x1f' || (item->>'id');
        if previous_key is not null and previous_key collate "C" >= current_key collate "C" then
            return false;
        end if;
        previous_key := current_key;
    end loop;
    return true;
end;
$$;

create or replace function raw.has_canonical_universe_refs(refs jsonb)
returns boolean language plpgsql immutable strict as $$
declare
    item jsonb;
    previous_key text;
    current_key text;
begin
    if jsonb_typeof(refs) <> 'array' or jsonb_array_length(refs) = 0 then
        return false;
    end if;
    for item in select value from jsonb_array_elements(refs) loop
        if jsonb_typeof(item) <> 'object'
           or (select count(*) from jsonb_object_keys(item)) <> 3
           or not item ?& array['universe_id', 'universe_version', 'content_sha256']
           or jsonb_typeof(item->'universe_id') <> 'string'
           or jsonb_typeof(item->'universe_version') <> 'string'
           or jsonb_typeof(item->'content_sha256') <> 'string'
           or item->>'universe_id' !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
           or item->>'universe_version' !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
           or lower(item->>'universe_id') ~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])'
           or lower(item->>'universe_version') ~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])'
           or (item->>'content_sha256') !~ '^[0-9a-f]{64}$' then
            return false;
        end if;
        current_key := (item->>'universe_id') || E'\x1f' || (item->>'universe_version')
            || E'\x1f' || (item->>'content_sha256');
        if previous_key is not null and previous_key collate "C" >= current_key collate "C" then
            return false;
        end if;
        previous_key := current_key;
    end loop;
    return true;
end;
$$;

create or replace function raw.has_canonical_text_json_array(values_json jsonb, allow_empty boolean)
returns boolean language plpgsql immutable strict as $$
declare
    item_json jsonb;
    item text;
    previous_item text;
begin
    if jsonb_typeof(values_json) <> 'array' then
        return false;
    end if;
    if jsonb_array_length(values_json) = 0 then
        return allow_empty;
    end if;
    for item_json in select value from jsonb_array_elements(values_json) loop
        if jsonb_typeof(item_json) <> 'string' then
            return false;
        end if;
        item := item_json#>>'{}';
        if item !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
           or (previous_item is not null and previous_item collate "C" >= item collate "C") then
            return false;
        end if;
        previous_item := item;
    end loop;
    return true;
end;
$$;

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
        if item_index > 1 and ids[item_index - 1] collate "C" >= ids[item_index] collate "C" then
            return false;
        end if;
    end loop;
    return true;
end;
$$;

create or replace function raw.has_canonical_reason_codes(codes text[])
returns boolean language plpgsql immutable strict as $$
declare
    item_index integer;
begin
    if cardinality(codes) = 0 then
        return false;
    end if;
    for item_index in 1..cardinality(codes) loop
        if codes[item_index] is null or codes[item_index] !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$' then
            return false;
        end if;
        if item_index > 1 and codes[item_index - 1] collate "C" >= codes[item_index] collate "C" then
            return false;
        end if;
    end loop;
    return true;
end;
$$;

create or replace function raw.has_retry_outcome_partition(retryable text[], terminal text[])
returns boolean language sql immutable strict as $$
    select cardinality(retryable) > 0
       and cardinality(terminal) > 0
       and retryable = array(select outcome from unnest(retryable) as outcome order by outcome collate "C")
       and terminal = array(select outcome from unnest(terminal) as outcome order by outcome collate "C")
       and cardinality(retryable) = cardinality(array(select distinct unnest(retryable)))
       and cardinality(terminal) = cardinality(array(select distinct unnest(terminal)))
       and retryable <@ array[
           'rate_limited', 'transport_error', 'server_error', 'interrupted',
           'success', 'unchanged', 'unavailable', 'failed'
       ]::text[]
       and terminal <@ array[
           'rate_limited', 'transport_error', 'server_error', 'interrupted',
           'success', 'unchanged', 'unavailable', 'failed'
       ]::text[]
       and not retryable && terminal
       and array['failed', 'success', 'unavailable', 'unchanged']::text[] <@ terminal
$$;

create table if not exists raw.capture_campaigns (
    campaign_id       text primary key check (campaign_id ~ '^capture-campaign:[0-9a-f]{64}$'),
    content_sha256    text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    policy_id         text not null check (
        policy_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
        and lower(policy_id) !~ '(^|[._:/@+\-])(latest|current|default|stable|main|head)($|[._:/@+\-])'
    ),
    environment       text not null check (
        environment in ('local', 'local_dev', 'local_test', 'github_ci', 'preview', 'staging', 'production')
    ),
    cutoff            timestamptz not null,
    cutoff_canonical  text not null,
    universe_refs     jsonb not null check (raw.has_canonical_universe_refs(universe_refs)),
    created_at        timestamptz not null default now()
);

create table if not exists raw.capture_runs (
    run_id                 text primary key check (run_id ~ '^capture-run:[0-9a-f]{64}$'),
    campaign_id            text not null references raw.capture_campaigns(campaign_id),
    run_sequence           integer not null check (run_sequence > 0),
    schedule_policy_id     text not null check (schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'),
    capture_scope_id       text not null check (capture_scope_id ~ '^capture-scope:[0-9a-f]{64}$'),
    content_sha256         text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at             timestamptz not null default now(),
    unique (run_id, campaign_id),
    unique (campaign_id, run_sequence)
);

create table if not exists raw.capture_list_versions (
    list_version_id        text primary key check (list_version_id ~ '^list-version:[0-9a-f]{64}$'),
    universe_id            text not null check (
        universe_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
        and lower(universe_id) !~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])'
    ),
    universe_version       text not null check (
        universe_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
        and lower(universe_version) !~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])'
    ),
    universe_sha256        text not null check (universe_sha256 ~ '^[0-9a-f]{64}$'),
    effective_at           timestamptz not null,
    effective_at_canonical text not null,
    member_count           integer not null check (member_count > 0),
    members                jsonb not null check (raw.has_canonical_subjects(members)),
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

create table if not exists raw.capture_campaign_list_versions (
    campaign_id            text not null references raw.capture_campaigns(campaign_id),
    list_version_id        text not null references raw.capture_list_versions(list_version_id),
    created_at             timestamptz not null default now(),
    primary key (campaign_id, list_version_id)
);

create table if not exists raw.capture_obligations (
    obligation_id          text primary key check (obligation_id ~ '^capture-list-obligation:[0-9a-f]{64}$'),
    campaign_id            text not null references raw.capture_campaigns(campaign_id),
    run_id                 text not null check (run_id ~ '^capture-run:[0-9a-f]{64}$'),
    list_version_id        text not null check (list_version_id ~ '^list-version:[0-9a-f]{64}$'),
    subject_kind           text not null,
    subject_id             text not null,
    capture_requirement_id text not null check (
        capture_requirement_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
        and lower(capture_requirement_id) !~
            '(^|[._:/@+\-])(latest|current|default|stable|main|head)($|[._:/@+\-])'
    ),
    partition_key          text not null check (partition_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'),
    content_sha256         text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at             timestamptz not null default now(),
    unique (run_id, list_version_id, subject_kind, subject_id, capture_requirement_id, partition_key),
    foreign key (list_version_id) references raw.capture_list_versions(list_version_id),
    foreign key (campaign_id, list_version_id)
        references raw.capture_campaign_list_versions(campaign_id, list_version_id),
    foreign key (run_id, campaign_id) references raw.capture_runs(run_id, campaign_id),
    foreign key (list_version_id, subject_kind, subject_id)
        references raw.capture_list_version_members(list_version_id, subject_kind, subject_id)
);

create table if not exists raw.capture_work_items (
    work_item_id       text primary key check (work_item_id ~ '^capture-work-item:[0-9a-f]{64}$'),
    campaign_id        text not null references raw.capture_campaigns(campaign_id),
    source_request_id  text not null check (source_request_id ~ '^source-request:[0-9a-f]{64}$'),
    schedule_policy_id text not null check (schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'),
    maximum_attempts   integer not null check (maximum_attempts between 1 and 20),
    retryable_outcomes text[] not null,
    terminal_outcomes  text[] not null,
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    storage_envelope_sha256 text not null check (storage_envelope_sha256 ~ '^[0-9a-f]{64}$'),
    created_at         timestamptz not null default now(),
    check (raw.has_retry_outcome_partition(retryable_outcomes, terminal_outcomes)),
    unique (campaign_id, source_request_id, schedule_policy_id)
);

create table if not exists raw.capture_obligation_work_bindings (
    binding_id         text primary key check (binding_id ~ '^capture-obligation-work-binding:[0-9a-f]{64}$'),
    obligation_id      text not null references raw.capture_obligations(obligation_id),
    work_item_id       text not null references raw.capture_work_items(work_item_id),
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at         timestamptz not null default now(),
    unique (obligation_id)
);

create table if not exists raw.capture_attempts (
    attempt_id         text primary key check (attempt_id ~ '^fetch-attempt:[0-9a-f]{64}$'),
    work_item_id       text not null references raw.capture_work_items(work_item_id),
    attempt_number     integer not null check (attempt_number > 0),
    started_at         timestamptz not null,
    started_at_canonical text not null,
    content_sha256     text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    unique (work_item_id, attempt_number)
);

create table if not exists raw.capture_attempt_results (
    attempt_result_id  text primary key check (attempt_result_id ~ '^fetch-attempt-result:[0-9a-f]{64}$'),
    attempt_id         text not null unique references raw.capture_attempts(attempt_id),
    completed_at       timestamptz not null,
    completed_at_canonical text not null,
    outcome            text not null check (outcome in (
        'rate_limited', 'transport_error', 'server_error', 'interrupted',
        'success', 'unchanged', 'unavailable', 'failed'
    )),
    status_code        integer check (status_code between 100 and 599),
    reason_codes       text[] not null check (raw.has_canonical_reason_codes(reason_codes)),
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
    run_id                     text not null references raw.capture_runs(run_id),
    sequence                   integer not null check (sequence > 0),
    phase                      text not null check (phase in ('planned', 'raw_landed', 'normalized', 'manifest_persisted')),
    completed_obligation_ids   text[] not null check (
        raw.has_canonical_obligation_ids(completed_obligation_ids, true)
    ),
    recorded_at                timestamptz not null,
    recorded_at_canonical      text not null,
    content_sha256             text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    unique (run_id, sequence)
);

create table if not exists raw.recapture_plans (
    plan_id                    text primary key check (plan_id ~ '^capture-list-recapture-plan:[0-9a-f]{64}$'),
    selection_cutoff           timestamptz not null,
    selection_cutoff_canonical text not null,
    predicate_sha256           text not null check (predicate_sha256 ~ '^[0-9a-f]{64}$'),
    predicate                  jsonb not null check (jsonb_typeof(predicate) = 'object'),
    selected_obligation_ids    text[] not null check (
        raw.has_canonical_obligation_ids(selected_obligation_ids, false)
    ),
    planner_version            text not null,
    content_sha256             text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at                 timestamptz not null default now()
);

create or replace function raw.validate_capture_campaign_address()
returns trigger language plpgsql as $$
declare
    payload jsonb;
begin
    new.cutoff_canonical := raw.persisted_canonical_timestamp(new.cutoff, new.cutoff_canonical);
    payload := jsonb_build_object(
        'campaign_policy_id', new.policy_id,
        'environment', new.environment,
        'cutoff', new.cutoff_canonical,
        'universe_refs', new.universe_refs
    );
    perform raw.assert_content_address(
        new.campaign_id,
        'capture-campaign',
        jsonb_build_object('kind', 'capture-campaign', 'identity', payload),
        new.content_sha256,
        payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_campaign_address on raw.capture_campaigns;
create trigger zz_validate_campaign_address
before insert on raw.capture_campaigns
for each row execute function raw.validate_capture_campaign_address();

create or replace function raw.validate_capture_run_address()
returns trigger language plpgsql as $$
declare
    payload jsonb;
begin
    payload := jsonb_build_object(
        'campaign_id', new.campaign_id,
        'run_sequence', new.run_sequence,
        'schedule_policy_id', new.schedule_policy_id,
        'capture_scope_id', new.capture_scope_id
    );
    perform raw.assert_content_address(
        new.run_id,
        'capture-run',
        jsonb_build_object('kind', 'capture-run', 'identity', payload),
        new.content_sha256,
        payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_run_address on raw.capture_runs;
create trigger zz_validate_run_address
before insert on raw.capture_runs
for each row execute function raw.validate_capture_run_address();

create or replace function raw.validate_capture_list_version_address()
returns trigger language plpgsql as $$
declare
    payload jsonb;
begin
    new.effective_at_canonical := raw.persisted_canonical_timestamp(
        new.effective_at, new.effective_at_canonical
    );
    if new.member_count <> jsonb_array_length(new.members) then
        raise check_violation using message = 'list member count does not match canonical members';
    end if;
    payload := jsonb_build_object(
        'universe', jsonb_build_object(
            'universe_id', new.universe_id,
            'universe_version', new.universe_version,
            'content_sha256', new.universe_sha256
        ),
        'members', new.members,
        'effective_at', new.effective_at_canonical
    );
    perform raw.assert_content_address(new.list_version_id, 'list-version', payload, new.content_sha256, payload);
    return new;
end;
$$;

drop trigger if exists zz_validate_list_version_address on raw.capture_list_versions;
create trigger zz_validate_list_version_address
before insert on raw.capture_list_versions
for each row execute function raw.validate_capture_list_version_address();

create or replace function raw.validate_capture_obligation_address()
returns trigger language plpgsql as $$
declare
    universe_payload jsonb;
    subject_payload jsonb;
    obligation_identity jsonb;
    obligation_hash text;
    obligation_payload jsonb;
    payload jsonb;
begin
    select jsonb_build_object(
        'universe_id', universe_id,
        'universe_version', universe_version,
        'content_sha256', universe_sha256
    ) into universe_payload
      from raw.capture_list_versions
     where list_version_id = new.list_version_id;
    subject_payload := jsonb_build_object('kind', new.subject_kind, 'id', new.subject_id);
    obligation_identity := jsonb_build_object(
        'run_id', new.run_id,
        'universe_ref', universe_payload,
        'subject', subject_payload,
        'capture_requirement_id', new.capture_requirement_id,
        'partition', new.partition_key
    );
    obligation_hash := raw.canonical_sha256(
        jsonb_build_object('kind', 'list-obligation', 'identity', obligation_identity)
    );
    obligation_payload := obligation_identity || jsonb_build_object(
        'obligation_id', 'list-obligation:' || obligation_hash,
        'content_sha256', raw.canonical_sha256(obligation_identity)
    );
    payload := jsonb_build_object(
        'list_version_id', new.list_version_id,
        'obligation', obligation_payload
    );
    perform raw.assert_content_address(
        new.obligation_id, 'capture-list-obligation', payload, new.content_sha256, payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_obligation_address on raw.capture_obligations;
create trigger zz_validate_obligation_address
before insert on raw.capture_obligations
for each row execute function raw.validate_capture_obligation_address();

create or replace function raw.validate_capture_work_item_address()
returns trigger language plpgsql as $$
declare
    identity_payload jsonb;
    envelope_payload jsonb;
begin
    identity_payload := jsonb_build_object(
        'campaign_id', new.campaign_id,
        'source_request_id', new.source_request_id,
        'schedule_policy_id', new.schedule_policy_id
    );
    perform raw.assert_content_address(
        new.work_item_id,
        'capture-work-item',
        jsonb_build_object('kind', 'capture-work-item', 'identity', identity_payload),
        new.content_sha256,
        identity_payload
    );
    envelope_payload := jsonb_build_object(
        'work_item_id', new.work_item_id,
        'content_sha256', new.content_sha256,
        'maximum_attempts', new.maximum_attempts,
        'retryable_outcomes', to_jsonb(new.retryable_outcomes),
        'terminal_outcomes', to_jsonb(new.terminal_outcomes)
    );
    if new.storage_envelope_sha256 <> raw.canonical_sha256(envelope_payload) then
        raise check_violation using message = 'capture work-item storage envelope hash does not match';
    end if;
    return new;
end;
$$;

drop trigger if exists zz_validate_work_item_address on raw.capture_work_items;
create trigger zz_validate_work_item_address
before insert on raw.capture_work_items
for each row execute function raw.validate_capture_work_item_address();

create or replace function raw.validate_capture_binding_address()
returns trigger language plpgsql as $$
declare
    payload jsonb;
begin
    payload := jsonb_build_object('obligation_id', new.obligation_id, 'work_item_id', new.work_item_id);
    perform raw.assert_content_address(
        new.binding_id,
        'capture-obligation-work-binding',
        jsonb_build_object('kind', 'capture-obligation-work-binding', 'identity', payload),
        new.content_sha256,
        payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_binding_address on raw.capture_obligation_work_bindings;
create trigger zz_validate_binding_address
before insert on raw.capture_obligation_work_bindings
for each row execute function raw.validate_capture_binding_address();

create or replace function raw.validate_capture_attempt_address()
returns trigger language plpgsql as $$
declare
    identity_payload jsonb;
    content_payload jsonb;
begin
    new.started_at_canonical := raw.persisted_canonical_timestamp(new.started_at, new.started_at_canonical);
    identity_payload := jsonb_build_object(
        'work_item_id', new.work_item_id,
        'attempt_number', new.attempt_number
    );
    content_payload := identity_payload || jsonb_build_object('started_at', new.started_at_canonical);
    perform raw.assert_content_address(
        new.attempt_id,
        'fetch-attempt',
        jsonb_build_object('kind', 'fetch-attempt', 'identity', identity_payload),
        new.content_sha256,
        content_payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_attempt_address on raw.capture_attempts;
create trigger zz_validate_attempt_address
before insert on raw.capture_attempts
for each row execute function raw.validate_capture_attempt_address();

create or replace function raw.validate_capture_attempt_result_address()
returns trigger language plpgsql as $$
declare
    identity_payload jsonb;
    content_payload jsonb;
begin
    new.completed_at_canonical := raw.persisted_canonical_timestamp(
        new.completed_at, new.completed_at_canonical
    );
    identity_payload := jsonb_build_object('attempt_id', new.attempt_id);
    content_payload := identity_payload || jsonb_build_object(
        'completed_at', new.completed_at_canonical,
        'outcome', new.outcome,
        'status_code', new.status_code,
        'source_vintage_id', new.source_vintage_id,
        'reused_source_vintage_id', new.reused_source_vintage_id,
        'reason_codes', to_jsonb(new.reason_codes)
    );
    perform raw.assert_content_address(
        new.attempt_result_id,
        'fetch-attempt-result',
        jsonb_build_object('kind', 'fetch-attempt-result', 'identity', identity_payload),
        new.content_sha256,
        content_payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_attempt_result_address on raw.capture_attempt_results;
create trigger zz_validate_attempt_result_address
before insert on raw.capture_attempt_results
for each row execute function raw.validate_capture_attempt_result_address();

create or replace function raw.validate_capture_checkpoint_address()
returns trigger language plpgsql as $$
declare
    identity_payload jsonb;
    content_payload jsonb;
begin
    new.recorded_at_canonical := raw.persisted_canonical_timestamp(new.recorded_at, new.recorded_at_canonical);
    identity_payload := jsonb_build_object('run_id', new.run_id, 'sequence', new.sequence);
    content_payload := identity_payload || jsonb_build_object(
        'phase', new.phase,
        'completed_obligation_ids', to_jsonb(new.completed_obligation_ids),
        'recorded_at', new.recorded_at_canonical
    );
    perform raw.assert_content_address(
        new.checkpoint_id, 'capture-checkpoint', identity_payload, new.content_sha256, content_payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_checkpoint_address on raw.capture_checkpoints;
create trigger zz_validate_checkpoint_address
before insert on raw.capture_checkpoints
for each row execute function raw.validate_capture_checkpoint_address();

create or replace function raw.validate_recapture_plan_address()
returns trigger language plpgsql as $$
declare
    payload jsonb;
    predicate_identity jsonb;
    dimension text;
    bounded boolean := false;
begin
    new.selection_cutoff_canonical := raw.persisted_canonical_timestamp(
        new.selection_cutoff, new.selection_cutoff_canonical
    );
    if jsonb_typeof(new.predicate) <> 'object'
       or (select count(*) from jsonb_object_keys(new.predicate)) <> 12
       or not new.predicate ?& array[
           'predicate_id', 'content_sha256', 'universe_refs', 'subject_ids',
           'source_policy_ids', 'semantic_types', 'partitions', 'terminal_states',
           'freshness_states', 'parser_versions', 'mapping_versions', 'assessment_policy_ids'
       ]
       or jsonb_typeof(new.predicate->'predicate_id') <> 'string'
       or new.predicate->>'predicate_id' !~ '^recapture-predicate:[0-9a-f]{64}$'
       or jsonb_typeof(new.predicate->'content_sha256') <> 'string'
       or new.predicate->>'content_sha256' !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(new.predicate->'universe_refs') <> 'array'
       or (
           jsonb_array_length(new.predicate->'universe_refs') > 0
           and not raw.has_canonical_universe_refs(new.predicate->'universe_refs')
       ) then
        raise check_violation using message = 'recapture predicate does not match the typed contract';
    end if;
    foreach dimension in array array[
        'subject_ids', 'source_policy_ids', 'semantic_types', 'partitions', 'terminal_states',
        'freshness_states', 'parser_versions', 'mapping_versions', 'assessment_policy_ids'
    ] loop
        if not raw.has_canonical_text_json_array(new.predicate->dimension, true) then
            raise check_violation using message = 'recapture predicate arrays must be canonical';
        end if;
        if dimension = 'terminal_states' and exists (
            select 1 from jsonb_array_elements_text(new.predicate->dimension) as state(value)
             where value not in ('success', 'unchanged', 'unavailable', 'skipped_by_policy', 'failed')
        ) then
            raise check_violation using message = 'recapture terminal state is unknown';
        end if;
        if dimension = 'freshness_states' and exists (
            select 1 from jsonb_array_elements_text(new.predicate->dimension) as state(value)
             where value not in ('fresh', 'stale', 'unknown')
        ) then
            raise check_violation using message = 'recapture freshness state is unknown';
        end if;
        if dimension = any(array[
            'source_policy_ids', 'parser_versions', 'mapping_versions', 'assessment_policy_ids'
        ]) and exists (
            select 1
              from jsonb_array_elements_text(new.predicate->dimension) as coordinate(value)
             where lower(value) ~ '(^|[._:/@+\-])(latest|current|default|stable|main|head|tip)($|[._:/@+\-])'
        ) then
            raise check_violation using message = 'recapture predicate version coordinates must not be mutable';
        end if;
        bounded := bounded or jsonb_array_length(new.predicate->dimension) > 0;
    end loop;
    bounded := bounded or jsonb_array_length(new.predicate->'universe_refs') > 0;
    if not bounded then
        raise check_violation using message = 'an unbounded recapture predicate is forbidden';
    end if;
    predicate_identity := new.predicate - 'predicate_id' - 'content_sha256';
    perform raw.assert_content_address(
        new.predicate->>'predicate_id',
        'recapture-predicate',
        jsonb_build_object('kind', 'recapture-predicate', 'identity', predicate_identity),
        new.predicate->>'content_sha256',
        predicate_identity
    );
    if new.predicate_sha256 <> new.predicate->>'content_sha256' then
        raise check_violation using message = 'recapture predicate hash does not match typed content';
    end if;
    if new.planner_version !~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'
       or lower(new.planner_version) ~
           '(^|[._:/@+\-])(latest|current|default|stable|main|head|tip)($|[._:/@+\-])' then
        raise check_violation using message = 'recapture planner version must not be mutable';
    end if;
    payload := jsonb_build_object(
        'selection_cutoff', new.selection_cutoff_canonical,
        'predicate', new.predicate,
        'selected_obligation_ids', to_jsonb(new.selected_obligation_ids),
        'planner_version', new.planner_version
    );
    perform raw.assert_content_address(
        new.plan_id,
        'capture-list-recapture-plan',
        jsonb_build_object('kind', 'capture-list-recapture-plan', 'identity', payload),
        new.content_sha256,
        payload
    );
    return new;
end;
$$;

drop trigger if exists zz_validate_recapture_plan_address on raw.recapture_plans;
create trigger zz_validate_recapture_plan_address
before insert on raw.recapture_plans
for each row execute function raw.validate_recapture_plan_address();

create or replace function raw.validate_capture_list_member()
returns trigger language plpgsql as $$
declare
    expected_members integer;
    expected_member jsonb;
begin
    select member_count, members->(new.member_ordinal - 1) into expected_members, expected_member
      from raw.capture_list_versions
     where list_version_id = new.list_version_id
       for update;
    if new.member_ordinal > expected_members then
        raise exception 'list member ordinal exceeds frozen member count';
    end if;
    if expected_member is distinct from jsonb_build_object('kind', new.subject_kind, 'id', new.subject_id) then
        raise check_violation using message = 'list member does not match canonical ordinal';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_list_member on raw.capture_list_version_members;
create trigger validate_list_member
before insert on raw.capture_list_version_members
for each row execute function raw.validate_capture_list_member();

create or replace function raw.validate_campaign_list_version()
returns trigger language plpgsql as $$
declare
    expected_members integer;
    persisted_members integer;
    list_universe jsonb;
    campaign_universes jsonb;
begin
    select member_count, jsonb_build_object(
        'universe_id', universe_id,
        'universe_version', universe_version,
        'content_sha256', universe_sha256
    ) into expected_members, list_universe
      from raw.capture_list_versions
     where list_version_id = new.list_version_id
       for share;
    select count(*) into persisted_members
      from raw.capture_list_version_members
     where list_version_id = new.list_version_id;
    if expected_members is distinct from persisted_members then
        raise exception 'campaign declaration requires a complete frozen list version';
    end if;
    select universe_refs into campaign_universes
      from raw.capture_campaigns
     where campaign_id = new.campaign_id;
    if campaign_universes is null or not campaign_universes @> jsonb_build_array(list_universe) then
        raise exception 'campaign declaration requires the list universe ref';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_campaign_list_version on raw.capture_campaign_list_versions;
create trigger validate_campaign_list_version
before insert on raw.capture_campaign_list_versions
for each row execute function raw.validate_campaign_list_version();

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

create or replace function raw.validate_obligation_work_campaign()
returns trigger language plpgsql as $$
declare
    obligation_campaign text;
    work_campaign text;
    obligation_schedule_policy text;
    work_schedule_policy text;
begin
    select obligation.campaign_id, run.schedule_policy_id
      into obligation_campaign, obligation_schedule_policy
      from raw.capture_obligations obligation
      join raw.capture_runs run using (run_id, campaign_id)
     where obligation.obligation_id = new.obligation_id;
    select campaign_id, schedule_policy_id into work_campaign, work_schedule_policy
      from raw.capture_work_items
     where work_item_id = new.work_item_id;
    if obligation_campaign is distinct from work_campaign then
        raise exception 'obligation and work item must belong to the same capture campaign';
    end if;
    if obligation_schedule_policy is distinct from work_schedule_policy then
        raise exception 'obligation and work item must use the same schedule policy';
    end if;
    return new;
end;
$$;

drop trigger if exists validate_binding_campaign on raw.capture_obligation_work_bindings;
create trigger validate_binding_campaign
before insert on raw.capture_obligation_work_bindings
for each row execute function raw.validate_obligation_work_campaign();

create or replace function raw.enforce_capture_checkpoint_progress()
returns trigger language plpgsql as $$
declare
    previous_sequence integer;
    previous_phase text;
    previous_completed text[];
    previous_recorded_at timestamptz;
    previous_phase_rank integer;
    new_phase_rank integer;
begin
    perform pg_advisory_xact_lock(hashtextextended(new.run_id, 0));
    select sequence, phase, completed_obligation_ids, recorded_at
      into previous_sequence, previous_phase, previous_completed, previous_recorded_at
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
    if new.recorded_at < previous_recorded_at then
        raise exception 'capture checkpoint time cannot regress';
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
    previous_completed_at timestamptz;
    retryable_outcomes text[];
begin
    select work.maximum_attempts, work.retryable_outcomes
      into allowed_attempts, retryable_outcomes
      from raw.capture_work_items work
     where work.work_item_id = new.work_item_id
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
        select result.outcome, result.completed_at
          into previous_outcome, previous_completed_at
          from raw.capture_attempts attempt
          left join raw.capture_attempt_results result using (attempt_id)
         where attempt.work_item_id = new.work_item_id
         order by attempt.attempt_number desc
         limit 1;
        if previous_outcome is null then
            raise exception 'previous capture attempt has no result';
        end if;
        if previous_outcome <> all(retryable_outcomes) then
            raise exception 'capture attempt after non-retryable outcome';
        end if;
        if new.started_at < previous_completed_at then
            raise exception 'capture retry starts before previous result completion';
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
    dispatched_attempt integer;
    allowed_attempts integer;
    retryable_outcomes text[];
    terminal_outcomes text[];
begin
    select attempt.started_at, attempt.attempt_number, work.maximum_attempts,
           work.retryable_outcomes, work.terminal_outcomes
      into dispatch_started_at, dispatched_attempt, allowed_attempts,
           retryable_outcomes, terminal_outcomes
      from raw.capture_attempts attempt
      join raw.capture_work_items work using (work_item_id)
     where attempt.attempt_id = new.attempt_id;
    if dispatch_started_at is null then
        raise exception 'attempt result has no persisted dispatch';
    end if;
    if new.completed_at < dispatch_started_at then
        raise exception 'attempt result completion precedes dispatch';
    end if;
    if new.outcome <> all(retryable_outcomes) and new.outcome <> all(terminal_outcomes) then
        raise exception 'attempt outcome is not classified by the retry policy';
    end if;
    if dispatched_attempt = allowed_attempts and new.outcome <> all(terminal_outcomes) then
        raise exception 'final permitted attempt must have a terminal outcome';
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
        'capture_campaigns', 'capture_runs', 'capture_list_versions', 'capture_list_version_members',
        'capture_campaign_list_versions',
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
