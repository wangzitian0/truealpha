-- idempotent: true
-- Canonical raw schema baseline (Wave 2 squash).
set lock_timeout = '5s';
set statement_timeout = '30s';

create extension if not exists pgcrypto;
create schema if not exists raw;
create schema if not exists staging;
create schema if not exists mart;
create schema if not exists app;
create schema if not exists dagster;

CREATE OR REPLACE FUNCTION raw.canonical_json_string(value text)
 RETURNS text
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.has_canonical_subjects(subjects jsonb)
 RETURNS boolean
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.has_canonical_universe_refs(refs jsonb)
 RETURNS boolean
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.has_canonical_reason_codes(codes text[])
 RETURNS boolean
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.has_retry_outcome_partition(retryable text[], terminal text[])
 RETURNS boolean
 LANGUAGE sql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.canonical_json(value jsonb)
 RETURNS text
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.canonical_sha256(value jsonb)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE STRICT
 SET search_path TO 'pg_catalog', 'public'
AS $function$
    select encode(digest(convert_to(raw.canonical_json(value), 'UTF8'), 'sha256'), 'hex')
$function$;

create table if not exists raw.capture_campaigns (
 campaign_id text not null,
 content_sha256 text not null,
 policy_id text not null,
 environment text not null,
 cutoff timestamp with time zone not null,
 cutoff_canonical text not null,
 universe_refs jsonb not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_campaigns_pkey PRIMARY KEY (campaign_id),
 constraint capture_campaigns_campaign_id_check CHECK ((campaign_id ~ '^capture-campaign:[0-9a-f]{64}$'::text)),
 constraint capture_campaigns_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_campaigns_environment_check CHECK ((environment = ANY (ARRAY['local'::text, 'local_dev'::text, 'local_test'::text, 'github_ci'::text, 'preview'::text, 'staging'::text, 'production'::text]))),
 constraint capture_campaigns_policy_id_check CHECK (((policy_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'::text) AND (lower(policy_id) !~ '(^|[._:/@+\-])(latest|current|default|stable|main|head)($|[._:/@+\-])'::text))),
 constraint capture_campaigns_universe_refs_check CHECK (raw.has_canonical_universe_refs(universe_refs))
);

create table if not exists raw.capture_list_versions (
 list_version_id text not null,
 universe_id text not null,
 universe_version text not null,
 universe_sha256 text not null,
 effective_at timestamp with time zone not null,
 effective_at_canonical text not null,
 member_count integer not null,
 members jsonb not null,
 content_sha256 text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_list_versions_pkey PRIMARY KEY (list_version_id),
 constraint capture_list_versions_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_list_versions_list_version_id_check CHECK ((list_version_id ~ '^list-version:[0-9a-f]{64}$'::text)),
 constraint capture_list_versions_member_count_check CHECK ((member_count > 0)),
 constraint capture_list_versions_members_check CHECK (raw.has_canonical_subjects(members)),
 constraint capture_list_versions_universe_id_check CHECK (((universe_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'::text) AND (lower(universe_id) !~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])'::text))),
 constraint capture_list_versions_universe_sha256_check CHECK ((universe_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_list_versions_universe_version_check CHECK (((universe_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'::text) AND (lower(universe_version) !~ '(^|[._:/@+\-])(current|head|latest)($|[._:/@+\-])'::text)))
);

create table if not exists raw.capture_schedule_policies (
 schedule_policy_id text not null,
 content_sha256 text not null,
 policy_version text not null,
 demanded_cadence interval not null,
 provider_availability_cadence text not null,
 freshness_max_age interval not null,
 retry_policy jsonb not null,
 payload jsonb not null,
 created_at timestamp with time zone default now() not null,
 semantic_freshness_max_age jsonb default '{}'::jsonb not null,
 constraint capture_schedule_policies_pkey PRIMARY KEY (schedule_policy_id),
 constraint capture_schedule_policies_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_schedule_policies_demanded_cadence_check CHECK ((demanded_cadence > '00:00:00'::interval)),
 constraint capture_schedule_policies_freshness_max_age_check CHECK ((freshness_max_age > '00:00:00'::interval)),
 constraint capture_schedule_policies_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint capture_schedule_policies_retry_policy_check CHECK ((jsonb_typeof(retry_policy) = 'object'::text)),
 constraint capture_schedule_policies_schedule_policy_id_check CHECK ((schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'::text)),
 constraint semantic_freshness_max_age_is_object CHECK ((jsonb_typeof(semantic_freshness_max_age) = 'object'::text))
);

create table if not exists raw.capture_source_requests (
 source_request_id text not null,
 content_sha256 text not null,
 source_registry_entry_id text not null,
 source_policy_id text not null,
 request_fingerprint_version text not null,
 canonical_request_sha256 text not null,
 subject_refs jsonb not null,
 capture_requirement_ids text[] not null,
 partition_key text not null,
 payload jsonb not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_source_requests_pkey PRIMARY KEY (source_request_id),
 constraint capture_source_requests_canonical_request_sha256_check CHECK ((canonical_request_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_source_requests_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_source_requests_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint capture_source_requests_source_registry_entry_id_check CHECK ((source_registry_entry_id ~ '^source-registry-entry:[0-9a-f]{64}$'::text)),
 constraint capture_source_requests_source_request_id_check CHECK ((source_request_id ~ '^source-request:[0-9a-f]{64}$'::text)),
 constraint capture_source_requests_subject_refs_check CHECK (raw.has_canonical_subjects(subject_refs))
);

create table if not exists raw.fetches (
 id bigint generated always as identity,
 source text not null,
 source_record_id text not null,
 payload_sha256 text not null,
 object_uri text not null,
 content_type text not null,
 byte_length bigint not null,
 source_published_at timestamp with time zone,
 fetched_at timestamp with time zone not null,
 recorded_at timestamp with time zone default now() not null,
 metadata jsonb default '{}'::jsonb not null,
 constraint fetches_pkey PRIMARY KEY (id),
 constraint fetches_source_source_record_id_payload_sha256_key UNIQUE (source, source_record_id, payload_sha256),
 constraint fetches_byte_length_check CHECK ((byte_length >= 0)),
 constraint fetches_check CHECK ((recorded_at >= fetched_at)),
 constraint fetches_object_uri_check CHECK ((object_uri ~~ 's3://%'::text)),
 constraint fetches_payload_sha256_check CHECK ((payload_sha256 ~ '^[0-9a-f]{64}$'::text))
);

create table if not exists raw.capture_campaign_list_versions (
 campaign_id text not null,
 list_version_id text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_campaign_list_versions_pkey PRIMARY KEY (campaign_id, list_version_id),
 constraint capture_campaign_list_versions_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES raw.capture_campaigns(campaign_id),
 constraint capture_campaign_list_versions_list_version_id_fkey FOREIGN KEY (list_version_id) REFERENCES raw.capture_list_versions(list_version_id)
);

create table if not exists raw.capture_list_version_members (
 list_version_id text not null,
 member_ordinal integer not null,
 subject_kind text not null,
 subject_id text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_list_version_members_pkey PRIMARY KEY (list_version_id, member_ordinal),
 constraint capture_list_version_members_list_version_id_subject_kind_s_key UNIQUE (list_version_id, subject_kind, subject_id),
 constraint capture_list_version_members_member_ordinal_check CHECK ((member_ordinal > 0)),
 constraint capture_list_version_members_list_version_id_fkey FOREIGN KEY (list_version_id) REFERENCES raw.capture_list_versions(list_version_id)
);

create table if not exists raw.capture_runs (
 run_id text not null,
 campaign_id text not null,
 run_sequence integer not null,
 schedule_policy_id text not null,
 capture_scope_id text not null,
 content_sha256 text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_runs_pkey PRIMARY KEY (run_id),
 constraint capture_runs_campaign_id_run_sequence_key UNIQUE (campaign_id, run_sequence),
 constraint capture_runs_run_id_campaign_id_key UNIQUE (run_id, campaign_id),
 constraint capture_runs_capture_scope_id_check CHECK ((capture_scope_id ~ '^capture-scope:[0-9a-f]{64}$'::text)),
 constraint capture_runs_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_runs_run_id_check CHECK ((run_id ~ '^capture-run:[0-9a-f]{64}$'::text)),
 constraint capture_runs_run_sequence_check CHECK ((run_sequence > 0)),
 constraint capture_runs_schedule_policy_id_check CHECK ((schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'::text)),
 constraint capture_runs_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES raw.capture_campaigns(campaign_id)
);

create table if not exists raw.capture_work_items (
 work_item_id text not null,
 campaign_id text not null,
 source_request_id text not null,
 schedule_policy_id text not null,
 maximum_attempts integer not null,
 retryable_outcomes text[] not null,
 terminal_outcomes text[] not null,
 content_sha256 text not null,
 storage_envelope_sha256 text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_work_items_pkey PRIMARY KEY (work_item_id),
 constraint capture_work_items_campaign_id_source_request_id_schedule_p_key UNIQUE (campaign_id, source_request_id, schedule_policy_id),
 constraint capture_work_items_check CHECK (raw.has_retry_outcome_partition(retryable_outcomes, terminal_outcomes)),
 constraint capture_work_items_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_work_items_maximum_attempts_check CHECK (((maximum_attempts >= 1) AND (maximum_attempts <= 20))),
 constraint capture_work_items_schedule_policy_id_check CHECK ((schedule_policy_id ~ '^schedule-policy:[0-9a-f]{64}$'::text)),
 constraint capture_work_items_source_request_id_check CHECK ((source_request_id ~ '^source-request:[0-9a-f]{64}$'::text)),
 constraint capture_work_items_storage_envelope_sha256_check CHECK ((storage_envelope_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_work_items_work_item_id_check CHECK ((work_item_id ~ '^capture-work-item:[0-9a-f]{64}$'::text)),
 constraint capture_work_items_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES raw.capture_campaigns(campaign_id)
);

create table if not exists raw.capture_source_vintages (
 source_vintage_id text not null,
 content_sha256 text not null,
 source_request_id text not null,
 source_record_id text not null,
 source_published_at timestamp with time zone,
 raw_object_id text not null,
 raw_fetch_id bigint not null,
 payload jsonb not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_source_vintages_pkey PRIMARY KEY (source_vintage_id),
 constraint capture_source_vintages_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_source_vintages_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint capture_source_vintages_raw_object_id_check CHECK ((raw_object_id ~ '^raw-object:[0-9a-f]{64}$'::text)),
 constraint capture_source_vintages_source_vintage_id_check CHECK ((source_vintage_id ~ '^source-vintage:[0-9a-f]{64}$'::text)),
 constraint capture_source_vintages_raw_fetch_id_fkey FOREIGN KEY (raw_fetch_id) REFERENCES raw.fetches(id),
 constraint capture_source_vintages_source_request_id_fkey FOREIGN KEY (source_request_id) REFERENCES raw.capture_source_requests(source_request_id)
);

create table if not exists raw.capture_obligations (
 obligation_id text not null,
 campaign_id text not null,
 run_id text not null,
 list_version_id text not null,
 subject_kind text not null,
 subject_id text not null,
 capture_requirement_id text not null,
 partition_key text not null,
 content_sha256 text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_obligations_pkey PRIMARY KEY (obligation_id),
 constraint capture_obligations_run_id_list_version_id_subject_kind_sub_key UNIQUE (run_id, list_version_id, subject_kind, subject_id, capture_requirement_id, partition_key),
 constraint capture_obligations_capture_requirement_id_check CHECK (((capture_requirement_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'::text) AND (lower(capture_requirement_id) !~ '(^|[._:/@+\-])(latest|current|default|stable|main|head)($|[._:/@+\-])'::text))),
 constraint capture_obligations_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_obligations_list_version_id_check CHECK ((list_version_id ~ '^list-version:[0-9a-f]{64}$'::text)),
 constraint capture_obligations_obligation_id_check CHECK ((obligation_id ~ '^capture-list-obligation:[0-9a-f]{64}$'::text)),
 constraint capture_obligations_partition_key_check CHECK ((partition_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$'::text)),
 constraint capture_obligations_run_id_check CHECK ((run_id ~ '^capture-run:[0-9a-f]{64}$'::text)),
 constraint capture_obligations_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES raw.capture_campaigns(campaign_id),
 constraint capture_obligations_campaign_id_list_version_id_fkey FOREIGN KEY (campaign_id, list_version_id) REFERENCES raw.capture_campaign_list_versions(campaign_id, list_version_id),
 constraint capture_obligations_list_version_id_fkey FOREIGN KEY (list_version_id) REFERENCES raw.capture_list_versions(list_version_id),
 constraint capture_obligations_list_version_id_subject_kind_subject_i_fkey FOREIGN KEY (list_version_id, subject_kind, subject_id) REFERENCES raw.capture_list_version_members(list_version_id, subject_kind, subject_id),
 constraint capture_obligations_run_id_campaign_id_fkey FOREIGN KEY (run_id, campaign_id) REFERENCES raw.capture_runs(run_id, campaign_id)
);

create table if not exists raw.capture_run_plans (
 run_id text not null,
 release_manifest_id text not null,
 content_sha256 text not null,
 payload jsonb not null,
 created_at timestamp with time zone default clock_timestamp() not null,
 constraint capture_run_plans_pkey PRIMARY KEY (run_id),
 constraint capture_run_plans_content_sha256_key UNIQUE (content_sha256),
 constraint capture_run_plans_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_run_plans_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint capture_run_plans_release_manifest_id_check CHECK ((release_manifest_id ~ '^release-manifest:[0-9a-f]{64}$'::text)),
 constraint capture_run_plans_run_id_fkey FOREIGN KEY (run_id) REFERENCES raw.capture_runs(run_id)
);

create table if not exists raw.capture_attempts (
 attempt_id text not null,
 work_item_id text not null,
 attempt_number integer not null,
 started_at timestamp with time zone not null,
 started_at_canonical text not null,
 content_sha256 text not null,
 constraint capture_attempts_pkey PRIMARY KEY (attempt_id),
 constraint capture_attempts_work_item_id_attempt_number_key UNIQUE (work_item_id, attempt_number),
 constraint capture_attempts_attempt_id_check CHECK ((attempt_id ~ '^fetch-attempt:[0-9a-f]{64}$'::text)),
 constraint capture_attempts_attempt_number_check CHECK ((attempt_number > 0)),
 constraint capture_attempts_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_attempts_work_item_id_fkey FOREIGN KEY (work_item_id) REFERENCES raw.capture_work_items(work_item_id)
);

create table if not exists raw.capture_obligation_work_bindings (
 binding_id text not null,
 obligation_id text not null,
 work_item_id text not null,
 content_sha256 text not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_obligation_work_bindings_pkey PRIMARY KEY (binding_id),
 constraint capture_obligation_work_bindings_obligation_id_key UNIQUE (obligation_id),
 constraint capture_obligation_work_bindings_binding_id_check CHECK ((binding_id ~ '^capture-obligation-work-binding:[0-9a-f]{64}$'::text)),
 constraint capture_obligation_work_bindings_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_obligation_work_bindings_obligation_id_fkey FOREIGN KEY (obligation_id) REFERENCES raw.capture_obligations(obligation_id),
 constraint capture_obligation_work_bindings_work_item_id_fkey FOREIGN KEY (work_item_id) REFERENCES raw.capture_work_items(work_item_id)
);

create table if not exists raw.capture_attempt_results (
 attempt_result_id text not null,
 attempt_id text not null,
 completed_at timestamp with time zone not null,
 completed_at_canonical text not null,
 outcome text not null,
 status_code integer,
 reason_codes text[] not null,
 source_vintage_id text,
 reused_source_vintage_id text,
 content_sha256 text not null,
 constraint capture_attempt_results_pkey PRIMARY KEY (attempt_result_id),
 constraint capture_attempt_results_attempt_id_key UNIQUE (attempt_id),
 constraint capture_attempt_results_attempt_result_id_check CHECK ((attempt_result_id ~ '^fetch-attempt-result:[0-9a-f]{64}$'::text)),
 constraint capture_attempt_results_check CHECK ((((outcome = 'success'::text) AND (source_vintage_id IS NOT NULL) AND (reused_source_vintage_id IS NULL)) OR ((outcome = 'unchanged'::text) AND (reused_source_vintage_id IS NOT NULL) AND (source_vintage_id IS NULL)) OR ((outcome <> ALL (ARRAY['success'::text, 'unchanged'::text])) AND (source_vintage_id IS NULL) AND (reused_source_vintage_id IS NULL)))),
 constraint capture_attempt_results_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_attempt_results_outcome_check CHECK ((outcome = ANY (ARRAY['rate_limited'::text, 'transport_error'::text, 'server_error'::text, 'interrupted'::text, 'success'::text, 'unchanged'::text, 'unavailable'::text, 'failed'::text]))),
 constraint capture_attempt_results_reason_codes_check CHECK (raw.has_canonical_reason_codes(reason_codes)),
 constraint capture_attempt_results_reused_source_vintage_id_check CHECK (((reused_source_vintage_id IS NULL) OR (reused_source_vintage_id ~ '^source-vintage:[0-9a-f]{64}$'::text))),
 constraint capture_attempt_results_source_vintage_id_check CHECK (((source_vintage_id IS NULL) OR (source_vintage_id ~ '^source-vintage:[0-9a-f]{64}$'::text))),
 constraint capture_attempt_results_status_code_check CHECK (((status_code >= 100) AND (status_code <= 599))),
 constraint capture_attempt_results_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES raw.capture_attempts(attempt_id)
);

create table if not exists raw.capture_obligation_results (
 result_id text not null,
 content_sha256 text not null,
 capture_obligation_id text not null,
 logical_obligation_id text not null,
 terminal_state text not null,
 completed_at timestamp with time zone not null,
 final_attempt_id text,
 reason_codes text[] not null,
 payload jsonb not null,
 created_at timestamp with time zone default now() not null,
 constraint capture_obligation_results_pkey PRIMARY KEY (result_id),
 constraint capture_obligation_results_capture_obligation_id_key UNIQUE (capture_obligation_id),
 constraint capture_obligation_results_logical_obligation_id_key UNIQUE (logical_obligation_id),
 constraint capture_obligation_results_check CHECK (((terminal_state = 'skipped_by_policy'::text) = (final_attempt_id IS NULL))),
 constraint capture_obligation_results_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
 constraint capture_obligation_results_logical_obligation_id_check CHECK ((logical_obligation_id ~ '^list-obligation:[0-9a-f]{64}$'::text)),
 constraint capture_obligation_results_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
 constraint capture_obligation_results_reason_codes_check CHECK (raw.has_canonical_reason_codes(reason_codes)),
 constraint capture_obligation_results_result_id_check CHECK ((result_id ~ '^list-obligation-result:[0-9a-f]{64}$'::text)),
 constraint capture_obligation_results_terminal_state_check CHECK ((terminal_state = ANY (ARRAY['success'::text, 'unchanged'::text, 'unavailable'::text, 'skipped_by_policy'::text, 'failed'::text]))),
 constraint capture_obligation_results_capture_obligation_id_fkey FOREIGN KEY (capture_obligation_id) REFERENCES raw.capture_obligations(obligation_id),
 constraint capture_obligation_results_final_attempt_id_fkey FOREIGN KEY (final_attempt_id) REFERENCES raw.capture_attempts(attempt_id)
);

do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'raw.capture_attempt_results'::regclass and conname = 'capture_attempt_results_reused_source_vintage_id_fkey') then alter table raw.capture_attempt_results add constraint capture_attempt_results_reused_source_vintage_id_fkey FOREIGN KEY (reused_source_vintage_id) REFERENCES raw.capture_source_vintages(source_vintage_id) NOT VALID; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'raw.capture_attempt_results'::regclass and conname = 'capture_attempt_results_source_vintage_id_fkey') then alter table raw.capture_attempt_results add constraint capture_attempt_results_source_vintage_id_fkey FOREIGN KEY (source_vintage_id) REFERENCES raw.capture_source_vintages(source_vintage_id) NOT VALID; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'raw.capture_runs'::regclass and conname = 'capture_runs_schedule_policy_id_fkey') then alter table raw.capture_runs add constraint capture_runs_schedule_policy_id_fkey FOREIGN KEY (schedule_policy_id) REFERENCES raw.capture_schedule_policies(schedule_policy_id) NOT VALID; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conrelid = 'raw.capture_work_items'::regclass and conname = 'capture_work_items_source_request_id_fkey') then alter table raw.capture_work_items add constraint capture_work_items_source_request_id_fkey FOREIGN KEY (source_request_id) REFERENCES raw.capture_source_requests(source_request_id) NOT VALID; end if; end $$;

CREATE OR REPLACE FUNCTION raw.assert_content_address(actual_id text, id_prefix text, identity_payload jsonb, actual_content_sha256 text, content_payload jsonb)
 RETURNS void
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.canonical_timestamp(value timestamp with time zone)
 RETURNS text
 LANGUAGE plpgsql
 IMMUTABLE STRICT
AS $function$
declare
    fraction text;
begin
    fraction := to_char(value at time zone 'UTC', 'US');
    return to_char(value at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || case when fraction = '000000' then '' else '.' || fraction end || 'Z';
end;
$function$;
CREATE OR REPLACE FUNCTION raw.enforce_capture_attempt_sequence()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.persisted_canonical_timestamp(value timestamp with time zone, canonical text)
 RETURNS text
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$
declare
    persisted text := coalesce(canonical, raw.canonical_timestamp(value));
begin
    if persisted !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$'
       or persisted::timestamptz <> value then
        raise check_violation using message = 'canonical timestamp does not match persisted instant';
    end if;
    return persisted;
end;
$function$;
CREATE OR REPLACE FUNCTION raw.reject_capture_control_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'capture control records are append-only';
end;
$function$;
CREATE OR REPLACE FUNCTION raw.reject_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    raise exception 'raw records are append-only; insert a new payload vintage';
end;
$function$;
CREATE OR REPLACE FUNCTION raw.validate_campaign_list_version()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_attempt_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_attempt_result()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_attempt_result_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_binding_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_campaign_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_list_member()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_list_version_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_obligation_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_obligation_list()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_run_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_source_vintage_lineage()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
    landed_sha256 text;
begin
    select payload_sha256 into landed_sha256 from raw.fetches where id = new.raw_fetch_id;
    if landed_sha256 is null or new.raw_object_id <> 'raw-object:' || landed_sha256 then
        raise check_violation using message = 'capture source vintage raw lineage does not match landed bytes';
    end if;
    return new;
end;
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_work_item_address()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_obligation_work_campaign()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;
CREATE OR REPLACE FUNCTION raw.validate_capture_run_plan()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    if raw.canonical_sha256(new.payload) <> new.content_sha256
       or new.payload->>'run_id' <> new.run_id
       or new.payload->>'release_manifest_id' <> new.release_manifest_id then
        raise check_violation using message = 'Production TOPT run plan identity drifted';
    end if;
    return new;
end;
$function$;

do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_attempt_results'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'c50db9926497ae7a1e4ad3652ead6c98') then drop trigger if exists reject_mutation on raw.capture_attempt_results; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_attempt_results FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_attempt_results'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '5c532e417bacdc65ee86076bb4687e48') then drop trigger if exists validate_attempt_result on raw.capture_attempt_results; CREATE TRIGGER validate_attempt_result BEFORE INSERT ON raw.capture_attempt_results FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_attempt_result(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_attempt_results'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'cb278db039b0f9819555520ba05077de') then drop trigger if exists zz_validate_attempt_result_address on raw.capture_attempt_results; CREATE TRIGGER zz_validate_attempt_result_address BEFORE INSERT ON raw.capture_attempt_results FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_attempt_result_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_attempts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '2cca943e11e51307225a75d36b25b725') then drop trigger if exists enforce_attempt_sequence on raw.capture_attempts; CREATE TRIGGER enforce_attempt_sequence BEFORE INSERT ON raw.capture_attempts FOR EACH ROW EXECUTE FUNCTION raw.enforce_capture_attempt_sequence(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_attempts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'e4e8a683331b0b03d0b447f575c16c7e') then drop trigger if exists reject_mutation on raw.capture_attempts; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_attempts FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_attempts'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'f875963705fd6c3cd83199fd2cdf60c4') then drop trigger if exists zz_validate_attempt_address on raw.capture_attempts; CREATE TRIGGER zz_validate_attempt_address BEFORE INSERT ON raw.capture_attempts FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_attempt_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_campaign_list_versions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'b4520383698dcad23f9e091f3f043cda') then drop trigger if exists reject_mutation on raw.capture_campaign_list_versions; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_campaign_list_versions FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_campaign_list_versions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '82617b05b33f10aef832e738e509c6d9') then drop trigger if exists validate_campaign_list_version on raw.capture_campaign_list_versions; CREATE TRIGGER validate_campaign_list_version BEFORE INSERT ON raw.capture_campaign_list_versions FOR EACH ROW EXECUTE FUNCTION raw.validate_campaign_list_version(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_campaigns'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'b5458d2645ea4b20fe5f4efdd922bea8') then drop trigger if exists reject_mutation on raw.capture_campaigns; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_campaigns FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_campaigns'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'd236bf192a3b9f5e541197ae0cd9cbb2') then drop trigger if exists zz_validate_campaign_address on raw.capture_campaigns; CREATE TRIGGER zz_validate_campaign_address BEFORE INSERT ON raw.capture_campaigns FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_campaign_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_list_version_members'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'a725f6ee425cd7a3cd9aaf321e33a3c3') then drop trigger if exists reject_mutation on raw.capture_list_version_members; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_list_version_members FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_list_version_members'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'fa67db2da991d643001b97b58e055a6d') then drop trigger if exists validate_list_member on raw.capture_list_version_members; CREATE TRIGGER validate_list_member BEFORE INSERT ON raw.capture_list_version_members FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_list_member(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_list_versions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'd36f9995082339c9bb10417c565622f3') then drop trigger if exists reject_mutation on raw.capture_list_versions; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_list_versions FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_list_versions'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '960ca29503921ab0ca4098a89616559d') then drop trigger if exists zz_validate_list_version_address on raw.capture_list_versions; CREATE TRIGGER zz_validate_list_version_address BEFORE INSERT ON raw.capture_list_versions FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_list_version_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligation_results'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'ee5fad991fd3afb60681ae9cef412cad') then drop trigger if exists reject_mutation on raw.capture_obligation_results; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_obligation_results FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligation_work_bindings'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '59dd46a7493ce3b7c130423613e1a69c') then drop trigger if exists reject_mutation on raw.capture_obligation_work_bindings; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_obligation_work_bindings FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligation_work_bindings'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '260372d2ce38ab2bf26c170716393db4') then drop trigger if exists validate_binding_campaign on raw.capture_obligation_work_bindings; CREATE TRIGGER validate_binding_campaign BEFORE INSERT ON raw.capture_obligation_work_bindings FOR EACH ROW EXECUTE FUNCTION raw.validate_obligation_work_campaign(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligation_work_bindings'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '427c46cd1533e0d006b0e76cd9c17fbd') then drop trigger if exists zz_validate_binding_address on raw.capture_obligation_work_bindings; CREATE TRIGGER zz_validate_binding_address BEFORE INSERT ON raw.capture_obligation_work_bindings FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_binding_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '490d75784df3646a4b8dd455b230d465') then drop trigger if exists reject_mutation on raw.capture_obligations; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_obligations FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '4cd30c8aea31780f24aa198a7ecdeb60') then drop trigger if exists validate_obligation_list on raw.capture_obligations; CREATE TRIGGER validate_obligation_list BEFORE INSERT ON raw.capture_obligations FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_obligation_list(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_obligations'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '0474ed99ac9a058683cb647eda0db47c') then drop trigger if exists zz_validate_obligation_address on raw.capture_obligations; CREATE TRIGGER zz_validate_obligation_address BEFORE INSERT ON raw.capture_obligations FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_obligation_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_runs'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '53237c38fb6c9ec3081e762be6a44c13') then drop trigger if exists reject_mutation on raw.capture_runs; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_runs FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_runs'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'd94d8e6aefd2237cd63b7551cff3f4e3') then drop trigger if exists zz_validate_run_address on raw.capture_runs; CREATE TRIGGER zz_validate_run_address BEFORE INSERT ON raw.capture_runs FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_run_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_schedule_policies'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '6dbd816af6be55e26648eed2eb9cdea5') then drop trigger if exists reject_mutation on raw.capture_schedule_policies; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_schedule_policies FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_source_requests'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '5bb49638b9477b296955f739bb65adf8') then drop trigger if exists reject_mutation on raw.capture_source_requests; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_source_requests FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_source_vintages'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '34122c7c12576f0cabce1f5db89482db') then drop trigger if exists reject_mutation on raw.capture_source_vintages; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_source_vintages FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_source_vintages'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '971ec9d036d9099ffbd343b1c0994b42') then drop trigger if exists validate_raw_lineage on raw.capture_source_vintages; CREATE TRIGGER validate_raw_lineage BEFORE INSERT ON raw.capture_source_vintages FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_source_vintage_lineage(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_work_items'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'e29b9cb1a5a2ae8b6bcb6550b00d83e4') then drop trigger if exists reject_mutation on raw.capture_work_items; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_work_items FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_work_items'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '9956aadc7a99a9c11ed5bf7d19a4db0d') then drop trigger if exists zz_validate_work_item_address on raw.capture_work_items; CREATE TRIGGER zz_validate_work_item_address BEFORE INSERT ON raw.capture_work_items FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_work_item_address(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.fetches'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '9db88add0a4ca8a34326b0e284fd5836') then drop trigger if exists trg_raw_fetches_append_only on raw.fetches; CREATE TRIGGER trg_raw_fetches_append_only BEFORE DELETE OR UPDATE ON raw.fetches FOR EACH ROW EXECUTE FUNCTION raw.reject_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_run_plans'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = '7b22901aabf63485b8068ef4a732f9b5') then drop trigger if exists reject_mutation on raw.capture_run_plans; CREATE TRIGGER reject_mutation BEFORE DELETE OR UPDATE ON raw.capture_run_plans FOR EACH ROW EXECUTE FUNCTION raw.reject_capture_control_mutation(); end if; end $$;
do $$ begin if not exists (select 1 from pg_trigger where tgrelid = 'raw.capture_run_plans'::regclass and not tgisinternal and md5(pg_get_triggerdef(oid)) = 'c17b007de6210e8b5f4e208eb13f2428') then drop trigger if exists validate_plan on raw.capture_run_plans; CREATE TRIGGER validate_plan BEFORE INSERT ON raw.capture_run_plans FOR EACH ROW EXECUTE FUNCTION raw.validate_capture_run_plan(); end if; end $$;

do $$ begin if to_regclass('raw.idx_raw_fetches_source_time') is null then CREATE INDEX idx_raw_fetches_source_time ON raw.fetches USING btree (source, source_record_id, fetched_at DESC); end if; end $$;
do $$ begin if to_regclass('raw.ix_raw_fetches_payload_sha256') is null then CREATE INDEX ix_raw_fetches_payload_sha256 ON raw.fetches USING btree (payload_sha256); end if; end $$;
